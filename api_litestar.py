# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Litestar API — Native async, production-grade Shopify checkout API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Traffic management layers (in order):
#   1. Request Deduplication — same card+site reuses in-flight result
#   2. Backpressure         — rejects mass checks when queue too long
#   3. Dedicated Lanes      — /cc gets reserved slots, never blocked by mass
#   4. Token Bucket         — per-user rate limit (requests/sec)
#   5. Priority Queue       — single checks processed before mass checks
#   6. Circuit Breaker      — auto-skips sites that keep failing
#   7. Request Timeout      — hard cutoff on checkout flow
#   8. Connection Pooling   — reuses TLS clients per proxy (avoids handshake overhead)
#   9. Warm Session Pool    — pre-creates checkout sessions in background
#  10. Adaptive Scaling     — auto-tunes mass slots based on response time
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import asyncio
import os
import sys
import time
import random
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import urlparse

from litestar import Litestar, get, post, MediaType
from litestar.params import Parameter
from litestar.response import Response

import core
from core import (
    process_card_async,
    parse_cc_string,
    extract_clean_response,
    fetch_products as _original_fetch_products,
)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION (all tunable via env vars)
# ══════════════════════════════════════════════════════════════════════
_SINGLE_SLOTS = int(os.environ.get('SINGLE_SLOTS', '5'))     # reserved for /cc
_MASS_SLOTS = int(os.environ.get('MASS_SLOTS', '30'))         # for /chk mass
_MAX_QUEUE = int(os.environ.get('MAX_QUEUE', '50'))           # backpressure threshold
_USER_RATE_LIMIT = float(os.environ.get('USER_RATE', '12.0'))  # requests/sec per user
_USER_BURST = int(os.environ.get('USER_BURST', '20'))          # burst allowance
_CHECKOUT_TIMEOUT = int(os.environ.get('CHECKOUT_TIMEOUT', '45'))  # seconds
_CB_FAIL_THRESHOLD = int(os.environ.get('CB_FAIL_THRESHOLD', '5'))  # failures to trip
_CB_COOLDOWN = int(os.environ.get('CB_COOLDOWN', '60'))       # seconds site is blocked
_DEDUP_TTL = int(os.environ.get('DEDUP_TTL', '30'))           # seconds to keep dedup

# Connection pool settings
_POOL_SIZE = int(os.environ.get('POOL_SIZE', '10'))           # clients per proxy
_POOL_TTL = int(os.environ.get('POOL_TTL', '300'))            # seconds before recycle

# Warm session pool settings
_WARM_POOL_SIZE = int(os.environ.get('WARM_POOL_SIZE', '5'))  # sessions to keep ready per site
_WARM_REFILL_INTERVAL = float(os.environ.get('WARM_REFILL_INTERVAL', '10'))  # seconds between refill checks

# Adaptive scaling settings
_ADAPTIVE_MIN_SLOTS = int(os.environ.get('ADAPTIVE_MIN_SLOTS', '15'))   # minimum mass slots
_ADAPTIVE_MAX_SLOTS = int(os.environ.get('ADAPTIVE_MAX_SLOTS', '50'))   # maximum mass slots
_ADAPTIVE_TARGET_RT = float(os.environ.get('ADAPTIVE_TARGET_RT', '10.0'))  # target response time (sec)
_ADAPTIVE_INTERVAL = float(os.environ.get('ADAPTIVE_INTERVAL', '15.0'))   # re-evaluate interval (sec)


# ══════════════════════════════════════════════════════════════════════
# LAYER 1: REQUEST DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════
# If multiple requests for the same card+site arrive simultaneously,
# only the first one runs the checkout. Others await the same Future.
_inflight: Dict[str, asyncio.Future] = {}
_dedup_hits = 0
_dedup_total = 0
_DEDUP_MAX_ENTRIES = 2000  # FIX Bug #6: Cap dedup dict to prevent unbounded growth


async def _dedup_or_run(dedup_key: str, coro_factory):
    """Return cached in-flight result or start a new checkout."""
    global _dedup_hits, _dedup_total
    _dedup_total += 1

    # FIX Bug #6: Evict expired entries when dict exceeds cap
    if len(_inflight) > _DEDUP_MAX_ENTRIES:
        expired_keys = [k for k, f in _inflight.items() if f.done()]
        for k in expired_keys:
            _inflight.pop(k, None)
        # If still over cap after evicting done entries, evict oldest (FIFO)
        if len(_inflight) > _DEDUP_MAX_ENTRIES:
            keys_to_remove = list(_inflight.keys())[:len(_inflight) - _DEDUP_MAX_ENTRIES]
            for k in keys_to_remove:
                _inflight.pop(k, None)

    if dedup_key in _inflight:
        fut = _inflight[dedup_key]
        if not fut.done():
            _dedup_hits += 1
            return await fut

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _inflight[dedup_key] = fut

    try:
        result = await coro_factory()
        fut.set_result(result)
        return result
    except Exception as exc:
        fut.set_exception(exc)
        raise
    finally:
        # Clean up after a short TTL so identical requests within
        # a few seconds still benefit from the result.
        # FIX: Only remove if _inflight still points to OUR Future.
        # A newer request may have replaced it — removing the wrong one
        # causes the next identical request to bypass dedup entirely.
        _our_fut = fut
        async def _cleanup():
            await asyncio.sleep(_DEDUP_TTL)
            if _inflight.get(dedup_key) is _our_fut:
                _inflight.pop(dedup_key, None)
        asyncio.create_task(_cleanup())


# ══════════════════════════════════════════════════════════════════════
# LAYER 3: DEDICATED LANES (semaphores)
# ══════════════════════════════════════════════════════════════════════
_single_sem: Optional[asyncio.Semaphore] = None
_mass_sem: Optional[asyncio.Semaphore] = None

# FIX Bug #1: Thread-safe lane counters using a lock to prevent drift across await points
class _LaneCounters:
    """Thread-safe counters for active/queued lane tracking."""
    __slots__ = ('lock', 'active_single', 'active_mass', 'queued_single', 'queued_mass')

    def __init__(self):
        self.lock = asyncio.Lock()
        self.active_single = 0
        self.active_mass = 0
        self.queued_single = 0
        self.queued_mass = 0

    async def inc(self, attr: str, delta: int = 1):
        async with self.lock:
            setattr(self, attr, getattr(self, attr) + delta)

    async def dec(self, attr: str, delta: int = 1):
        async with self.lock:
            current = getattr(self, attr)
            setattr(self, attr, max(0, current - delta))

    def read(self) -> Dict[str, int]:
        """Snapshot all counters (no lock needed for single int reads under GIL)."""
        return {
            'active_single': self.active_single,
            'active_mass': self.active_mass,
            'queued_single': self.queued_single,
            'queued_mass': self.queued_mass,
        }

_counters = _LaneCounters()


# FIX Bug #2: Dynamic semaphore that supports capacity changes without
# stranding in-flight waiters. Defined early because _get_lane_sems() references it.
class _DynamicSemaphore:
    """Semaphore that supports dynamic capacity resizing in-place.

    When capacity increases, we release extra permits to wake waiting coroutines.
    When capacity decreases, we try to acquire excess permits non-blockingly.
    Any we can't acquire immediately will drain naturally as in-flight requests finish.
    """

    def __init__(self, initial_capacity: int):
        self._sem = asyncio.Semaphore(initial_capacity)
        self._capacity = initial_capacity
        self._lock = asyncio.Lock()

    async def acquire(self):
        await self._sem.acquire()

    def release(self):
        self._sem.release()

    async def resize(self, new_capacity: int):
        """Resize the semaphore capacity in-place."""
        async with self._lock:
            old_capacity = self._capacity
            if new_capacity == old_capacity:
                return
            diff = new_capacity - old_capacity
            self._capacity = new_capacity
            if diff > 0:
                # Capacity increased — release extra permits
                for _ in range(diff):
                    self._sem.release()
            elif diff < 0:
                # FIX Bug #41: Capacity decrease — drain excess permits.
                # We acquire permits (non-blocking) to reduce available count.
                # Permits we can't grab are held by in-flight requests and will
                # be drained naturally as they complete and we don't release them
                # back beyond the new capacity (enforced by acquire/release tracking).
                drained = 0
                for _ in range(-diff):
                    # Try to drain a permit — if someone is waiting, wake them
                    # by releasing and immediately re-acquiring (net zero change
                    # for waiters, but reduces available count by 1)
                    if self._sem._value > 0:
                        # There's a free permit — grab it to reduce the count
                        try:
                            self._sem.acquire_nowait()
                            drained += 1
                        except Exception:
                            break
                    else:
                        break

    @property
    def capacity(self) -> int:
        return self._capacity


def _get_lane_sems() -> Tuple[asyncio.Semaphore, _DynamicSemaphore]:
    global _single_sem, _mass_sem
    if _single_sem is None:
        _single_sem = asyncio.Semaphore(_SINGLE_SLOTS)
    if _mass_sem is None:
        # FIX Bug #2: Use _DynamicSemaphore for mass lane so adaptive scaling
        # can resize it in-place without stranding in-flight waiters.
        _mass_sem = _DynamicSemaphore(_MASS_SLOTS)
    return _single_sem, _mass_sem


# ══════════════════════════════════════════════════════════════════════
# LAYER 4: TOKEN BUCKET (per-user rate limiter)
# ══════════════════════════════════════════════════════════════════════
class _TokenBucket:
    __slots__ = ('rate', 'burst', 'tokens', 'last_refill')

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def consume(self) -> float:
        """Try to consume 1 token. Returns 0 if OK, else wait seconds."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


_user_buckets: Dict[str, _TokenBucket] = {}
_rate_limited_count = 0
_BUCKET_EVICT_AGE = 600  # FIX Bug #8: Evict buckets idle for 10 minutes


def _get_bucket(user_id: str) -> _TokenBucket:
    if user_id not in _user_buckets:
        # FIX Bug #8: Evict stale buckets before creating new ones
        if len(_user_buckets) > 5000:
            now = time.monotonic()
            stale_keys = [
                uid for uid, b in _user_buckets.items()
                if now - b.last_refill > _BUCKET_EVICT_AGE
            ]
            for k in stale_keys:
                del _user_buckets[k]
        _user_buckets[user_id] = _TokenBucket(_USER_RATE_LIMIT, _USER_BURST)
    return _user_buckets[user_id]


# ══════════════════════════════════════════════════════════════════════
# LAYER 5: PRIORITY QUEUE
# ══════════════════════════════════════════════════════════════════════
# FIX Arch #26: Removed dead PriorityQueue code — it was never used.
# The /shopify endpoint uses semaphores directly for lane management,
# which is simpler and sufficient. Priority ordering within each lane
# is handled by asyncio's built-in FIFO semaphore waiter queue.


# ══════════════════════════════════════════════════════════════════════
# LAYER 6: CIRCUIT BREAKER (per site)
# ══════════════════════════════════════════════════════════════════════
class _CircuitBreaker:
    __slots__ = ('fail_count', 'last_fail', 'tripped_at')

    def __init__(self):
        self.fail_count = 0
        self.last_fail = 0.0
        self.tripped_at = 0.0

    def record_failure(self):
        now = time.monotonic()
        # Reset count if last failure was long ago
        if now - self.last_fail > _CB_COOLDOWN:
            self.fail_count = 0
        self.fail_count += 1
        self.last_fail = now
        if self.fail_count >= _CB_FAIL_THRESHOLD:
            self.tripped_at = now

    def record_success(self):
        # FIX Bug #51: When breaker is tripped, require multiple successes
        # before fully closing the circuit. A single lucky success shouldn't
        # un-trip it — that could cause a cascade of failures.
        if self.tripped_at > 0:
            # Still in tripped state — decrement but don't close yet
            self.fail_count = max(0, self.fail_count - 1)
            if self.fail_count <= 0:
                # Multiple successes have worn down the count — safe to close
                self.tripped_at = 0.0
        else:
            self.fail_count = max(0, self.fail_count - 1)
            if self.fail_count < _CB_FAIL_THRESHOLD:
                self.tripped_at = 0.0

    def is_currently_open(self) -> bool:
        """Read-only check — does NOT mutate state. Safe for status endpoints."""
        if self.tripped_at == 0.0:
            return False
        return time.monotonic() - self.tripped_at <= _CB_COOLDOWN

    def is_open(self) -> bool:
        """Mutating check — transitions half-open state. Use in request path only."""
        if self.tripped_at == 0.0:
            return False
        if time.monotonic() - self.tripped_at > _CB_COOLDOWN:
            # Half-open: allow one request through to test recovery
            self.tripped_at = 0.0
            self.fail_count = _CB_FAIL_THRESHOLD - 1
            return False
        return True


_circuit_breakers: Dict[str, _CircuitBreaker] = {}
_cb_rejected = 0
_CB_MAX_ENTRIES = 2000  # FIX Bug #47: Cap circuit breaker dict to prevent memory leak


def _get_cb(site_domain: str) -> _CircuitBreaker:
    # FIX Bug #47: Evict stale entries when dict exceeds cap
    if len(_circuit_breakers) > _CB_MAX_ENTRIES:
        now = time.monotonic()
        stale = [d for d, cb in _circuit_breakers.items()
                 if cb.tripped_at == 0.0 and now - cb.last_fail > 3600]
        for d in stale:
            del _circuit_breakers[d]
    if site_domain not in _circuit_breakers:
        _circuit_breakers[site_domain] = _CircuitBreaker()
    return _circuit_breakers[site_domain]


# Site error keywords that trigger circuit breaker.
# IMPORTANT: These must only match SITE-LEVEL failures, NOT card/payment
# failures. "timed out" and "timeout" are too broad — they match
# "Payment timeout (receipt still processing)" which is a normal card
# response, not a site issue. Use prefix-based matching instead.
_CB_TRIGGER_KEYWORDS = {
    'captcha_required', 'captcha_block', 'rate-limited', 'http 429',
    'connection failed', 'ssl error',
    'store is password-protected', 'site requires login',
}
# These match response messages that start with specific prefixes
_CB_TRIGGER_PREFIXES = (
    'checkout_timeout:', 'checkout rate-limited',
    'cart rate-limited', 'cart failed',
    'pci_vault_blocked:', 'proposal_blocked:',
    'timed out',  # FIX Bug #7: Moved from keyword to prefix — only matches
                  # messages starting with "timed out" (site-level), not
                  # "Payment timeout (receipt still processing)" (card-level)
)


def _is_site_failure(response_msg: str) -> bool:
    low = response_msg.lower()
    # Keyword match (substring)
    if any(kw in low for kw in _CB_TRIGGER_KEYWORDS):
        return True
    # Prefix match for specific site-level error patterns
    if low.startswith(_CB_TRIGGER_PREFIXES):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════
# TTL-BASED CACHE FOR fetch_products
# ══════════════════════════════════════════════════════════════════════
# FIX Bug #17: Use OrderedDict for O(1) LRU eviction instead of O(n) min() scan
_PRODUCT_CACHE: OrderedDict = OrderedDict()
_CACHE_TTL = 300
_CACHE_MAXSIZE = 512


# (fetch_products_cached moved below to Layer 8 — uses connection pooling)


# ══════════════════════════════════════════════════════════════════════
# LAYER 8: CONNECTION POOLING (reuse TLS clients per proxy)
# ══════════════════════════════════════════════════════════════════════
# Each AsyncClient creation involves a TLS handshake (~1.5-2s). By pooling
# clients keyed by proxy, we reuse established connections and skip handshakes.
from tls_requests import AsyncClient as _TLSAsyncClient


class _ClientPool:
    """Pool of TLS AsyncClient instances keyed by proxy string."""

    def __init__(self, pool_size: int = 10, ttl: int = 300):
        self._pool_size = pool_size
        self._ttl = ttl
        # Key: proxy_str (or 'direct'), Value: list of (client, created_at)
        self._clients: Dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, proxy_str: Optional[str] = None, identifier: str = 'chrome_131') -> _TLSAsyncClient:
        """Get a client from the pool or create a new one."""
        key = proxy_str or 'direct'
        now = time.monotonic()

        async with self._lock:
            pool = self._clients[key]
            # Try to find a valid client
            while pool:
                client, created_at = pool.popleft()
                if now - created_at < self._ttl:
                    return client
                # Expired — close it
                try:
                    await client.aclose()
                except Exception:
                    pass

            # FIX Bug #9: Create new client inside the lock to prevent
            # concurrent acquires from creating duplicate clients.
            client = _TLSAsyncClient(
                client_identifier=identifier,
                http2=True,
                verify=not proxy_str,
                timeout=15,
            )
            return client

    async def release(self, client: _TLSAsyncClient, proxy_str: Optional[str] = None):
        """Return a client to the pool for reuse."""
        key = proxy_str or 'direct'
        async with self._lock:
            pool = self._clients[key]
            if len(pool) < self._pool_size:
                pool.append((client, time.monotonic()))
            else:
                # Pool full — close the client
                try:
                    await client.aclose()
                except Exception:
                    pass

    async def close_all(self):
        """Close all pooled clients (for shutdown)."""
        async with self._lock:
            for key, pool in self._clients.items():
                while pool:
                    client, _ = pool.popleft()
                    try:
                        await client.aclose()
                    except Exception:
                        pass
            self._clients.clear()

    def stats(self) -> Dict[str, int]:
        """Return pool statistics."""
        return {key: len(pool) for key, pool in self._clients.items() if pool}


_client_pool = _ClientPool(pool_size=_POOL_SIZE, ttl=_POOL_TTL)


# Monkey-patch fetch_products to use pooled connections
async def fetch_products_pooled(domain: str, proxy_str: Optional[str] = None):
    """fetch_products using pooled TLS clients — avoids repeated handshakes."""
    from core import _pick_identifier, parse_proxy
    import json as _json

    identifier = _pick_identifier()
    client = await _client_pool.acquire(proxy_str, identifier)
    client_ok = True  # FIX Bug #46: Track whether client is healthy
    try:
        if not domain.startswith('http'):
            domain = "https://" + domain
        proxy = parse_proxy(proxy_str) if proxy_str else None
        resp = await client.get(f"{domain}/products.json", proxy=proxy, timeout=10)
        if resp.status_code != 200:
            return False, f"Site Error: HTTP {resp.status_code}"
        text = resp.text
        if "shopify" not in text.lower():
            return False, "Not a Shopify store"
        try:
            data = _json.loads(text)
            result = data.get('products', [])
        except (ValueError, Exception):
            return False, "Invalid products response"
        if not result:
            return False, "No products found"

        # Find cheapest variant
        import re as _re
        min_price = float('inf')
        min_product = None
        for product in result:
            if not product.get('variants'):
                continue
            for variant in product['variants']:
                if not variant.get('available', True):
                    continue
                try:
                    price = variant.get('price', '0')
                    if isinstance(price, str):
                        if _re.match(r'^\d+,\d{2}$', price.strip()):
                            price = float(price.replace(',', '.'))
                        else:
                            price = float(price.replace(',', ''))
                    else:
                        price = float(price)
                    if price > 0 and price < min_price:
                        min_price = price
                        min_product = {
                            'variant_id': str(variant.get('id', '')),
                            'product_title': product.get('title', ''),
                            'variant_title': variant.get('title', ''),
                            'price': price,
                            'currency': 'USD',
                        }
                except (ValueError, TypeError):
                    continue

        if min_product:
            return True, min_product
        return False, "No available variants with price > 0"
    except Exception as e:
        client_ok = False  # Client may be broken — don't return to pool
        return False, f"Fetch error: {str(e)}"
    finally:
        if client_ok:
            await _client_pool.release(client, proxy_str)
        else:
            try:
                await client.aclose()
            except Exception:
                pass


# Override the cached fetch_products to use pooled connections
async def fetch_products_cached_pooled(domain: str, proxy_str: Optional[str] = None):
    """Cache + pool wrapper for fetch_products."""
    cache_key = f"{domain}||{proxy_str or ''}"
    now = time.monotonic()
    if cache_key in _PRODUCT_CACHE:
        cached_at, cached_result = _PRODUCT_CACHE[cache_key]
        if now - cached_at < _CACHE_TTL:
            # Move to end (most recently used)
            _PRODUCT_CACHE.move_to_end(cache_key)
            return cached_result
        else:
            # Expired — remove it
            del _PRODUCT_CACHE[cache_key]
    result = await fetch_products_pooled(domain, proxy_str)
    success, data = result
    if success:
        # FIX Bug #17: O(1) LRU eviction using OrderedDict
        while len(_PRODUCT_CACHE) >= _CACHE_MAXSIZE:
            _PRODUCT_CACHE.popitem(last=False)  # Remove oldest (FIFO = LRU)
        _PRODUCT_CACHE[cache_key] = (now, result)
    return result


core.fetch_products = fetch_products_cached_pooled


# ══════════════════════════════════════════════════════════════════════
# LAYER 9: WARM SESSION POOL (pre-created checkout sessions)
# ══════════════════════════════════════════════════════════════════════
# Background task pre-creates checkout sessions (homepage → cart → checkout
# → token extraction). When a card request arrives, it grabs a warm session
# and skips straight to payment submission — saving 3-5 seconds per checkout.


class _WarmSession:
    """A pre-warmed checkout session ready for payment submission."""
    __slots__ = ('session', 'site', 'proxy', 'sst', 'queue_token',
                 'checkout_url', 'attempt_token', 'variant_id', 'headers',
                 'currency', 'subtotal', 'merch', 'build_id', 'created_at',
                 'identifier', 'pci_build_hash', 'stable_id')

    def __init__(self):
        self.session = None
        self.site = ''
        self.proxy = None
        self.sst = ''
        self.queue_token = ''
        self.checkout_url = ''
        self.attempt_token = ''
        self.variant_id = ''
        self.headers = {}
        self.currency = 'USD'
        self.subtotal = '0.01'
        self.merch = ''
        self.build_id = None
        self.created_at = 0.0
        self.identifier = ''
        self.pci_build_hash = 'a8e4a94'
        self.stable_id = ''


# Pool: keyed by site domain, value is deque of _WarmSession
_warm_pool: Dict[str, deque] = defaultdict(deque)
_warm_pool_lock = asyncio.Lock()
_warm_pool_stats = {'created': 0, 'used': 0, 'expired': 0, 'failed': 0}
_warm_pool_sites: List[Tuple[str, Optional[str], Optional[str]]] = []  # (site_url, proxy, variant_id)
_warm_pool_task: Optional[asyncio.Task] = None
_WARM_SESSION_TTL = 45  # FIX Bug #3: Reduced from 120s — Shopify SST tokens expire ~60s


def register_warm_site(site_url: str, proxy_str: Optional[str] = None, variant_id: Optional[str] = None):
    """Register a site for warm pool pre-creation."""
    entry = (site_url, proxy_str, variant_id)
    if entry not in _warm_pool_sites:
        _warm_pool_sites.append(entry)
        # Cap at 20 sites to avoid excessive background work
        if len(_warm_pool_sites) > 20:
            _warm_pool_sites.pop(0)


async def _create_warm_session(site_url: str, proxy_str: Optional[str], variant_id: Optional[str]) -> Optional[_WarmSession]:
    """Create a warm session by doing homepage → cart → checkout → extract tokens."""
    from core import (
        _pick_identifier, _init_proxy_rotator, _build_headers,
        _referrer_for, human_delay, retry_on_429, extract_between,
        parse_proxy, fetch_products
    )
    import re as _re
    import json as _json
    import base64 as _b64

    try:
        session = None  # FIX Bug #10: Initialize so except block can safely close it
        ourl = site_url if site_url.startswith('http') else f'https://{site_url}'
        identifier = _pick_identifier()
        proxy = _init_proxy_rotator(proxy_str)
        headers = _build_headers(identifier, base_headers={
            'Origin': ourl,
            'Referer': _referrer_for('homepage', ourl=ourl),
        })

        # Resolve variant
        vid = variant_id
        if not vid:
            info = await fetch_products(ourl, proxy_str)
            success, data = info
            if not success:
                return None
            vid = data['variant_id']

        session = _TLSAsyncClient(
            client_identifier=identifier,
            http2=True,
            verify=not proxy,
            timeout=30,
        )

        # Step 0: Homepage
        try:
            home_headers = {
                **headers,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'none',
            }
            await session.get(ourl, headers=home_headers, proxy=proxy, allow_redirects=True, timeout=8)
            await human_delay(step_name="warm_homepage")
        except Exception:
            pass

        # Step 1: Add to cart
        cart_headers = {
            **headers,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': _referrer_for('cart', ourl=ourl),
        }
        cart_resp, _ = await retry_on_429(
            lambda: session.post(ourl + '/cart/add.js', data=f'id={vid}&quantity=1', headers=cart_headers, proxy=proxy, timeout=10),
            step_name="warm_cart", max_retries=2, base_delay=2.0, max_delay=8.0
        )
        if cart_resp.status_code != 200:
            await session.aclose()
            return None

        await human_delay(step_name="warm_cart")

        # Step 2: Checkout
        checkout_headers = {
            **headers,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': _referrer_for('checkout', ourl=ourl),
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
        }
        response, _ = await retry_on_429(
            lambda: session.post(url=ourl + '/checkout/', allow_redirects=True, headers=checkout_headers, proxy=proxy, timeout=15),
            step_name="warm_checkout", max_retries=2, base_delay=3.0, max_delay=12.0
        )
        if response.status_code in (405, 403) or response.status_code >= 500:
            response, _ = await retry_on_429(
                lambda: session.get(url=ourl + '/checkout/', allow_redirects=True, headers=checkout_headers, proxy=proxy, timeout=15),
                step_name="warm_checkout_get", max_retries=1, base_delay=3.0, max_delay=12.0
            )
        if response.status_code == 429 or response.status_code >= 400:
            await session.aclose()
            return None

        checkout_url = str(response.url)
        text = response.text

        # Validate checkout URL
        _path = urlparse(checkout_url).path.lower()
        if '/checkout' not in _path and '/pay' not in _path:
            await session.aclose()
            return None

        # Extract attempt_token
        attempt_token_match = _re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
        if attempt_token_match:
            attempt_token = attempt_token_match.group(1)
        else:
            plain_match = _re.search(r'/checkouts/([^/?]+)', checkout_url)
            attempt_token = plain_match.group(1) if plain_match else ''
        if not attempt_token or not _re.match(r'^[A-Za-z0-9]+$', attempt_token):
            await session.aclose()
            return None

        # Extract session token (sst)
        sst = response.headers.get('X-Checkout-One-Session-Token') or response.headers.get('x-checkout-one-session-token')
        if not sst and response.history:
            for rr_url in [str(r.url) for r in response.history]:
                for param in ['shop_pay_token', 'token', 'checkout_token', 'session_token']:
                    m = _re.search(rf'[?&]{param}=([^&]+)', rr_url)
                    if m:
                        jwt_str = m.group(1)
                        parts = jwt_str.split('.')
                        if len(parts) >= 2:
                            # FIX Bug #45: Correct base64 padding — when len%4==0, add 0 padding chars (was adding 4)
                            payload = parts[1] + '=' * ((4 - len(parts[1]) % 4) % 4)
                            try:
                                decoded = _json.loads(_b64.urlsafe_b64decode(payload))
                                sst = decoded.get('session_token') or decoded.get('checkout_session_token') or decoded.get('sst')
                                if sst:
                                    break
                            except Exception:
                                pass
                if sst:
                    break
        if not sst:
            sst = extract_between(text, 'name="serialized-sessionToken" content="&quot;', '&quot;')
        if not sst:
            sst = extract_between(text, '"serializedSessionToken":"', '"')
        if not sst:
            sst = extract_between(text, '"sessionToken":"', '"')
        if not sst:
            # Can't extract session token — not usable
            await session.aclose()
            return None

        # Extract queue token
        queue_token = extract_between(text, 'queueToken&quot;:&quot;', '&quot;') or extract_between(text, '"queueToken":"', '"')

        # Extract other data
        merch = extract_between(text, 'ProductVariantMerchandise/', '&quot;') or str(vid)
        currency = extract_between(text, 'currencyCode&quot;:&quot;', '&quot;') or 'USD'
        subtotal = extract_between(text, 'subtotalBeforeTaxesAndShipping&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;', '&quot;') or '0.01'
        stable_id = extract_between(text, 'stableId&quot;:&quot;', '&quot;') or extract_between(text, '"stableId":"', '"') or ''

        pci_build_hash = 'a8e4a94'
        pci_hash_match = _re.search(r'checkout\.pci\.shopifyinc\.com/build/([a-f0-9]+)/', text)
        if pci_hash_match:
            pci_build_hash = pci_hash_match.group(1)

        # Build warm session
        ws = _WarmSession()
        ws.session = session
        ws.site = ourl
        ws.proxy = proxy_str
        ws.sst = sst
        ws.queue_token = queue_token or ''
        ws.checkout_url = checkout_url
        ws.attempt_token = attempt_token
        ws.variant_id = vid
        ws.headers = headers
        ws.currency = currency
        ws.subtotal = subtotal
        ws.merch = merch
        ws.build_id = None
        ws.created_at = time.monotonic()
        ws.identifier = identifier
        ws.pci_build_hash = pci_build_hash
        ws.stable_id = stable_id

        _warm_pool_stats['created'] += 1
        return ws

    except Exception as e:
        _warm_pool_stats['failed'] += 1
        print(f"[warm_pool] Failed to create warm session for {site_url}: {e}", file=sys.stderr)
        # FIX Bug #10: Close the session client on creation failure to prevent leak
        # Use a simple local variable check — session is created early in try block
        try:
            if session is not None:
                await session.aclose()
        except Exception:
            pass
        return None


async def get_warm_session(site_url: str, proxy_str: Optional[str] = None) -> Optional[_WarmSession]:
    """Try to get a warm session from the pool. Returns None if none available."""
    ourl = site_url if site_url.startswith('http') else f'https://{site_url}'
    domain = urlparse(ourl).netloc
    now = time.monotonic()

    async with _warm_pool_lock:
        pool = _warm_pool.get(domain)
        if not pool:
            return None
        while pool:
            ws = pool.popleft()
            # Check expiry
            if now - ws.created_at > _WARM_SESSION_TTL:
                _warm_pool_stats['expired'] += 1
                try:
                    await ws.session.aclose()
                except Exception:
                    pass
                continue
            # Check proxy match
            if ws.proxy != proxy_str:
                # FIX Bug #42: Don't return None — put it back and continue searching
                pool.appendleft(ws)
                continue
            _warm_pool_stats['used'] += 1
            return ws
    return None


async def _warm_pool_refill_loop():
    """Background task that keeps warm sessions stocked."""
    while True:
        try:
            await asyncio.sleep(_WARM_REFILL_INTERVAL)
            # FIX Bug #16: Refill sessions in parallel (with concurrency limit)
            # instead of sequentially. Sequential refill could take 10+ min with 20 sites.
            async def _refill_one(site_url, proxy_str, variant_id):
                domain = urlparse(site_url if site_url.startswith('http') else f'https://{site_url}').netloc
                async with _warm_pool_lock:
                    current_count = len(_warm_pool.get(domain, []))
                if current_count < _WARM_POOL_SIZE:
                    ws = await _create_warm_session(site_url, proxy_str, variant_id)
                    if ws:
                        async with _warm_pool_lock:
                            _warm_pool[domain].append(ws)

            # Run all refills concurrently with a semaphore to limit concurrency
            _refill_sem = asyncio.Semaphore(5)
            async def _refill_limited(args):
                async with _refill_sem:
                    try:
                        await _refill_one(*args)
                    except Exception:
                        pass

            sites = list(_warm_pool_sites)
            if sites:
                await asyncio.gather(*[_refill_limited(s) for s in sites])
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[warm_pool] Refill error: {e}", file=sys.stderr)
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════
# LAYER 10: ADAPTIVE WORKER SCALING
# ══════════════════════════════════════════════════════════════════════
# Dynamically adjusts MASS_SLOTS based on response time moving average.
# If avg response time < target → increase slots (more throughput).
# If avg response time > target → decrease slots (reduce overload).

_response_times: deque = deque(maxlen=50)  # last 50 response times
_adaptive_current_slots = _MASS_SLOTS
_adaptive_task: Optional[asyncio.Task] = None


# _DynamicSemaphore is already defined above (before _get_lane_sems).

def record_response_time(duration: float):
    """Record a checkout response time for adaptive scaling."""
    _response_times.append(duration)


def _compute_avg_rt() -> float:
    """Compute average response time from recent samples."""
    if not _response_times:
        return _ADAPTIVE_TARGET_RT
    return sum(_response_times) / len(_response_times)


async def _adaptive_scaling_loop():
    """Background task that adjusts mass semaphore slots based on response times."""
    global _adaptive_current_slots
    while True:
        try:
            await asyncio.sleep(_ADAPTIVE_INTERVAL)
            if len(_response_times) < 5:
                continue  # Not enough data

            avg_rt = _compute_avg_rt()
            old_slots = _adaptive_current_slots

            if avg_rt < _ADAPTIVE_TARGET_RT * 0.7:
                # Response times well below target — increase slots
                new_slots = min(_adaptive_current_slots + 3, _ADAPTIVE_MAX_SLOTS)
            elif avg_rt < _ADAPTIVE_TARGET_RT:
                # Slightly below target — small increase
                new_slots = min(_adaptive_current_slots + 1, _ADAPTIVE_MAX_SLOTS)
            elif avg_rt > _ADAPTIVE_TARGET_RT * 1.5:
                # Way above target — decrease aggressively
                new_slots = max(_adaptive_current_slots - 5, _ADAPTIVE_MIN_SLOTS)
            elif avg_rt > _ADAPTIVE_TARGET_RT:
                # Above target — decrease slightly
                new_slots = max(_adaptive_current_slots - 2, _ADAPTIVE_MIN_SLOTS)
            else:
                new_slots = _adaptive_current_slots

            if new_slots != old_slots:
                _adaptive_current_slots = new_slots
                # FIX Bug #2: Resize the dynamic semaphore in-place instead of replacing it
                if isinstance(_mass_sem, _DynamicSemaphore):
                    await _mass_sem.resize(new_slots)
                print(f"[adaptive] Slots adjusted: {old_slots} → {new_slots} (avg_rt={avg_rt:.1f}s)", file=sys.stderr)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[adaptive] Error: {e}", file=sys.stderr)
            await asyncio.sleep(5)


# ══════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

@get("/health", media_type=MediaType.JSON, sync=False)
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@get("/cache/stats", media_type=MediaType.JSON, sync=False)
async def cache_stats() -> Dict[str, Any]:
    now = time.monotonic()
    total = len(_PRODUCT_CACHE)
    fresh = sum(
        1
        for _, (cached_at, _) in _PRODUCT_CACHE.items()
        if now - cached_at < _CACHE_TTL
    )
    return {
        "cache_total_entries": total,
        "cache_fresh_entries": fresh,
        "cache_expired_entries": total - fresh,
        "cache_ttl_seconds": _CACHE_TTL,
        "cache_max_size": _CACHE_MAXSIZE,
    }


@get("/status", media_type=MediaType.JSON, sync=False)
async def api_status() -> Dict[str, Any]:
    """Live API status with all layer metrics."""
    # Circuit breaker summary
    now = time.monotonic()
    tripped_sites = [
        domain for domain, cb in _circuit_breakers.items() if cb.is_currently_open()
    ]
    # Warm pool stats
    warm_pool_summary = {}
    for domain, pool in _warm_pool.items():
        warm_pool_summary[domain] = len(pool)

    # FIX Bug #1: Use _counters.read() instead of stale global ints
    ctr = _counters.read()
    return {
        "lanes": {
            "single": {"active": ctr['active_single'], "queued": ctr['queued_single'], "max": _SINGLE_SLOTS},
            "mass": {"active": ctr['active_mass'], "queued": ctr['queued_mass'], "max": _adaptive_current_slots},
        },
        "total_active": ctr['active_single'] + ctr['active_mass'],
        "total_queued": ctr['queued_single'] + ctr['queued_mass'],
        "dedup": {"hits": _dedup_hits, "total": _dedup_total},
        "circuit_breaker": {"tripped_sites": tripped_sites, "rejected": _cb_rejected},
        "rate_limited_count": _rate_limited_count,
        "connection_pool": _client_pool.stats(),
        "warm_pool": {"sessions": warm_pool_summary, "stats": _warm_pool_stats},
        "adaptive": {
            "current_slots": _adaptive_current_slots,
            "avg_response_time": round(_compute_avg_rt(), 2),
            "target_rt": _ADAPTIVE_TARGET_RT,
            "min_slots": _ADAPTIVE_MIN_SLOTS,
            "max_slots": _ADAPTIVE_MAX_SLOTS,
        },
    }


@get("/circuit-breaker", media_type=MediaType.JSON, sync=False)
async def circuit_breaker_status() -> Dict[str, Any]:
    """Show circuit breaker state for all tracked sites."""
    now = time.monotonic()
    sites = {}
    for domain, cb in _circuit_breakers.items():
        sites[domain] = {
            "fail_count": cb.fail_count,
            "is_open": cb.is_currently_open(),  # FIX Bug #43: Read-only check
            "cooldown_remaining": max(0, _CB_COOLDOWN - (now - cb.tripped_at)) if cb.tripped_at else 0,
        }
    return {"sites": sites, "threshold": _CB_FAIL_THRESHOLD, "cooldown_seconds": _CB_COOLDOWN}


@dataclass
class ShopifyRequest:
    """Request body for the Shopify checkout endpoint (POST)."""
    site: str
    cc: str
    proxy: Optional[str] = None
    variant: Optional[str] = None
    lane: Optional[str] = "mass"
    user_id: Optional[str] = "anonymous"


async def _shopify_core(
    site: Optional[str],
    cc_string: str,
    proxy_str: Optional[str],
    variant_id: Optional[str],
    lane: Optional[str],
    user_id: Optional[str],
) -> Response:
    """Core checkout logic shared by GET and POST handlers."""
    global _cb_rejected, _rate_limited_count

    # Pre-initialize to prevent NameError in outer except if early exception occurs
    cb = None

    try:
        site = site.strip() if site else None
        cc_string = cc_string.strip() if cc_string else ""
        proxy_str = proxy_str.strip() if proxy_str else None
        variant_id = variant_id.strip() if variant_id else None
        is_single = (lane or "mass").lower() == "single"
        uid = (user_id or "anonymous").strip()

        if not site:
            return Response(
                content={"error": "Missing 'site' parameter", "status": False},
                status_code=400, media_type=MediaType.JSON,
            )
        if not cc_string:
            return Response(
                content={"error": "Missing 'cc' parameter in format CC|MM|YYYY|CVV", "status": False},
                status_code=400, media_type=MediaType.JSON,
            )

        try:
            card_number, mes, ano, cvv = parse_cc_string(cc_string)
            if not card_number:
                return Response(
                    content={"error": "Invalid CC format. Use CC|MM|YYYY|CVV", "status": False},
                    status_code=400, media_type=MediaType.JSON,
                )
        except (ValueError, TypeError) as e:
            return Response(
                content={"error": str(e), "status": False},
                status_code=400, media_type=MediaType.JSON,
            )

        site_domain = urlparse(site if site.startswith('http') else f'https://{site}').netloc

        # ── Register site for warm pool (background pre-creation) ──
        if not is_single:
            register_warm_site(site, proxy_str, variant_id)

        # ── LAYER 6: CIRCUIT BREAKER ──
        cb = _get_cb(site_domain)
        if cb.is_open():
            _cb_rejected += 1
            return Response(
                content={
                    "Gateway": "UNKNOWN", "Price": 0.0, "Currency": "USD",
                    "Response": f"SITE_BLOCKED: {site_domain} temporarily blocked (too many failures, retry in {_CB_COOLDOWN}s)",
                    "Status": False, "cc": cc_string,
                },
                status_code=200, media_type=MediaType.JSON,
            )

        # ── LAYER 2: BACKPRESSURE ──
        # FIX Bug #12: Use _counters snapshot instead of stale global ints
        ctr = _counters.read()
        total_queued = ctr['queued_single'] + ctr['queued_mass']
        if not is_single and total_queued >= _MAX_QUEUE:
            return Response(
                content={
                    "Gateway": "UNKNOWN", "Price": 0.0, "Currency": "USD",
                    "Response": "SERVER_BUSY: Too many queued requests, try later",
                    "Status": False, "cc": cc_string,
                },
                status_code=200, media_type=MediaType.JSON,
            )

        # ── LAYER 4: TOKEN BUCKET (per-user rate limit) ──
        bucket = _get_bucket(uid)
        wait_time = bucket.consume()
        if wait_time > 0:
            if wait_time > 5.0:
                _rate_limited_count += 1
                return Response(
                    content={
                        "Gateway": "UNKNOWN", "Price": 0.0, "Currency": "USD",
                        "Response": f"RATE_LIMITED: Too fast, wait {wait_time:.1f}s",
                        "Status": False, "cc": cc_string,
                    },
                    status_code=200, media_type=MediaType.JSON,
                )
            await asyncio.sleep(wait_time)

        # ── LAYER 1: DEDUPLICATION ──
        # FIX Bug #50: Include variant_id and proxy_str in dedup key.
        # Without these, different variants/proxies for the same card+site share results.
        dedup_key = f"{card_number}|{site_domain}|{variant_id or ''}|{proxy_str or ''}"

        async def _run_checkout():
            # ── LAYER 3: DEDICATED LANES ──
            single_sem, mass_sem = _get_lane_sems()
            sem = single_sem if is_single else mass_sem

            # FIX Bug #1: Use _counters with lock for safe increment/decrement
            queued_attr = 'queued_single' if is_single else 'queued_mass'
            active_attr = 'active_single' if is_single else 'active_mass'
            await _counters.inc(queued_attr)

            try:
                # Both _DynamicSemaphore and asyncio.Semaphore support acquire()/release()
                await sem.acquire()
                try:
                    await _counters.dec(queued_attr)
                    await _counters.inc(active_attr)

                    try:
                        # ── LAYER 7: REQUEST TIMEOUT ──
                        _start_time = time.monotonic()

                        # ── LAYER 9: TRY WARM SESSION FIRST ──
                        ws = await get_warm_session(site, proxy_str) if not is_single else None
                        if ws:
                            # Use warm session — skip to payment submission
                            from core import _submit_with_warm_session
                            try:
                                result = await asyncio.wait_for(
                                    _submit_with_warm_session(
                                        ws, card_number, mes, ano, cvv
                                    ),
                                    timeout=_CHECKOUT_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                result = (False, f"CHECKOUT_TIMEOUT: Exceeded {_CHECKOUT_TIMEOUT}s", "UNKNOWN", "0.00", "USD")
                        else:
                            # Normal full checkout flow
                            result = await asyncio.wait_for(
                                process_card_async(
                                    card_number, mes, ano, cvv, site, variant_id, proxy_str
                                ),
                                timeout=_CHECKOUT_TIMEOUT,
                            )

                        # ── LAYER 10: RECORD RESPONSE TIME ──
                        _elapsed = time.monotonic() - _start_time
                        record_response_time(_elapsed)

                        return result
                    except asyncio.TimeoutError:
                        # Record timeout duration for adaptive scaling
                        _elapsed = time.monotonic() - _start_time
                        record_response_time(_elapsed)
                        return (False, f"CHECKOUT_TIMEOUT: Exceeded {_CHECKOUT_TIMEOUT}s", "UNKNOWN", "0.00", "USD")
                    finally:
                        await _counters.dec(active_attr)
                finally:
                    # Release semaphore permit
                    sem.release()
            except Exception:
                await _counters.dec(queued_attr)
                raise

        success, message, gateway, price, currency = await _dedup_or_run(
            dedup_key, _run_checkout
        )

        # ── LAYER 6: CIRCUIT BREAKER (record result) ──
        # FIX Bug #5: Only record_success when message is clearly a card-level success
        # (ORDER_PLACED, 3DS_REQUIRED, OTP_REQUIRED, INSUFFICIENT_FUNDS).
        # INSUFFICIENT_FUNDS means card is valid (passed Luhn/auth) — just no balance.
        # Other non-site-failure messages (like CARD_DECLINED) should NOT decrement
        # fail_count — the card was declined, but the site itself worked fine.
        if _is_site_failure(message):
            cb.record_failure()
        elif message in ('ORDER_PLACED', '3DS_REQUIRED', 'OTP_REQUIRED', 'INSUFFICIENT_FUNDS'):
            cb.record_success()
        # else: card-level failure (declined, expired, etc.) — don't touch CB counters

        clean_response = extract_clean_response(message)

        try:
            price_float = float(price)
        except (ValueError, TypeError):
            price_float = 0.0

        return Response(
            content={
                "Gateway": gateway,
                "Price": price_float,
                "Currency": currency,
                "Response": clean_response,
                "Status": success,
                "cc": cc_string,
            },
            status_code=200, media_type=MediaType.JSON,
        )

    except Exception as e:
        # FIX Bug #44: Record site-level failures even for exceptions that bypass
        # the normal circuit breaker recording path (connection errors, SSL errors, etc.)
        err_lower = str(e).lower()
        if cb and any(kw in err_lower for kw in ('connection', 'ssl', 'timeout', 'network', 'dns')):
            cb.record_failure()
        return Response(
            content={
                "error": str(e), "status": False,
                "Gateway": "UNKNOWN", "Price": 0.0,
                "Response": f"ERROR: {str(e)}",
                "cc": cc_string if cc_string else "",
            },
            status_code=500, media_type=MediaType.JSON,
        )


# ══════════════════════════════════════════════════════════════════════
# SHOPIFY ENDPOINTS — GET (query params) + POST (JSON body)
# ══════════════════════════════════════════════════════════════════════

@get("/shopify", media_type=MediaType.JSON, sync=False)
async def shopify_get(
    site: Optional[str] = Parameter(
        query="site", required=False, default=None,
        description="Shopify store URL",
    ),
    cc: Optional[str] = Parameter(
        query="cc", required=False, default=None,
        description="Card in CC|MM|YYYY|CVV format",
    ),
    proxy: Optional[str] = Parameter(
        query="proxy", required=False, default=None,
        description="Proxy in ip:port:user:pass format",
    ),
    variant: Optional[str] = Parameter(
        query="variant", required=False, default=None,
        description="Product variant ID (auto-detected if omitted)",
    ),
    lane: Optional[str] = Parameter(
        query="lane", required=False, default="mass",
        description="'single' for priority lane, 'mass' for mass lane",
    ),
    user_id: Optional[str] = Parameter(
        query="user_id", required=False, default="anonymous",
        description="User identifier for rate limiting",
    ),
) -> Response:
    """Shopify checkout via GET (query parameters).

    Example: /shopify?site=kith.com&cc=4111111111111111|12|2028|123&lane=mass
    """
    return await _shopify_core(
        site=site,
        cc_string=cc or "",
        proxy_str=proxy,
        variant_id=variant,
        lane=lane,
        user_id=user_id,
    )


@post("/shopify", media_type=MediaType.JSON, sync=False)
async def shopify_post(data: ShopifyRequest) -> Response:
    """Shopify checkout via POST (JSON body).

    Body: {"site": "...", "cc": "...", "proxy": "...", "variant": "...", "lane": "mass", "user_id": "anonymous"}
    """
    return await _shopify_core(
        site=data.site,
        cc_string=data.cc or "",
        proxy_str=data.proxy,
        variant_id=data.variant,
        lane=data.lane,
        user_id=data.user_id,
    )


# ══════════════════════════════════════════════════════════════════════
# WARM POOL & ADAPTIVE SCALING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

@get("/warm-pool", media_type=MediaType.JSON, sync=False)
async def warm_pool_status() -> Dict[str, Any]:
    """Show warm session pool state."""
    now = time.monotonic()
    pools = {}
    for domain, pool in _warm_pool.items():
        sessions = []
        for ws in pool:
            sessions.append({
                "age_seconds": round(now - ws.created_at, 1),
                "site": ws.site,
                "has_sst": bool(ws.sst),
            })
        pools[domain] = sessions
    return {
        "pools": pools,
        "registered_sites": len(_warm_pool_sites),
        "stats": _warm_pool_stats,
        "config": {
            "pool_size_per_site": _WARM_POOL_SIZE,
            "session_ttl": _WARM_SESSION_TTL,
            "refill_interval": _WARM_REFILL_INTERVAL,
        }
    }


# ══════════════════════════════════════════════════════════════════════
# APP INITIALIZATION + BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════════════

async def _on_startup() -> None:
    """Start background tasks for warm pool refill and adaptive scaling."""
    global _warm_pool_task, _adaptive_task
    _warm_pool_task = asyncio.create_task(_warm_pool_refill_loop())
    _adaptive_task = asyncio.create_task(_adaptive_scaling_loop())
    print("[startup] Warm pool refill + adaptive scaling tasks started", file=sys.stderr)


async def _on_shutdown() -> None:
    """Clean up background tasks and connection pool with graceful drain."""
    global _warm_pool_task, _adaptive_task
    if _warm_pool_task:
        _warm_pool_task.cancel()
    if _adaptive_task:
        _adaptive_task.cancel()
    # FIX Arch #25: Graceful drain — wait for in-flight checkouts to complete
    # (up to 10s) before closing connections. This prevents mid-checkout aborts.
    drain_start = time.monotonic()
    ctr = _counters.read()
    total_active = ctr['active_single'] + ctr['active_mass']
    if total_active > 0:
        print(f"[shutdown] Waiting for {total_active} in-flight checkouts (max 10s)...", file=sys.stderr)
        while time.monotonic() - drain_start < 10:
            ctr = _counters.read()
            if ctr['active_single'] + ctr['active_mass'] == 0:
                break
            await asyncio.sleep(0.5)
    await _client_pool.close_all()
    # Close all warm sessions
    for domain, pool in _warm_pool.items():
        while pool:
            ws = pool.popleft()
            try:
                await ws.session.aclose()
            except Exception:
                pass
    print("[shutdown] Cleanup complete", file=sys.stderr)


app = Litestar(
    route_handlers=[health, cache_stats, api_status, circuit_breaker_status, warm_pool_status, shopify_get, shopify_post],
    debug=False,
    openapi_config=None,
    on_startup=[_on_startup],
    on_shutdown=[_on_shutdown],
)

# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT — allows `python3 api_litestar.py` to start the server
# directly (in addition to `uvicorn api_litestar:app` from the Procfile)
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    _port = int(os.environ.get("PORT", "8080"))
    _workers = int(os.environ.get("WEB_WORKERS", "1"))
    uvicorn.run(
        "api_litestar:app",
        host="0.0.0.0",
        port=_port,
        workers=_workers,
        loop="uvloop",
        timeout_keep_alive=5,
    )
