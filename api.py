# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shopify Checkout API — FastAPI Edition (Request-Based)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Drop-in replacement for the original Litestar API.
# Uses standard Python requests library instead of tls_requests.
#
# Endpoints:
#   GET  /health               → health check
#   GET  /status               → live queue/circuit-breaker stats
#   GET  /shopify              → run checkout (query params)
#   POST /shopify              → run checkout (JSON body)
#   GET  /circuit-breaker      → circuit-breaker state per site
#   GET  /cache/stats          → product cache stats
#
# Config (env vars):
#   PORT=8080
#   MAX_WORKERS=10             concurrent thread pool size
#   CHECKOUT_TIMEOUT=45        seconds before aborting a checkout
#   CB_FAIL_THRESHOLD=5        failures before tripping circuit breaker
#   CB_COOLDOWN=60             seconds to keep circuit breaker open
#   CACHE_TTL=300              product cache TTL in seconds
#   DELAY_SCALE=0.25           scale factor for human-delay pauses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from core import (
    parse_cc_string,
    process_card,
    extract_clean_response,
)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════
_MAX_WORKERS = int(os.environ.get('MAX_WORKERS', '10'))
_CHECKOUT_TIMEOUT = int(os.environ.get('CHECKOUT_TIMEOUT', '45'))
_CB_FAIL_THRESHOLD = int(os.environ.get('CB_FAIL_THRESHOLD', '5'))
_CB_COOLDOWN = int(os.environ.get('CB_COOLDOWN', '60'))
_CACHE_TTL = int(os.environ.get('CACHE_TTL', '300'))
_CACHE_MAXSIZE = int(os.environ.get('CACHE_MAXSIZE', '200'))

# ══════════════════════════════════════════════════════════════════════
# THREAD POOL
# ══════════════════════════════════════════════════════════════════════
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

# ══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════
class _CircuitBreaker:
    """Per-site circuit breaker. Opens after N consecutive failures."""

    def __init__(self):
        self._lock = threading.Lock()
        self.fail_count = 0
        self.tripped_at: float = 0.0

    def is_open(self) -> bool:
        with self._lock:
            if self.tripped_at and (time.monotonic() - self.tripped_at) < _CB_COOLDOWN:
                return True
            return False

    def record_failure(self):
        with self._lock:
            self.fail_count += 1
            if self.fail_count >= _CB_FAIL_THRESHOLD:
                self.tripped_at = time.monotonic()

    def record_success(self):
        with self._lock:
            self.fail_count = 0
            self.tripped_at = 0.0

    def cooldown_remaining(self) -> float:
        with self._lock:
            if not self.tripped_at:
                return 0.0
            return max(0.0, _CB_COOLDOWN - (time.monotonic() - self.tripped_at))


_circuit_breakers: dict[str, _CircuitBreaker] = {}
_cb_lock = threading.Lock()
_cb_rejected = 0


def _get_cb(domain: str) -> _CircuitBreaker:
    with _cb_lock:
        if domain not in _circuit_breakers:
            _circuit_breakers[domain] = _CircuitBreaker()
        return _circuit_breakers[domain]


_SITE_FAILURE_PATTERNS = [
    'connection', 'ssl', 'timeout', 'network', 'dns',
    'proxy_error', 'ssl_error', 'connection_error',
    'checkout_page_failed', 'proposal_blocked',
]


def _is_site_failure(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    return any(p in m for p in _SITE_FAILURE_PATTERNS)


# ══════════════════════════════════════════════════════════════════════
# PRODUCT CACHE (in-memory, thread-safe)
# ══════════════════════════════════════════════════════════════════════
_product_cache: dict[str, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        entry = _product_cache.get(key)
        if entry:
            cached_at, value = entry
            if time.monotonic() - cached_at < _CACHE_TTL:
                return value
            del _product_cache[key]
    return None


def _cache_set(key: str, value: tuple):
    with _cache_lock:
        if len(_product_cache) >= _CACHE_MAXSIZE:
            # Evict oldest entry
            oldest_key = next(iter(_product_cache))
            del _product_cache[oldest_key]
        _product_cache[key] = (time.monotonic(), value)


# ══════════════════════════════════════════════════════════════════════
# STATS COUNTERS
# ══════════════════════════════════════════════════════════════════════
_stats_lock = threading.Lock()
_stats = {
    'total_requests': 0,
    'active_requests': 0,
    'completed_requests': 0,
    'failed_requests': 0,
    'timeout_requests': 0,
    'cb_rejected': 0,
}


def _inc_stat(key: str, delta: int = 1):
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + delta


# ══════════════════════════════════════════════════════════════════════
# CORE CHECKOUT RUNNER
# ══════════════════════════════════════════════════════════════════════
def _run_checkout(cc_string: str, site: str, proxy_str: str | None, variant_id: str | None) -> dict:
    """Run a checkout in a thread pool worker and return the result dict."""
    _inc_stat('total_requests')
    _inc_stat('active_requests')

    try:
        card_number, month, year, cvv = parse_cc_string(cc_string)
    except (ValueError, TypeError) as e:
        _inc_stat('active_requests', -1)
        _inc_stat('failed_requests')
        return {
            'Status': False,
            'Response': str(e),
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Currency': 'USD',
            'cc': cc_string,
        }

    site_url = site if site.startswith('http') else f'https://{site}'
    domain = urlparse(site_url).netloc

    cb = _get_cb(domain)
    if cb.is_open():
        _inc_stat('active_requests', -1)
        _inc_stat('cb_rejected')
        return {
            'Status': False,
            'Response': 'SITE_CIRCUIT_OPEN: Site temporarily blocked due to repeated failures',
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Currency': 'USD',
            'cc': cc_string,
        }

    try:
        future = _executor.submit(process_card, card_number, month, year, cvv, site_url, variant_id, proxy_str)
        success, message, gateway, price, currency = future.result(timeout=_CHECKOUT_TIMEOUT)

        clean = extract_clean_response(message)

        if _is_site_failure(message):
            cb.record_failure()
        elif success or message in ('ORDER_PLACED', '3DS_REQUIRED', 'OTP_REQUIRED', 'INSUFFICIENT_FUNDS'):
            cb.record_success()

        _inc_stat('completed_requests')
        try:
            price_float = float(price)
        except (ValueError, TypeError):
            price_float = 0.0

        return {
            'Status': success,
            'Response': clean,
            'Gateway': gateway,
            'Price': price_float,
            'Currency': currency,
            'cc': cc_string,
        }

    except FuturesTimeoutError:
        _inc_stat('timeout_requests')
        cb.record_failure()
        return {
            'Status': False,
            'Response': f'CHECKOUT_TIMEOUT: Exceeded {_CHECKOUT_TIMEOUT}s',
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Currency': 'USD',
            'cc': cc_string,
        }
    except Exception as e:
        cb.record_failure()
        _inc_stat('failed_requests')
        return {
            'Status': False,
            'Response': f'ERROR: {type(e).__name__}: {str(e)[:150]}',
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Currency': 'USD',
            'cc': cc_string,
        }
    finally:
        _inc_stat('active_requests', -1)


# ══════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════════════════════════════════
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(_app):
    print(f"[startup] Shopify API ready — {_MAX_WORKERS} workers, timeout={_CHECKOUT_TIMEOUT}s", file=sys.stderr)
    yield
    print("[shutdown] Shutting down thread pool...", file=sys.stderr)
    _executor.shutdown(wait=True, cancel_futures=False)
    print("[shutdown] Done.", file=sys.stderr)

app = FastAPI(lifespan=lifespan,
    title="Shopify Checkout API",
    description="Request-based Shopify checkout flow — supports GET and POST",
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ──────────────────────────────────────────────────────────────────────
# REQUEST MODEL (POST body)
# ──────────────────────────────────────────────────────────────────────
class ShopifyRequest(BaseModel):
    site: str
    cc: str
    proxy: Optional[str] = None
    variant: Optional[str] = None
    user_id: Optional[str] = "anonymous"

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "site": "allbirds.com",
            "cc": "4111111111111111|12|2028|123",
            "proxy": "user:pass@host:port",
            "variant": None,
            "user_id": "user123",
        }
    })


# ──────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ──────────────────────────────────────────────────────────────────────
VERSION = "2.4.9"

@app.get("/health", tags=["System"])
async def health():
    """Simple health check."""
    return {"status": "ok", "workers": _MAX_WORKERS, "version": VERSION}


@app.post("/debug", tags=["System"])
async def debug_checkout(req: ShopifyRequest):
    """Debug endpoint — returns raw logs for diagnosing checkout failures."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        loop = asyncio.get_event_loop()
        status, response, gw, price, currency = await loop.run_in_executor(
            _executor, lambda: run_checkout(req.site, req.cc)
        )
    logs = buf.getvalue()
    return {
        "status": status, "response": response,
        "gateway": gw, "price": price, "currency": currency,
        "logs": logs[-4000:],
    }


# ──────────────────────────────────────────────────────────────────────
# LIVE STATUS
# ──────────────────────────────────────────────────────────────────────
@app.get("/status", tags=["System"])
async def api_status():
    """Live API statistics and system status."""
    with _stats_lock:
        stats_snapshot = dict(_stats)

    with _cache_lock:
        cache_total = len(_product_cache)
        fresh = sum(
            1 for _, (ts, _) in _product_cache.items()
            if time.monotonic() - ts < _CACHE_TTL
        )

    tripped = [
        d for d, cb in _circuit_breakers.items()
        if cb.is_open()
    ]

    return {
        "requests": stats_snapshot,
        "circuit_breaker": {
            "tripped_sites": tripped,
            "threshold": _CB_FAIL_THRESHOLD,
            "cooldown_seconds": _CB_COOLDOWN,
        },
        "product_cache": {
            "total": cache_total,
            "fresh": fresh,
            "expired": cache_total - fresh,
            "ttl_seconds": _CACHE_TTL,
        },
        "config": {
            "max_workers": _MAX_WORKERS,
            "checkout_timeout": _CHECKOUT_TIMEOUT,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER STATUS
# ──────────────────────────────────────────────────────────────────────
@app.get("/circuit-breaker", tags=["System"])
async def circuit_breaker():
    """Circuit breaker state for all tracked domains."""
    sites = {}
    for domain, cb in _circuit_breakers.items():
        sites[domain] = {
            "fail_count": cb.fail_count,
            "is_open": cb.is_open(),
            "cooldown_remaining_seconds": round(cb.cooldown_remaining(), 1),
        }
    return {
        "sites": sites,
        "threshold": _CB_FAIL_THRESHOLD,
        "cooldown_seconds": _CB_COOLDOWN,
    }


# ──────────────────────────────────────────────────────────────────────
# PRODUCT CACHE STATS
# ──────────────────────────────────────────────────────────────────────
@app.get("/cache/stats", tags=["System"])
async def cache_stats():
    """Product cache statistics."""
    with _cache_lock:
        total = len(_product_cache)
        now = time.monotonic()
        fresh = sum(1 for _, (ts, _) in _product_cache.items() if now - ts < _CACHE_TTL)
    return {
        "total_entries": total,
        "fresh_entries": fresh,
        "expired_entries": total - fresh,
        "ttl_seconds": _CACHE_TTL,
        "max_size": _CACHE_MAXSIZE,
    }


# ──────────────────────────────────────────────────────────────────────
# SHOPIFY CHECKOUT — GET (query params)
# ──────────────────────────────────────────────────────────────────────
@app.get("/shopify", tags=["Checkout"])
async def shopify_get(
    site: str = Query(..., description="Shopify store URL, e.g. allbirds.com"),
    cc: str = Query(..., description="Card in CC|MM|YYYY|CVV format"),
    proxy: Optional[str] = Query(None, description="Proxy: user:pass@host:port or ip:port:user:pass"),
    variant: Optional[str] = Query(None, description="Product variant ID (auto-detected if omitted)"),
    user_id: Optional[str] = Query("anonymous", description="User identifier for logging"),
):
    """
    Run a Shopify checkout via GET query parameters.

    Example:
        GET /shopify?site=allbirds.com&cc=4111111111111111|12|2028|123

    Returns:
        {
          "Status": true/false,
          "Response": "ORDER_PLACED" | "CARD_DECLINED" | "3DS_REQUIRED" | ...,
          "Gateway": "Shopify Payments" | "Stripe" | ...,
          "Price": 59.95,
          "Currency": "USD",
          "cc": "4111111111111111|12|2028|123"
        }
    """
    if not site or not cc:
        raise HTTPException(status_code=400, detail="Both 'site' and 'cc' are required.")

    loop = __import__('asyncio').get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _run_checkout(cc_string=cc, site=site, proxy_str=proxy, variant_id=variant),
    )
    return JSONResponse(content=result)


# ──────────────────────────────────────────────────────────────────────
# SHOPIFY CHECKOUT — POST (JSON body)
# ──────────────────────────────────────────────────────────────────────
@app.post("/shopify", tags=["Checkout"])
async def shopify_post(body: ShopifyRequest):
    """
    Run a Shopify checkout via POST JSON body.

    Body:
        {
          "site": "allbirds.com",
          "cc": "4111111111111111|12|2028|123",
          "proxy": "user:pass@host:port",  // optional
          "variant": "12345678",            // optional
          "user_id": "user123"              // optional
        }

    Returns same response schema as GET /shopify.
    """
    loop = __import__('asyncio').get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _run_checkout(
            cc_string=body.cc,
            site=body.site,
            proxy_str=body.proxy,
            variant_id=body.variant,
        ),
    )
    return JSONResponse(content=result)





# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    workers = int(os.environ.get("WEB_WORKERS", "1"))
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        reload=False,
    )
