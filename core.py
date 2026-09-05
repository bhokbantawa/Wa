# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shopify Checkout Core — Request-Based Rewrite (Pure requests/httpx)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Converted from async tls_requests to synchronous requests + httpx.
# Full 12-step Shopify checkout flow preserved from original core.py.
#
# FLOW:
#   1. GET /products.json         → cheapest physical product
#   2. GET homepage               → Storefront accessToken
#   3. POST /api/unstable/graphql → cartCreate → checkoutUrl
#   4. GET checkoutUrl            → sessionToken, sourceToken, etc.
#   5-8. POST /checkouts/unstable/graphql → Negotiate rounds
#   9. POST checkout.pci.shopifyinc.com  → tokenize card
#  10. POST /checkouts/unstable/graphql  → payment proposal
#  11. POST /checkouts/unstable/graphql  → submitForCompletion
#  12. POST /checkouts/unstable/graphql  → PollForReceipt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import base64
import json
import os
import re
import html as _html
import random
import sys
import time
import uuid
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
try:
    from tls_requests import Client as TLSClient, TLSIdentifierRotator
    _TLS_AVAILABLE = True
    _TLS_IDENTIFIER_POOL = [
        'chrome_131', 'chrome_133', 'chrome_124', 'chrome_120',
        'chrome_117', 'chrome_112',
    ]
    _tls_rotator = TLSIdentifierRotator(items=_TLS_IDENTIFIER_POOL, strategy='random')
    def _pick_tls_identifier():
        return _tls_rotator.next()
except ImportError:
    _TLS_AVAILABLE = False
    def _pick_tls_identifier():
        return 'chrome_120'
try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

# =====================================================================
# BROWSER / CLIENT-HINTS POOL
# =====================================================================
_BROWSER_PROFILES = [
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Google Chrome";v="133", "Chromium";v="133", "Not/A)Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="133.0.0.0", "Chromium";v="133.0.0.0", "Not/A)Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Google Chrome";v="131", "Chromium";v="131", "Not/A)Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="131.0.0.0", "Chromium";v="131.0.0.0", "Not/A)Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="124.0.0.0", "Chromium";v="124.0.0.0", "Not_A Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'sec_ch_ua': '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="120.0.0.0", "Chromium";v="120.0.0.0", "Not_A Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
]

# Phone numbers pool for delivery — Shopify requires a valid phone for some stores
_PHONE_POOL = [
    '+12025551234', '+13105557890', '+16175553456', '+17185551122',
    '+14155559876', '+12125554321', '+13305552468', '+18135551357',
]

def _pick_phone():
    return random.choice(_PHONE_POOL)


def _pick_profile():
    p = dict(random.choice(_BROWSER_PROFILES))
    p['phone'] = _pick_phone()
    return p


# =====================================================================
# PROXY HELPERS
# =====================================================================
def parse_proxy(proxy_str: str) -> dict | None:
    """Normalize proxy string → requests proxy dict."""
    if not proxy_str:
        return None
    proxy_str = proxy_str.strip()
    if proxy_str.startswith(('http://', 'https://', 'socks5://', 'socks4://')):
        url = proxy_str
    elif '@' in proxy_str:
        url = f'http://{proxy_str}'
    else:
        parts = proxy_str.split(':')
        if len(parts) == 4:
            host, port, user, pwd = parts
            url = f'http://{user}:{pwd}@{host}:{port}'
        elif len(parts) == 2:
            url = f'http://{proxy_str}'
        else:
            url = proxy_str
    return {'http': url, 'https': url}


# =====================================================================
# SESSION FACTORY (requests.Session with retry + optional proxy)
# =====================================================================
def make_session(proxy_str: str | None = None, retries: int = 3):
    """Create a TLS-fingerprinted session using tls_requests (Chrome JA3/H2),
    with curl_cffi fallback, then plain requests. Proper TLS avoids ARTIFACT_DISSATISFACTION."""
    proxy = None
    if proxy_str:
        _p = parse_proxy(proxy_str)
        if _p:
            proxy = _p.get('https') or _p.get('http')
    if _TLS_AVAILABLE:
        identifier = _pick_tls_identifier()
        session = TLSClient(
            client_identifier=identifier,
            http2=True,
            verify=True,
            timeout=30,
            follow_redirects=True,
            proxy=proxy,
        )
        return session
    if _CURL_CFFI_AVAILABLE:
        proxies = {'https': proxy, 'http': proxy} if proxy else {}
        session = curl_requests.Session(impersonate='chrome124', proxies=proxies)
        return session
    # Plain requests fallback
    session = requests.Session()
    retry = Retry(total=retries, backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET', 'POST'], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter); session.mount('https://', adapter)
    if proxy_str:
        proxies = parse_proxy(proxy_str)
        if proxies: session.proxies.update(proxies)
    return session


# =====================================================================
# DELAY UTILITIES
# =====================================================================
DELAY_SCALE = float(os.environ.get('DELAY_SCALE', '0.25'))


def human_delay(min_sec: float = 0.8, max_sec: float = 2.5):
    if DELAY_SCALE <= 0:
        return
    scaled_min = min_sec * DELAY_SCALE
    scaled_max = max_sec * DELAY_SCALE
    delay = random.triangular(scaled_min, scaled_max, (scaled_min + scaled_max) / 2.5)
    if random.random() < 0.05:
        delay += random.uniform(0.3, 1.0) * DELAY_SCALE
    time.sleep(delay)


def retry_on_429(fn, step_name="request", max_retries=3, base_delay=3.0, max_delay=15.0):
    """Call fn() and retry on HTTP 429 with exponential backoff."""
    for attempt in range(max_retries + 1):
        response = fn()
        if response.status_code != 429:
            return response
        if attempt == max_retries:
            return response
        backoff = min(base_delay * (2 ** attempt), max_delay)
        delay = backoff * random.uniform(0.5, 1.5)
        print(f"[rate-limit] {step_name} HTTP 429, retry {attempt+1}/{max_retries} in {delay:.1f}s", file=sys.stderr)
        time.sleep(delay)
    return response


# =====================================================================
# STRING / HTML EXTRACTION HELPERS
# =====================================================================
def extract_between(text: str, start: str, end: str) -> str:
    if not text:
        return ''
    idx = text.find(start)
    if idx == -1:
        return ''
    idx += len(start)
    end_idx = text.find(end, idx)
    if end_idx == -1:
        return ''
    return text[idx:end_idx]


def extract_storefront_token(text: str) -> str:
    """Extract Shopify Storefront API access token from page HTML.
    Handles classic themes, Hydrogen/headless, encoded variants, single/double quotes.
    """
    import html as _html_mod, re as _re_mod
    if not text:
        return ''

    # ── Direct string search (fastest, handles 95% of stores) ──────────
    # Each entry: (search_prefix, end_char)
    _str_patterns = [
        # Classic theme meta tags (most reliable)
        ('name="shopify-storefront-api-token" content="', '"'),
        ("name='shopify-storefront-api-token' content='", "'"),
        ('name="serialized-storefront-api-token" content="', '"'),
        # JSON/JS double-quote patterns
        ('"accessToken":"', '"'),
        ('"storefrontApiToken":"', '"'),
        ('"storefrontAccessToken":"', '"'),
        ('"PUBLIC_STOREFRONT_API_TOKEN":"', '"'),
        # Single-quote JS patterns
        ("'accessToken':'", "'"),
        ("'storefrontApiToken':'", "'"),
        ("'storefrontAccessToken':'", "'"),
        # Mixed (key double, value single or unquoted)
        ('"accessToken":', None),   # handled below
        # Hydrogen/Remix env blob
        ('"PUBLIC_STOREFRONT_API_TOKEN","', '"'),
        # Encoded HTML entities
        ('&quot;accessToken&quot;:&quot;', '&quot;'),
        ('&quot;storefrontApiToken&quot;:&quot;', '&quot;'),
        # JS variable assignments
        ('storefrontAccessToken = "', '"'),
        ("storefrontAccessToken = '", "'"),
    ]

    for start_pat, end_char in _str_patterns:
        idx = text.find(start_pat)
        if idx < 0:
            continue
        val_start = idx + len(start_pat)
        if end_char is None:
            # Handle '"accessToken": VALUE' where value may start with " or '
            rest = text[val_start:val_start+50].lstrip(' :\t')
            if rest.startswith(('"', "\'")):
                quote_char = rest[0]
                rest = rest[1:]
                end = rest.find(quote_char)
                if end > 0:
                    val = rest[:end].strip()
                    if val and len(val) >= 16:
                        return _html_mod.unescape(val)
            continue
        end_idx = text.find(end_char, val_start)
        if end_idx < 0:
            continue
        val = text[val_start:end_idx].strip()
        val = _html_mod.unescape(val).strip('"\' ').strip()
        if val and len(val) >= 16:
            return val

    # ── Regex fallback (handles edge cases) ─────────────────────────────
    _regex_patterns = [
        # Standard keys with any quote style
        r'["\'\`](?:accessToken|storefrontApiToken|storefrontAccessToken|PUBLIC_STOREFRONT_API_TOKEN)["\'\`]\s*[=:]\s*["\'\`]([a-zA-Z0-9_\-]{20,})["\'\`]',
        # window.__st blob
        r'window\.__st\s*=\s*\{[^}]*"accessToken"\s*:\s*"([a-f0-9]{24,})"',
        # Shopify.storefront blob
        r'Shopify\.storefront\s*=\s*\{[^}]*"accessToken"\s*:\s*"([a-f0-9]{24,})"',
        # Any 32-char hex after common field names
        r'(?:accessToken|storefrontToken|sfToken)["\'\`\s:=]+([a-f0-9]{32})\b',
    ]
    for pattern in _regex_patterns:
        try:
            m = _re_mod.search(pattern, text, _re_mod.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val and len(val) >= 16:
                    return val
        except Exception:
            continue

    return ''

# =====================================================================
# GRAPHQL QUERY/MUTATION CONSTANTS
# =====================================================================
MUTATION_CART_CREATE = """mutation cartCreate($input:CartInput!){result:cartCreate(input:$input){cart{id checkoutUrl cost{subtotalAmount{amount currencyCode}totalAmount{amount currencyCode}}lines(first:10){edges{node{quantity merchandise{...on ProductVariant{id title requiresShipping product{id title}priceV2{amount currencyCode}}}}}}}errors:userErrors{message field code}}}"""
MUTATION_CART_BUYER_IDENTITY_UPDATE = """mutation cartBuyerIdentityUpdate($cartId:ID!,$buyerIdentity:CartBuyerIdentityInput!){cartBuyerIdentityUpdate(cartId:$cartId,buyerIdentity:$buyerIdentity){cart{id checkoutUrl}userErrors{message field code}}}"""

QUERY_PROPOSAL = """
query Proposal($input:SessionNegotiationInput!){session{negotiate(input:$input){errors{code localizedMessage}result{__typename ...on NegotiationResultAvailable{queueToken sessionToken sellerProposal{__typename checkoutTotal{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}...on MoneyIntervalConstraint{lowerBound{amount currencyCode}upperBound{amount currencyCode}}}isShippingRequired delivery{__typename ...on PendingTerms{pollDelay taskId}...on FilledDeliveryTerms{deliveryLines{__typename deliveryMethodTypes stableId selectedDeliveryStrategy{__typename ...on CompleteDeliveryStrategy{handle code title amount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}}...on CustomDeliveryStrategy{code title price{__typename ...on MoneyValueConstraint{value{amount currencyCode}}}}...on DeliveryStrategyReference{handle}}totalAmount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}destinationAddress{__typename ...on StreetAddress{address1 address2 city countryCode zoneCode postalCode}...on PartialStreetAddress{address1 city countryCode zoneCode postalCode}}targetMerchandise{__typename ...on AnyMerchandiseLineTargetCollection{any}...on FilledMerchandiseLineTargetCollection{linesV2{__typename ...on MerchandiseLine{stableId}}}}}}...on UnavailableTerms{__typename}}merchandise{__typename ...on FilledMerchandiseTerms{merchandiseLines{stableId merchandise{__typename ...on ProductVariantMerchandise{variantId title digest product{title id}}...on ContextualizedProductVariantMerchandise{variantId title digest price{amount currencyCode}product{title id}}...on SourceProvidedMerchandise{variantId title digest price{amount currencyCode}requiresShipping taxable giftCard}}}}}payment{__typename ...on FilledPaymentTerms{availablePaymentLines{paymentMethod{__typename ...on PaymentProvider{paymentMethodIdentifier name brands}}}}}}buyerProposal{__typename checkoutTotal{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}}}...on NegotiationResultFailed{failureCode}...on SubmittedForCompletion{receipt{__typename ...on ProcessingReceipt{id __typename}...on ProcessedReceipt{id order{id __typename}}...on FailedReceipt{id processingError{__typename}}...on ActionRequiredReceipt{id __typename}...on WaitingReceipt{id __typename}}}...on CheckpointDenied{__typename}...on Throttled{__typename}...on TooManyRequests{__typename}}}}}
"""

MUTATION_SUBMIT = (
    "mutation SubmitForCompletion($input:NegotiationInput!$attemptToken:String!)"
    "{submitForCompletion(input:$input attemptToken:$attemptToken)"
    "{__typename"
    "...on SubmitSuccess{receipt{__typename"
    "...on ProcessingReceipt{id pollDelay __typename}"
    "...on ProcessedReceipt{id order{id __typename} orderStatusPageUrl}"
    "...on FailedReceipt{id processingError{__typename ...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on InventoryClaimFailure{__typename}}}"
    "...on ActionRequiredReceipt{id __typename}"
    "...on WaitingReceipt{id pollDelay __typename}"
    "}configurationRecordId}"
    "...on SubmittedForCompletion{receipt{__typename"
    "...on ProcessingReceipt{id pollDelay __typename}"
    "...on ProcessedReceipt{id order{id __typename} orderStatusPageUrl}"
    "...on FailedReceipt{id processingError{__typename ...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on InventoryClaimFailure{__typename}}}"
    "...on ActionRequiredReceipt{id __typename}"
    "...on WaitingReceipt{id pollDelay __typename}"
    "}configurationRecordId}"
    "...on SubmitFailed{reason}"
    "...on SubmitRejected{__typename errors{code localizedMessage}sellerProposal{__typename checkoutTotal{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}delivery{__typename ...on FilledDeliveryTerms{deliveryLines{__typename deliveryMethodTypes stableId selectedDeliveryStrategy{__typename ...on CompleteDeliveryStrategy{handle code title amount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}}...on CustomDeliveryStrategy{code title price{__typename ...on MoneyValueConstraint{value{amount currencyCode}}}}...on DeliveryStrategyReference{handle}}totalAmount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}destinationAddress{__typename ...on StreetAddress{address1 address2 city countryCode zoneCode postalCode}}targetMerchandise{__typename ...on AnyMerchandiseLineTargetCollection{any}...on FilledMerchandiseLineTargetCollection{linesV2{__typename ...on MerchandiseLine{stableId}}}}}}}}}"
    "...on CheckpointDenied{redirectUrl __typename}"
    "...on Throttled{pollAfter queueToken __typename}"
    "...on TooManyAttempts{__typename}"
    "...on TooManyRequests{retryAfter __typename}"
    "...on SubmitAlreadyAccepted{receipt{__typename ...on ProcessingReceipt{id __typename}...on ProcessedReceipt{id order{id __typename} orderStatusPageUrl}...on FailedReceipt{id processingError{__typename ...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on InventoryClaimFailure{__typename}}}...on ActionRequiredReceipt{id __typename}...on WaitingReceipt{id __typename}}__typename}}"
    "}"
)

QUERY_POLL = """query PollForReceipt{receipt{__typename ...on ProcessingReceipt{id pollDelay __typename}...on ProcessedReceipt{id order{id __typename} orderStatusPageUrl}...on FailedReceipt{id processingError{__typename ...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on InventoryClaimFailure{__typename}}}...on ActionRequiredReceipt{id __typename}...on WaitingReceipt{id pollDelay __typename}...on ReceiptNotFound{__typename}}}"""


# =====================================================================
# CARD PARSING
# =====================================================================
def parse_cc_string(cc_string: str) -> tuple[str, str, str, str]:
    """Parse CC|MM|YYYY|CVV or CC|MM|YY|CVV format."""
    cc_string = cc_string.strip()
    delimiters = ['|', ':', '/', ' ']
    parts = None
    for d in delimiters:
        if d in cc_string:
            parts = cc_string.split(d)
            break
    if not parts or len(parts) < 4:
        raise ValueError(f"Invalid CC format. Expected CC|MM|YYYY|CVV, got: {cc_string}")
    cc, month, year, cvv = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
    if len(year) == 2:
        year = f"20{year}"
    return cc, month, year, cvv


# =====================================================================
# DEEP DICT HELPERS
# =====================================================================
def _dget(data, *keys, default=None):
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


def _extract_money(obj: dict) -> tuple[str, str]:
    """Extract (amount, currencyCode) from MoneyConstraint or Money union."""
    if not obj or not isinstance(obj, dict):
        return '0', 'USD'
    value = obj.get('value')
    if isinstance(value, dict):
        return str(value.get('amount', '0')), str(value.get('currencyCode', 'USD'))
    if 'amount' in obj:
        return str(obj['amount']), str(obj.get('currencyCode', 'USD'))
    return '0', 'USD'


# =====================================================================
# PAYMENT ERROR HELPERS
# =====================================================================
_GENERIC_PAYMENT_CODES = {
    'PAYMENT', 'ERROR', 'FAILED', 'FAILURE', 'UNKNOWN', 'CODE', 'MESSAGE',
    'STATUS', 'RESULT', 'CHECKOUT', 'SHOPIFY', 'CARD', 'PROCESSING',
}


def _is_generic_payment_code(code: str) -> bool:
    return code.upper() in _GENERIC_PAYMENT_CODES


def _extract_payment_error_response(error: dict) -> str:
    """Extract human-readable error code from PaymentFailed processingError."""
    if not isinstance(error, dict):
        return ''
    for field in ('messageUntranslated', 'code', 'message'):
        val = error.get(field)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    generic_code = ''
    for field in ('code', 'errorCode', 'declineCode'):
        val = error.get(field)
        if val and isinstance(val, str) and val.strip():
            generic_code = val.strip()
            break
    message = error.get('messageUntranslated') or error.get('message') or ''
    return generic_code or message or 'UNKNOWN_PAYMENT_ERROR'


def extract_clean_response(message: str) -> str:
    """Clean and normalize a response message to a code string."""
    if not message:
        return "UNKNOWN_ERROR"
    message = str(message)
    message = re.sub(r'<[^>]+>', '', message).strip()
    if not message:
        return "UNKNOWN_ERROR"

    DIAGNOSTIC_PREFIXES = [
        'PROPOSAL_BLOCKED:', 'PROPOSAL_EMPTY:', 'PROPOSAL_JSON_ERROR:',
        'SUBMIT_BLOCKED:', 'SUBMIT_JSON_ERROR:', 'PCI_VAULT_BLOCKED:',
        'PCI_VAULT_ERROR:', 'BLOCKED:', 'POLL_BLOCKED:', 'POLL_JSON_ERROR:',
        'POLL_EMPTY:', 'SESSION_TOKEN_MISSING:', 'CHECKOUT_PAGE_FAILED:',
        'DELIVERY_PENDING_TIMEOUT:', 'NO_SHIPPING_STRATEGY:',
        'INVENTORY_CLAIM_FAILURE:', 'RECEIPT_NOT_FOUND:',
    ]
    for prefix in DIAGNOSTIC_PREFIXES:
        if message.startswith(prefix):
            return message[:200]

    # IP-block / datacenter codes → always return PROCESSING_ERROR
    _IP_BLOCK_CODES = {
        'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'ARTIFACT_DISSATISFACTION',
        'WAITING_PENDING_TERMS',
    }
    if message.strip().upper() in _IP_BLOCK_CODES:
        return 'PROCESSING_ERROR'
    # Normalize compound responses that ONLY contain IP-block codes
    if all(c.strip() in _IP_BLOCK_CODES for c in message.split(',') if c.strip()):
        return 'PROCESSING_ERROR'

    _KNOWN_CODES = {
        'CARD_DECLINED', 'INSUFFICIENT_FUNDS', 'EXPIRED_CARD', 'INVALID_CVC',
        'INCORRECT_NUMBER', 'INCORRECT_CVC', 'INCORRECT_ZIP', 'INCORRECT_ADDRESS',
        'PROCESSING_ERROR', 'CALL_ISSUER', 'PICK_UP_CARD', 'DO_NOT_HONOR',
        'CARD_NOT_SUPPORTED', 'TRY_AGAIN_LATER', 'INVALID_ACCOUNT',
        'INVALID_AMOUNT', 'INVALID_NUMBER', 'ALREADY_REFUNDED',
        'AUTHENTICATION_REQUIRED', 'TEST_MODE_LIVE_CARD',
        '3DS_REQUIRED', 'OTP_REQUIRED', 'ORDER_PLACED',
        'INVENTORY_CLAIM_FAILURE', 'RECEIPT_NOT_FOUND',
        'CAPTCHA_REQUIRED', 'GENERIC_ERROR', 'PAYMENT_FAILED',
        'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT', 'TAX_MISMATCH',
        'TAX_NEW_TAX_MUST_BE_ACCEPTED', 'DESTINATION_ADDRESS_REQUIRED',
        'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED', 'MERCHANDISE_SIGNATURE_MISMATCH',
        'MERCHANDISE_CART_UPDATED_BASED_ON_COUNTRY',
        'PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH',
        'PAYMENTS_PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH',
        'PAYMENTS_PHONE_NUMBER_DOES_NOT_MATCH_EXPECTED_PATTERN',
        'DESTINATION_ADDRESS_VALIDATION_FAILED',
        'CHECKOUT_ALREADY_COMPLETED',
        'MERCHANDISE_LINE_LIMIT_REACHED',
    }
    if message.strip().upper() in _KNOWN_CODES:
        return message.strip()

    patterns = [
        r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)',
        r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'([A-Z]+_[A-Z_]+)',
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, message, re.IGNORECASE):
            if isinstance(match, tuple):
                match = match[0]
            if match and "_" in match and len(match) < 50:
                match = match.strip("{}:'\" ")
                if match.upper() not in _GENERIC_PAYMENT_CODES:
                    return match

    words = message.split()
    if words:
        first_word = words[0]
        if "_" in first_word and first_word.isupper():
            if not _is_generic_payment_code(first_word) or len(words) <= 1:
                return first_word

    return message[:200]


# =====================================================================
# INPUT BUILDERS
# =====================================================================
def _build_merchandise_line(variant_id, product_id, price, currency, title, requires_shipping, quantity=1, digest=None):
    return {
        'merchandise': {
            'productVariantReference': {
                'id': f'gid://shopify/ProductVariantMerchandise/{variant_id}',
                'variantId': f'gid://shopify/ProductVariant/{variant_id}',
                'properties': [],
                'sellingPlanId': None,
                'sellingPlanDigest': None,
            },
        },
        'quantity': {'items': {'value': quantity}},
        'expectedTotalPrice': {
            'value': {'amount': f'{float(price) * quantity:.2f}', 'currencyCode': currency},
        },
        'lineComponentsSource': None,
        'lineComponents': [],
    }


def _build_delivery_line(currency, first_name, last_name, street, city, country_code, zone_code, postal_code, phone='', shipping_handle='shipping', shipping_amount=None):
    delivery_line = {
        'deliveryMethodTypes': ['SHIPPING'],
        'selectedDeliveryStrategy': {
            'deliveryStrategyByHandle': {
                'handle': shipping_handle,
                'customDeliveryRate': False,
            },
        },
        'targetMerchandiseLines': {'any': True},
        'destination': {
            'streetAddress': {
                'firstName': first_name,
                'lastName': last_name,
                'address1': street,
                'address2': '',
                'city': city,
                'countryCode': country_code,
                'zoneCode': zone_code,
                'postalCode': postal_code,
                **({'phone': phone} if phone else {}),
            },
        },
    }
    if shipping_amount is not None:
        delivery_line['expectedTotalPrice'] = {
            'value': {'amount': f'{float(shipping_amount):.2f}', 'currencyCode': currency},
        }
    else:
        delivery_line['expectedTotalPrice'] = {'any': True}
    return delivery_line


def _build_delivery_terms(delivery_lines: list, no_delivery_required: list = None) -> dict:
    return {
        'deliveryLines': delivery_lines,
        'noDeliveryRequired': no_delivery_required if no_delivery_required is not None else [],
    }


def _build_payment_line(total_amount: str, currency: str, cc: str, month: str, year: str, cvv: str, payment_token: str, payment_method_identifier: str = 'credit_card') -> dict:
    # Force to credit_card for BNPL/installment stores (prevents PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH)
    if payment_method_identifier not in ('credit_card', 'debit_card'):
        payment_method_identifier = 'credit_card'
    return {
        'paymentMethod': {
            'directPaymentMethod': {
                'paymentMethodIdentifier': payment_method_identifier,
                'sessionId': payment_token,
                'billingAddress': {
                    'streetAddress': {
                        'firstName': 'John',
                        'lastName': 'Doe',
                        'address1': '123 Main St',
                        'address2': '',
                        'city': 'New York',
                        'countryCode': 'US',
                        'zoneCode': 'NY',
                        'postalCode': '10001',
                    },
                },
            },
        },
        'amount': {'value': {'amount': total_amount, 'currencyCode': currency}},
    }


# =====================================================================
# NEGOTIATE RESPONSE PARSER
# =====================================================================
def _parse_negotiate_response(resp_json: dict) -> dict:
    """Parse a Shopify negotiate response into a flat result dict."""
    result = {
        'result_type': '',
        'queue_token': '',
        'session_token': '',
        'checkout_total': '0',
        'checkout_total_currency': 'USD',
        'tax_total': '0',
        'is_shipping_required': True,
        'delivery_resolved': True,
        'delivery_task_id': None,
        'delivery_poll_delay': 500,
        'shipping_strategies': [],
        'server_delivery_lines': [],
        'stable_ids': [],
        'variant_id': '',
        'product_title': '',
        'product_price': '0',
        'product_currency': 'USD',
        'seller_digest': '',
        'gateway_name': '',
        'payment_method_identifier': '',
        'errors': [],
        'failureCode': '',
    }

    negotiate = _dget(resp_json, 'data', 'session', 'negotiate') or {}
    errors_list = negotiate.get('errors') or []
    result['errors'] = errors_list

    res = negotiate.get('result') or {}
    typename = res.get('__typename', '')
    result['result_type'] = typename

    if typename == 'NegotiationResultFailed':
        result['failureCode'] = res.get('failureCode', 'UNKNOWN_FAILURE')
        return result

    if typename not in ('NegotiationResultAvailable', 'SubmittedForCompletion'):
        return result

    result['queue_token'] = res.get('queueToken', '')
    result['session_token'] = res.get('sessionToken', '')

    seller = res.get('sellerProposal') or {}

    # Checkout total
    ct = seller.get('checkoutTotal') or {}
    ct_amount, ct_currency = _extract_money(ct)
    if ct_amount and ct_amount != '0':
        result['checkout_total'] = ct_amount
        result['checkout_total_currency'] = ct_currency

    result['is_shipping_required'] = seller.get('isShippingRequired', True)

    # Delivery
    delivery_obj = seller.get('delivery') or {}
    delivery_typename = delivery_obj.get('__typename', '')
    if delivery_typename == 'FilledDeliveryTerms':
        result['delivery_resolved'] = True
        delivery_lines = delivery_obj.get('deliveryLines') or []
        strategies = []
        server_delivery_lines = []
        for dl in delivery_lines:
            methods = dl.get('deliveryMethodTypes') or []
            dl_stable_id = dl.get('stableId', '')
            sel_strategy = dl.get('selectedDeliveryStrategy') or {}
            strat_typename = sel_strategy.get('__typename', '')

            server_handle = ''
            server_strategy_code = ''
            server_strategy_amount = None
            server_strategy_currency = None

            if strat_typename == 'CompleteDeliveryStrategy':
                server_handle = sel_strategy.get('handle', '')
                server_strategy_code = sel_strategy.get('code', '')
                _amt = sel_strategy.get('amount') or {}
                if _amt.get('__typename') == 'MoneyValueConstraint':
                    v = _amt.get('value') or {}
                    server_strategy_amount = v.get('amount')
                    server_strategy_currency = v.get('currencyCode')
            elif strat_typename == 'CustomDeliveryStrategy':
                server_strategy_code = sel_strategy.get('code', '')
                _price = sel_strategy.get('price') or {}
                if _price.get('__typename') == 'MoneyValueConstraint':
                    v = _price.get('value') or {}
                    server_strategy_amount = v.get('amount')
                    server_strategy_currency = v.get('currencyCode')
            elif strat_typename == 'DeliveryStrategyReference':
                server_handle = sel_strategy.get('handle', '')

            total_obj = dl.get('totalAmount') or {}
            total_typename = total_obj.get('__typename', '')
            server_total_amount, server_total_currency = _extract_money(total_obj)

            dest_addr = dl.get('destinationAddress') or {}
            dest_typename = dest_addr.get('__typename', '')

            server_dl = {
                'deliveryMethodTypes': methods or ['SHIPPING'],
                'selectedDeliveryStrategy': {
                    'deliveryStrategyByHandle': {
                        'handle': server_handle or 'shipping',
                        'customDeliveryRate': False,
                    },
                },
            }

            if total_typename == 'AnyConstraint' or total_obj.get('any'):
                server_dl['expectedTotalPrice'] = {'any': True}
            elif server_total_amount and server_total_amount != '0':
                server_dl['expectedTotalPrice'] = {
                    'value': {'amount': server_total_amount, 'currencyCode': server_total_currency or 'USD'},
                }
            else:
                server_dl['expectedTotalPrice'] = {'any': True}

            server_dl['targetMerchandiseLines'] = {'any': True}

            if dest_addr.get('address1') or dest_addr.get('city'):
                server_dl['destination'] = {
                    'streetAddress': {
                        'firstName': '',
                        'lastName': '',
                        'address1': dest_addr.get('address1', ''),
                        'address2': dest_addr.get('address2', ''),
                        'city': dest_addr.get('city', ''),
                        'countryCode': dest_addr.get('countryCode', ''),
                        'zoneCode': dest_addr.get('zoneCode', ''),
                        'postalCode': dest_addr.get('postalCode', ''),
                    },
                }

            server_delivery_lines.append(server_dl)
            strategies.append({
                'code': server_strategy_code or (methods[0] if methods else 'SHIPPING'),
                'handle': server_handle or 'shipping',
                'name': server_strategy_code or (methods[0] if methods else 'SHIPPING'),
                'server_price': server_strategy_amount or server_total_amount,
                'server_price_currency': server_strategy_currency or server_total_currency,
            })

        result['shipping_strategies'] = strategies
        result['server_delivery_lines'] = server_delivery_lines

    elif delivery_typename == 'PendingTerms':
        result['delivery_resolved'] = False
        result['delivery_task_id'] = delivery_obj.get('taskId')
        result['delivery_poll_delay'] = delivery_obj.get('pollDelay', 500)

    # Merchandise
    merch_obj = seller.get('merchandise') or {}
    merch_typename = merch_obj.get('__typename', '')
    if merch_typename == 'FilledMerchandiseTerms':
        merch_lines = merch_obj.get('merchandiseLines') or []
        for ml in merch_lines:
            sid = ml.get('stableId', '')
            if sid:
                result['stable_ids'].append(sid)
            merch = ml.get('merchandise') or {}
            mt = merch.get('__typename', '')
            result['seller_digest'] = merch.get('digest', '')
            vid = merch.get('variantId', '')
            if vid:
                result['variant_id'] = vid.split('/')[-1] if '/' in vid else vid
            if mt == 'SourceProvidedMerchandise':
                price_obj = merch.get('price') or {}
                if price_obj:
                    result['product_price'] = str(price_obj.get('amount', '0'))
                    result['product_currency'] = price_obj.get('currencyCode', 'USD')
            elif mt in ('ProductVariantMerchandise', 'ContextualizedProductVariantMerchandise'):
                price_obj = merch.get('price') or {}
                if price_obj:
                    result['product_price'] = str(price_obj.get('amount', '0'))
                    result['product_currency'] = price_obj.get('currencyCode', 'USD')
            product_obj = merch.get('product') or {}
            if product_obj.get('title'):
                result['product_title'] = product_obj['title']

    # Payment method identifier
    payment_obj = seller.get('payment') or {}
    if payment_obj.get('__typename') == 'FilledPaymentTerms':
        for apl in (payment_obj.get('availablePaymentLines') or []):
            pm = apl.get('paymentMethod') or {}
            pmi = pm.get('paymentMethodIdentifier', '')
            pm_name = pm.get('name', '').lower()
            if pmi:
                result['payment_method_identifier'] = pmi
            if not result['gateway_name']:
                if 'stripe' in pm_name:
                    result['gateway_name'] = 'Stripe'
                elif 'braintree' in pm_name:
                    result['gateway_name'] = 'Braintree'
                elif 'paypal' in pm_name:
                    result['gateway_name'] = 'PayPal'
                elif 'shopify' in pm_name:
                    result['gateway_name'] = 'Shopify Payments'
                elif pm_name:
                    result['gateway_name'] = pm_name.title()

    return result


# =====================================================================
# PRODUCT FETCHER
# =====================================================================
def fetch_products(site_url: str, session: requests.Session, profile: dict) -> tuple[str, str, float, str, bool, str]:
    """Fetch cheapest physical product from /products.json.
    Returns: (variant_id, product_id, price, currency, requires_shipping, title)
    """
    ourl = site_url.rstrip('/')
    headers = {
        'User-Agent': profile['ua'],
        'Accept': 'application/json',
        'sec-ch-ua': profile['sec_ch_ua'],
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': profile['platform'],
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
    }
    # Paginate up to 3 pages to find available products (some stores have
    # first page fully OOS — e.g. goodfair.com rotates stock frequently)
    products = []
    for _page in range(1, 4):
        _purl = f'{ourl}/products.json?limit=250&sort_by=price-ascending&page={_page}'
        resp = retry_on_429(
            lambda u=_purl: session.get(u, headers=headers, timeout=15),
            step_name="products_json",
        )
        if resp.status_code != 200:
            if _page == 1:
                raise RuntimeError(f"products.json returned HTTP {resp.status_code}")
            break
        try:
            data = resp.json()
        except Exception:
            if _page == 1:
                raise RuntimeError("products.json returned non-JSON")
            break
        _page_products = data.get('products') or []
        if not _page_products:
            break
        products.extend(_page_products)
        # Stop early if we already have an available variant
        if any(v.get('available') for p in products for v in (p.get('variants') or [])):
            break
    best_variant_id = None
    best_product_id = None
    best_price = None
    best_title = None
    best_requires_shipping = True

    for product in products:
        product_id = str(product.get('id', ''))
        for variant in (product.get('variants') or []):
            if not variant.get('available', True):
                continue
            try:
                price = float(variant.get('price', 0))
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue
            variant_id = str(variant.get('id', ''))
            if not variant_id:
                continue
            if best_price is None or price < best_price:
                best_price = price
                best_variant_id = variant_id
                best_product_id = product_id
                best_title = variant.get('title') or product.get('title') or 'Product'
                best_requires_shipping = variant.get('requires_shipping', True)

    if not best_variant_id:
        raise RuntimeError("No purchasable variants found on /products.json")

    return best_variant_id, best_product_id, best_price, 'USD', best_requires_shipping, best_title


# =====================================================================
# CARD TOKENIZER (PCI Vault)
# =====================================================================
def tokenize_card(cc: str, month: str, year: str, cvv: str, session, profile: dict, caller_id_sig: str = '') -> str:
    """POST card data to Shopify PCI vault and return session token.
    caller_id_sig: checkoutCardsinkCallerIdentificationSignature from checkout HTML.
    This binds the payment token to the checkout session — required to avoid ARTIFACT_DISSATISFACTION.
    """
    pci_url = 'https://checkout.pci.shopifyinc.com/sessions'
    _pci_build = 'https://checkout.pci.shopifyinc.com/build/27bcf73'
    headers = {
        'User-Agent': profile['ua'],
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        # Real browser sends referer = the PCI iframe URL (same-origin, no Origin header)
        'Referer': f'{_pci_build}/number-ltr.html?identifier=&locationURL=',
        'sec-ch-ua': profile['sec_ch_ua'],
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': profile['platform'],
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
    }
    # Identification signature — binds this PCI token to the checkout session.
    # Without it Shopify fraud engine sees an unsigned token → ARTIFACT_DISSATISFACTION.
    if caller_id_sig:
        headers['Shopify-Identification-Signature'] = caller_id_sig
    payload = {
        'credit_card': {
            'number': cc,
            'month': int(month),
            'year': int(year),
            'verification_value': cvv,
            'name': 'John Doe',
        },
    }
    resp = retry_on_429(
        lambda: session.post(pci_url, headers=headers, json=payload, timeout=15),
        step_name="pci_vault",
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"PCI vault returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"PCI vault non-JSON: {resp.text[:200]}")
    token = data.get('id') or data.get('token') or data.get('session_id')
    if not token:
        raise RuntimeError(f"PCI vault returned no token: {data}")
    return str(token)


# =====================================================================
# MAIN CHECKOUT FLOW
# =====================================================================
def process_card(
    cc: str,
    month: str,
    year: str,
    cvv: str,
    site_url: str,
    variant_id_override: str | None = None,
    proxy_str: str | None = None,
) -> tuple[bool, str, str, str, str]:
    """
    Run the full Shopify checkout flow for one card.

    Returns: (success: bool, message: str, gateway: str, price: str, currency: str)
    """
    profile = _pick_profile()
    session = make_session(proxy_str)
    ourl = site_url.rstrip('/')
    if not ourl.startswith('http'):
        ourl = 'https://' + ourl
    parsed = urlparse(ourl)
    domain = parsed.netloc

    gateway = 'UNKNOWN'
    total_price = '0.00'
    currency = 'USD'

    # Billing/shipping info (use realistic US test data)
    firstName = 'John'
    lastName = 'Doe'
    street = '1600 Pennsylvania Ave NW'
    city = 'Washington'
    country_code = 'US'
    state = 'DC'
    s_zip = '20500'
    phone = '+12025551234'

    try:
        # ======== STEP 1: Fetch Products ========
        if variant_id_override:
            product_numeric_id = variant_id_override
            variant_id = variant_id_override
            price = 1.0
            product_title = 'Product'
            requires_shipping = True
        else:
            variant_id, product_numeric_id_raw, price, currency, requires_shipping, product_title = fetch_products(
                ourl, session, profile
            )
            product_numeric_id = product_numeric_id_raw

        product_id_for_cart = variant_id

        # Tracking IDs (may be updated from cart response)
        storefront_variant_numeric = variant_id
        storefront_product_numeric = product_numeric_id

        print(f'[STEP1] variant_id={variant_id} price={price} title={product_title}', file=sys.stderr)

        # ======== STEP 2: Storefront Token ========
        product_headers = {
            'User-Agent': profile['ua'],
            'Accept': 'application/json',
            'sec-ch-ua': profile['sec_ch_ua'],
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': profile['platform'],
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
        }

        session_cookies = {}

        def absorb_cookies(resp):
            for k, v in resp.cookies.items():
                if v:
                    session_cookies[k] = v

        home_resp = retry_on_429(
            lambda: session.get(ourl, headers={**product_headers, 'Accept': 'text/html'}, timeout=15, allow_redirects=True),
            step_name="homepage",
        )
        absorb_cookies(home_resp)
        site_key = extract_storefront_token(home_resp.text)

        if not site_key:
            for alt_path in ['/collections/all', '/collections', '/pages/home']:
                try:
                    alt_resp = session.get(f'{ourl}{alt_path}', headers={**product_headers, 'Accept': 'text/html'}, timeout=10, allow_redirects=True)
                    site_key = extract_storefront_token(alt_resp.text)
                    if site_key:
                        break
                except Exception:
                    pass

        if not site_key:
            return False, "Failed to extract Storefront API access token", gateway, total_price, currency

        print(f'[STEP2] site_key={site_key[:16]}...', file=sys.stderr)
        human_delay()

        # ======== STEP 3: cartCreate (Storefront API) ========
        storefront_headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'origin': ourl,
            'referer': f'{ourl}/',
            'sec-ch-ua': profile['sec_ch_ua'],
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': profile['platform'],
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': profile['ua'],
            'x-sdk-variant': 'portable-wallets',
            'x-shopify-storefront-access-token': site_key,
            'x-start-wallet-checkout': 'true',
            'x-wallet-name': 'MoreOptions',
        }

        cart_data = {
            'query': MUTATION_CART_CREATE,
            'variables': {
                'input': {
                    'lines': [{'merchandiseId': f'gid://shopify/ProductVariant/{product_id_for_cart}', 'quantity': 1, 'attributes': []}],
                    'discountCodes': [],
                    'buyerIdentity': {
                        'countryCode': 'US',
                        'email': f'test{random.randint(1000,9999)}@gmail.com',
                    },
                },
            },
            'operationName': 'cartCreate',
        }

        cart_resp = retry_on_429(
            lambda: session.post(f'{ourl}/api/unstable/graphql.json', params={'operation_name': 'cartCreate'}, headers=storefront_headers, json=cart_data, timeout=20, allow_redirects=True),
            step_name="cart_create",
        )
        if cart_resp.status_code != 200:
            return False, f"CartCreate failed: HTTP {cart_resp.status_code}", gateway, total_price, currency

        try:
            cart_json = cart_resp.json()
            cart_result = cart_json.get('data', {}).get('result', {})
            cart_obj = cart_result.get('cart')
            if not cart_obj:
                cart_errors = cart_result.get('errors') or cart_json.get('errors') or []
                msgs = [e.get('message', str(e)) for e in cart_errors[:3]]
                return False, f"CartCreate error: {'; '.join(msgs) or 'null cart'}", gateway, total_price, currency
            checkout_url = cart_obj.get('checkoutUrl')
            if not checkout_url:
                return False, "CartCreate returned no checkoutUrl", gateway, total_price, currency

            # Extract server-confirmed variant GID from cart
            cart_lines = cart_obj.get('lines', {}).get('edges', [])
            if cart_lines:
                first_merch = _dget(cart_lines, 0, 'node', 'merchandise') or {}
                raw_id = first_merch.get('id', '')
                if raw_id:
                    try:
                        decoded = base64.b64decode(raw_id).decode('utf-8')
                        if decoded.startswith('gid://shopify/ProductVariant/'):
                            storefront_variant_numeric = decoded.split('/')[-1]
                    except Exception:
                        if '/' in raw_id:
                            storefront_variant_numeric = raw_id.split('/')[-1]

                raw_pid = _dget(first_merch, 'product', 'id') or ''
                if raw_pid:
                    try:
                        decoded_pid = base64.b64decode(raw_pid).decode('utf-8')
                        if decoded_pid.startswith('gid://shopify/Product/'):
                            storefront_product_numeric = decoded_pid.split('/')[-1]
                    except Exception:
                        if '/' in raw_pid:
                            storefront_product_numeric = raw_pid.split('/')[-1]

                cart_price = _dget(first_merch, 'priceV2') or {}
                if cart_price.get('amount'):
                    price = float(cart_price['amount'])
                    currency = cart_price.get('currencyCode', currency)
                cart_title = first_merch.get('title')
                if cart_title:
                    product_title = cart_title

        except Exception as e:
            return False, f"CartCreate parse error: {e}", gateway, total_price, currency

        print(f'[STEP3] checkout_url={checkout_url[:60]}...', file=sys.stderr)
        human_delay()

        # ======== STEP 4: GET checkout page → session tokens ========
        checkout_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'User-Agent': profile['ua'],
            'sec-ch-ua': profile['sec_ch_ua'],
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': profile['platform'],
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'Referer': f'{ourl}/cart',
        }

        checkout_page_resp = retry_on_429(
            lambda: session.get(checkout_url, headers=checkout_headers, timeout=20, allow_redirects=True),
            step_name="checkout_page",
        )
        absorb_cookies(checkout_page_resp)

        if checkout_page_resp.status_code not in (200, 301, 302):
            return False, f"CHECKOUT_PAGE_FAILED: HTTP {checkout_page_resp.status_code}", gateway, total_price, currency

        page_html = checkout_page_resp.text
        final_checkout_url = str(checkout_page_resp.url)

        # Extract Shopify-Identification-Signature from checkout page HTML.
        # This binds the PCI vault payment token to this session — prevents ARTIFACT_DISSATISFACTION.
        def _extract_caller_sig(html: str) -> str:
            for pattern in [
                r'checkoutCardsinkCallerIdentificationSignature&quot;:&quot;([^&]+)&quot',
                r'"checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
                r'checkoutCardsinkCallerIdentificationSignature\\u0022:\\u0022([^\\]+)\\u0022',
            ]:
                m = re.search(pattern, html)
                if m: return m.group(1).strip()
            return ''
        caller_id_sig = _extract_caller_sig(page_html)
        if caller_id_sig:
            print(f'[STEP4] caller_id_sig extracted ({len(caller_id_sig)} chars)', file=sys.stderr)
        else:
            print('[STEP4] caller_id_sig NOT found — payment token will be unsigned', file=sys.stderr)

        # Extract checkout session token — Shopify new checkout uses HTML-encoded meta tags
        def _extract_token_from_html(html_text: str, meta_name: str) -> str:
            """Extract token from <meta name="serialized-X" content="&quot;TOKEN&quot;"> pattern."""
            # Pattern 1: HTML-entity-encoded meta tag (new checkout)
            m = re.search(
                r'<meta[^>]+name=["\']serialized-' + re.escape(meta_name) + r'["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
            )
            if m:
                raw = _html.unescape(m.group(1)).strip('"')
                if raw:
                    return raw
            # Pattern 2: HTML-entity-encoded inline (&quot;KEY&quot;:&quot;VALUE&quot;)
            m2 = re.search(r'&quot;' + re.escape(meta_name) + r'&quot;:&quot;([^&]+)&quot;', html_text)
            if m2:
                val = m2.group(1).strip()
                if val and val != 'null':
                    return val
            return ''

        x_checkout_one_session_token = (
            _extract_token_from_html(page_html, 'sessionToken') or
            extract_between(page_html, '"sessionToken":"', '"') or
            extract_between(page_html, 'sessionToken":"', '"') or
            ''
        )
        # Extract source token
        source_token = (
            _extract_token_from_html(page_html, 'sourceToken') or
            extract_between(page_html, '"sourceToken":"', '"') or
            extract_between(page_html, 'sourceToken":"', '"') or
            ''
        )
        # Extract shop ID
        shop_id = (
            extract_between(page_html, '"shopId":"', '"') or
            extract_between(page_html, '"shop_id":"', '"') or
            extract_between(page_html, 'shopId":"', '"') or
            ''
        )
        # Extract queueToken from page HTML (saves one round-trip negotiate)
        page_queue_token = (
            _extract_token_from_html(page_html, 'queueToken') or
            extract_between(page_html, '"queueToken":"', '"') or
            ''
        )

        print(f'[STEP4] session_token={x_checkout_one_session_token[:20] if x_checkout_one_session_token else "MISSING"}... source_token={source_token[:16] if source_token else "NONE"} queue_token={page_queue_token[:20] if page_queue_token else "NONE"} shop_id={shop_id}', file=sys.stderr)

        if not x_checkout_one_session_token:
            return False, "SESSION_TOKEN_MISSING: Could not extract checkout session token", gateway, total_price, currency

        # Build checkout GraphQL endpoint
        parsed_checkout = urlparse(final_checkout_url)
        checkout_host = f'{parsed_checkout.scheme}://{parsed_checkout.netloc}'
        graphql_url = f'{checkout_host}/checkouts/unstable/graphql'

        checkout_web_headers = {
            'accept': 'application/json',
            'content-type': 'application/json',
            'origin': checkout_host,
            'referer': final_checkout_url,
            'user-agent': profile['ua'],
            'sec-ch-ua': profile['sec_ch_ua'],
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': profile['platform'],
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'x-checkout-one-session-token': x_checkout_one_session_token,
            'authorization': f'Bearer {x_checkout_one_session_token}',
            # Required by Shopify fraud engine — mirrors real checkout-web browser headers
            'x-checkout-web-deploy-stage': 'production',
            'x-checkout-web-server-rendering': 'yes',
            'x-checkout-web-server-handling': 'fast',
            'shopify-checkout-client': 'checkout-web/1.0',
            'sec-ch-viewport-width': '1280',
        }
        if source_token:
            checkout_web_headers['x-checkout-web-source-identifier'] = source_token
            checkout_web_headers['x-checkout-web-source-id'] = source_token

        def refresh_session_token(resp):
            nonlocal x_checkout_one_session_token
            new_token = resp.headers.get('x-checkout-one-session-token', '')
            if new_token and new_token != x_checkout_one_session_token:
                x_checkout_one_session_token = new_token
                checkout_web_headers['x-checkout-one-session-token'] = new_token
                checkout_web_headers['authorization'] = f'Bearer {new_token}'

        human_delay()

        # ======== STEP 5: Initial negotiate (empty proposal) ========
        # Use page_queue_token from HTML if available (skips one round-trip)
        queue_token = page_queue_token or ''

        p0_data = {
            'query': QUERY_PROPOSAL,
            'variables': {
                'input': {
                    'purchaseProposal': {},
                    'queueToken': queue_token or None,
                },
            },
            'operationName': 'Proposal',
        }

        p0_resp = retry_on_429(
            lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=p0_data, timeout=20, allow_redirects=True),
            step_name="negotiate_init",
        )
        if p0_resp.status_code not in (200,):
            return False, f"PROPOSAL_BLOCKED: HTTP {p0_resp.status_code}", gateway, total_price, currency
        refresh_session_token(p0_resp)

        try:
            p0_json = p0_resp.json()
            p0_parsed = _parse_negotiate_response(p0_json)
        except Exception as e:
            return False, f"PROPOSAL_JSON_ERROR: {e}", gateway, total_price, currency

        queue_token = p0_parsed['queue_token']
        if p0_parsed['session_token']:
            x_checkout_one_session_token = p0_parsed['session_token']
            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'

        for err in p0_parsed['errors']:
            if err.get('code') == 'CHECKPOINT_BLOCKED':
                return False, "CAPTCHA_BLOCK", gateway, total_price, currency

        print(f'[STEP5] queue_token={queue_token[:20] if queue_token else "NONE"}', file=sys.stderr)
        human_delay()

        # ======== STEP 6: Negotiate with buyer identity + merchandise + delivery ========
        merch_line = _build_merchandise_line(
            variant_id=storefront_variant_numeric,
            product_id=storefront_product_numeric,
            price=price,
            currency=currency,
            title=product_title,
            requires_shipping=requires_shipping,
        )

        delivery_line = _build_delivery_line(
            currency=currency,
            first_name=firstName,
            last_name=lastName,
            street=street,
            city=city,
            country_code=country_code,
            zone_code=state,
            postal_code=s_zip,
            phone=phone,
        )

        pp = {
            'buyerIdentity': {
                'email': f'test{random.randint(1000,9999)}@gmail.com',
                # phone omitted intentionally: causes PAYMENTS_PHONE_NUMBER_DOES_NOT_MATCH_EXPECTED_PATTERN on many stores
            },
            'merchandise': {
                'merchandiseLines': [merch_line],
            },
            'delivery': _build_delivery_terms(
                delivery_lines=[delivery_line],
                no_delivery_required=[],
            ),
            # Accept any computed tax amount — prevents TAX_NEW_TAX_MUST_BE_ACCEPTED on submit
            'taxes': {'proposedTotalAmount': {'any': True}},
            # Required by Shopify fraud/artifact engine — prevents REQUIRED_ARTIFACTS_UNAVAILABLE
            'scriptFingerprint': {
                'signature': None,
                'signatureUuid': None,
                'lineItemScriptChanges': [],
                'paymentScriptChanges': [],
                'shippingScriptChanges': [],
            },
            'optionalDuties': {'buyerRefusesDuties': False},
            'cartMetafields': [],
            'memberships': {'memberships': []},
            'tip': {'tipLines': []},
            'note': {'message': None, 'customAttributes': []},
            'localizationExtension': {'fields': []},
        }

        p1_data = {
            'query': QUERY_PROPOSAL,
            'variables': {
                'input': {
                    'purchaseProposal': pp,
                    'queueToken': queue_token or '',
                },
            },
            'operationName': 'Proposal',
        }

        p1_resp = retry_on_429(
            lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=p1_data, timeout=20, allow_redirects=True),
            step_name="negotiate_proposal1",
        )
        if p1_resp.status_code != 200:
            return False, f"PROPOSAL_BLOCKED: HTTP {p1_resp.status_code}", gateway, total_price, currency
        refresh_session_token(p1_resp)

        try:
            p1_json = p1_resp.json()
            p1_parsed = _parse_negotiate_response(p1_json)
        except Exception as e:
            return False, f"PROPOSAL_JSON_ERROR: {e}", gateway, total_price, currency

        if p1_parsed['queue_token']:
            queue_token = p1_parsed['queue_token']
        if p1_parsed['session_token']:
            x_checkout_one_session_token = p1_parsed['session_token']
            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'

        for err in p1_parsed['errors']:
            if err.get('code') == 'CHECKPOINT_BLOCKED':
                return False, "CAPTCHA_BLOCK", gateway, total_price, currency

        if p1_parsed['checkout_total'] and p1_parsed['checkout_total'] != '0':
            total_price = p1_parsed['checkout_total']
            currency = p1_parsed['checkout_total_currency']
        if p1_parsed['gateway_name']:
            gateway = p1_parsed['gateway_name']

        stable_ids = p1_parsed['stable_ids']
        seller_digest = p1_parsed['seller_digest']
        is_shipping_required = p1_parsed['is_shipping_required']
        server_delivery_lines = p1_parsed['server_delivery_lines']
        payment_method_identifier = p1_parsed['payment_method_identifier'] or 'credit_card'

        print(f'[STEP6] total={total_price} {currency} shipping_required={is_shipping_required} gateway={gateway}', file=sys.stderr)
        human_delay()

        # ======== STEP 7: Poll if delivery is PendingTerms ========
        if not p1_parsed['delivery_resolved'] and p1_parsed['delivery_task_id']:
            poll_delay_ms = p1_parsed['delivery_poll_delay']
            for _ in range(8):
                time.sleep(min(poll_delay_ms / 1000.0, 3.0))
                p_poll = {
                    'query': QUERY_PROPOSAL,
                    'variables': {'input': {'purchaseProposal': pp, 'queueToken': queue_token or ''}},
                    'operationName': 'Proposal',
                }
                poll_resp = session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=p_poll, timeout=20)
                refresh_session_token(poll_resp)
                if poll_resp.status_code == 200:
                    try:
                        poll_json = poll_resp.json()
                        poll_parsed = _parse_negotiate_response(poll_json)
                        if poll_parsed['delivery_resolved']:
                            if poll_parsed['queue_token']:
                                queue_token = poll_parsed['queue_token']
                            if poll_parsed['session_token']:
                                x_checkout_one_session_token = poll_parsed['session_token']
                                checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                                checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                            server_delivery_lines = poll_parsed['server_delivery_lines'] or server_delivery_lines
                            stable_ids = poll_parsed['stable_ids'] or stable_ids
                            if poll_parsed['checkout_total'] and poll_parsed['checkout_total'] != '0':
                                total_price = poll_parsed['checkout_total']
                                currency = poll_parsed['checkout_total_currency']
                            break
                    except Exception:
                        pass

        # ======== STEP 8: Re-negotiate to accept taxes ========
        if server_delivery_lines:
            final_delivery_lines = []
            for sdl in server_delivery_lines:
                fdl = dict(sdl)
                dest = fdl.get('destination') or {}
                sa = (dest.get('streetAddress') or {}) if dest else {}
                if not sa.get('phone'):
                    sa['phone'] = profile.get('phone', '+12025551234')
                if not sa.get('firstName'):
                    sa.update({
                        'firstName': firstName, 'lastName': lastName,
                        'address1': street, 'address2': '',
                        'city': city, 'countryCode': country_code,
                        'zoneCode': state, 'postalCode': s_zip,
                        'phone': profile.get('phone', '+12025551234'),
                    })
                # Always ensure phone is present
                if phone and not sa.get('phone'):
                    sa['phone'] = phone
                fdl['destination'] = {'streetAddress': sa}
                final_delivery_lines.append(fdl)
        else:
            final_delivery_lines = [delivery_line]

        pp['delivery'] = _build_delivery_terms(delivery_lines=final_delivery_lines, no_delivery_required=[])

        tax_reneg_data = {
            'query': QUERY_PROPOSAL,
            'variables': {'input': {'purchaseProposal': pp, 'queueToken': queue_token or ''}},
            'operationName': 'Proposal',
        }
        tax_resp = retry_on_429(
            lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=tax_reneg_data, timeout=20, allow_redirects=True),
            step_name="negotiate_tax",
        )
        refresh_session_token(tax_resp)
        if tax_resp.status_code == 200:
            try:
                tax_json = tax_resp.json()
                tax_parsed = _parse_negotiate_response(tax_json)
                if tax_parsed['queue_token']:
                    queue_token = tax_parsed['queue_token']
                if tax_parsed['session_token']:
                    x_checkout_one_session_token = tax_parsed['session_token']
                    checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                    checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                if tax_parsed['checkout_total'] and tax_parsed['checkout_total'] != '0':
                    total_price = tax_parsed['checkout_total']
                    currency = tax_parsed['checkout_total_currency']
                if tax_parsed['server_delivery_lines']:
                    server_delivery_lines = tax_parsed['server_delivery_lines']
                if tax_parsed['stable_ids']:
                    stable_ids = tax_parsed['stable_ids']
                if tax_parsed['gateway_name'] and gateway == 'UNKNOWN':
                    gateway = tax_parsed['gateway_name']
                if tax_parsed['payment_method_identifier']:
                    payment_method_identifier = tax_parsed['payment_method_identifier']
            except Exception:
                pass

        print(f'[STEP8] post-tax total={total_price} {currency} stable_ids={stable_ids}', file=sys.stderr)
        human_delay()

        # ======== STEP 9: PCI card tokenization ========
        payment_token = tokenize_card(cc, month, year, cvv, session, profile, caller_id_sig=caller_id_sig)
        print(f'[STEP9] payment_token={payment_token[:16]}...', file=sys.stderr)
        human_delay()

        # ======== STEP 10: Negotiate with payment proposal ========
        payment_line = _build_payment_line(
            total_amount=str(total_price),
            currency=currency,
            cc=cc, month=month, year=year, cvv=cvv,
            payment_token=payment_token,
            payment_method_identifier=payment_method_identifier,
        )

        pp['payment'] = {
            'totalAmount': {'value': {'amount': str(total_price), 'currencyCode': currency}},
            'paymentLines': [payment_line],
        }

        p2_data = {
            'query': QUERY_PROPOSAL,
            'variables': {'input': {'purchaseProposal': pp, 'queueToken': queue_token or ''}},
            'operationName': 'Proposal',
        }

        p2_resp = retry_on_429(
            lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=p2_data, timeout=20, allow_redirects=True),
            step_name="negotiate_payment",
        )
        if p2_resp.status_code != 200:
            return False, f"PROPOSAL_BLOCKED: HTTP {p2_resp.status_code}", gateway, total_price, currency
        refresh_session_token(p2_resp)

        try:
            p2_json = p2_resp.json()
            p2_parsed = _parse_negotiate_response(p2_json)
        except Exception as e:
            return False, f"PROPOSAL_JSON_ERROR: {e}", gateway, total_price, currency

        if p2_parsed['queue_token']:
            queue_token = p2_parsed['queue_token']
        if p2_parsed['session_token']:
            x_checkout_one_session_token = p2_parsed['session_token']
            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
        if p2_parsed['checkout_total'] and p2_parsed['checkout_total'] != '0':
            total_price = p2_parsed['checkout_total']
            currency = p2_parsed['checkout_total_currency']
        if p2_parsed['gateway_name'] and gateway == 'UNKNOWN':
            gateway = p2_parsed['gateway_name']
        if p2_parsed['payment_method_identifier']:
            payment_method_identifier = p2_parsed['payment_method_identifier']

        # Already submitted?
        receipt_id = None
        if p2_parsed['result_type'] == 'SubmittedForCompletion':
            receipt_obj = _dget(p2_json, 'data', 'session', 'negotiate', 'result', 'receipt') or {}
            if receipt_obj.get('__typename') == 'FailedReceipt':
                pe = receipt_obj.get('processingError') or {}
                if pe.get('hasOffsitePaymentMethod'):
                    return True, "3DS_REQUIRED", gateway, total_price, currency
                return False, _extract_payment_error_response(pe) or "CARD_DECLINED", gateway, total_price, currency
            receipt_id = receipt_obj.get('id')
        elif p2_parsed['result_type'] == 'CheckpointDenied':
            return False, "CAPTCHA_BLOCK: CheckpointDenied", gateway, total_price, currency
        elif p2_parsed['result_type'] == 'Throttled':
            return False, "PROPOSAL_BLOCKED: Throttled", gateway, total_price, currency
        elif p2_parsed['result_type'] == 'NegotiationResultFailed':
            return False, f"PROPOSAL_BLOCKED: {p2_parsed['failureCode']}", gateway, total_price, currency

        # Update payment amounts with confirmed total
        if pp.get('payment') and total_price and total_price != '0':
            pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
            for pl in pp['payment'].get('paymentLines', []):
                pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}

        if p2_parsed.get('server_delivery_lines'):
            server_delivery_lines = p2_parsed['server_delivery_lines']

        # Update stableIds from latest P2 response (stableIds rotate each negotiate)
        if p2_parsed['stable_ids']:
            stable_ids = p2_parsed['stable_ids']

        # CRITICAL: Rebuild payment line with the payment_method_identifier returned by P2.
        # Shopify returns a store-specific hash pmi (e.g. '69523ab9...') in FilledPaymentTerms.
        # Submitting with 'credit_card' (the default) causes PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE.
        if payment_method_identifier and payment_method_identifier != 'credit_card':
            _refreshed_pl = _build_payment_line(
                total_amount=str(total_price),
                currency=currency,
                cc=cc, month=month, year=year, cvv=cvv,
                payment_token=payment_token,
                payment_method_identifier=payment_method_identifier,
            )
            pp['payment'] = {
                'totalAmount': {'value': {'amount': str(total_price), 'currencyCode': currency}},
                'paymentLines': [_refreshed_pl],
            }
            print(f'[STEP10] rebuilt payment line with pmi={payment_method_identifier[:16]}...', file=sys.stderr)

        print(f'[STEP10] payment negotiated. total={total_price} {currency} stableIds={stable_ids}', file=sys.stderr)
        human_delay()

        # ======== STEP 11: submitForCompletion ========
        if not receipt_id:
            # Build merchandise lines with latest stableId from P2 response
            submit_merch_lines = []
            if stable_ids:
                for sid in stable_ids:
                    ml = dict(merch_line)
                    ml['stableId'] = sid
                    submit_merch_lines.append(ml)
            else:
                submit_merch_lines = [merch_line]

            # Build delivery for submit
            submit_delivery_lines = []
            _phone_val = profile.get('phone', '+12025551234')
            for sdl in (server_delivery_lines or [final_delivery_line for final_delivery_line in [delivery_line]]):
                fdl = dict(sdl)
                dest = fdl.get('destination') or {}
                sa = dict((dest.get('streetAddress') or {}) if dest else {})
                if not sa.get('firstName'):
                    sa.update({
                        'firstName': firstName, 'lastName': lastName,
                        'address1': street, 'address2': '',
                        'city': city, 'countryCode': country_code,
                        'zoneCode': state, 'postalCode': s_zip,
                        'phone': _phone_val,
                    })
                elif not sa.get('phone'):
                    sa['phone'] = _phone_val
                fdl['destination'] = {'streetAddress': sa}
                submit_delivery_lines.append(fdl)

            # ── ARTIFACTS POLL ─────────────────────────────────────────────────────
            # Shopify's fraud/risk engine computes async scores after payment negotiate.
            # Real browser polls Proposal until REQUIRED_ARTIFACTS_UNAVAILABLE clears.
            # Without this, submit returns PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE.
            _artifacts_ready = False
            _artifacts_polls = 0
            _max_artifacts_polls = 8
            # Shopify fraud-engine headers — must be set right before submit
            if source_token:
                checkout_web_headers['shopify-checkout-source'] = f'id="{source_token}", type="cn"'
            checkout_web_headers['shopify-checkout-event-ids'] = f'extensibility:submitForCompletion:{str(uuid.uuid4())}'
            # Build submit_data shell early so artifacts poll can update queueToken in it
            submit_data = {'query': None, 'variables': {'input': {}}}  # placeholder, rebuilt below
            while not _artifacts_ready and _artifacts_polls < _max_artifacts_polls:
                _artifacts_polls += 1
                time.sleep(1.2)
                _art_poll_data = {
                    'query': QUERY_PROPOSAL,
                    'variables': {'input': {'purchaseProposal': pp, 'queueToken': queue_token or ''}},
                    'operationName': 'Proposal',
                }
                _art_resp = retry_on_429(
                    lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=_art_poll_data, timeout=20, allow_redirects=True),
                    step_name='artifacts_poll', max_retries=1, base_delay=2.0, max_delay=6.0,
                )
                if _art_resp and _art_resp.status_code == 200:
                    refresh_session_token(_art_resp)
                    try:
                        _art_json = _art_resp.json()
                        _art_parsed = _parse_negotiate_response(_art_json)
                        _art_errors = [e.get('code','') for e in (_art_parsed.get('errors') or [])]
                        if _art_parsed.get('queue_token'):
                            queue_token = _art_parsed['queue_token']
                        if _art_parsed.get('session_token'):
                            x_checkout_one_session_token = _art_parsed['session_token']
                            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                        if _art_parsed.get('stable_ids'):
                            stable_ids = _art_parsed['stable_ids']
                        if _art_parsed.get('server_delivery_lines'):
                            server_delivery_lines = _art_parsed['server_delivery_lines']
                        if _art_parsed.get('checkout_total') and _art_parsed['checkout_total'] != '0':
                            total_price = _art_parsed['checkout_total']; currency = _art_parsed.get('checkout_total_currency', currency)
                            pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                            for _pl in pp['payment'].get('paymentLines', []):
                                _pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                        print(f'[ARTIFACTS] poll={_artifacts_polls} errors={_art_errors}', file=sys.stderr)
                        _blocking = {'REQUIRED_ARTIFACTS_UNAVAILABLE', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE'}
                        if not _blocking.intersection(set(_art_errors)):
                            _artifacts_ready = True
                            print(f'[ARTIFACTS] Ready after {_artifacts_polls} poll(s). Remaining errors: {_art_errors}', file=sys.stderr)
                            break
                    except Exception as _ae:
                        print(f'[ARTIFACTS] poll error: {_ae}', file=sys.stderr)
            if not _artifacts_ready:
                print(f'[ARTIFACTS] Timeout after {_max_artifacts_polls} polls — re-tokenizing card', file=sys.stderr)
                # Re-tokenize the card since payment session may have expired during long artifact wait
                try:
                    # Use proper PCI vault with caller_id_sig binding (same as STEP9)
                    _pci_headers = {
                        'User-Agent': profile['ua'],
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                        'Referer': 'https://checkout.pci.shopifyinc.com/build/27bcf73/number-ltr.html?identifier=&locationURL=',
                        'sec-fetch-dest': 'empty', 'sec-fetch-mode': 'cors', 'sec-fetch-site': 'same-origin',
                    }
                    if caller_id_sig:
                        _pci_headers['Shopify-Identification-Signature'] = caller_id_sig
                    _retok_resp = session.post(
                        'https://checkout.pci.shopifyinc.com/sessions',
                        json={'credit_card': {'number': cc, 'month': int(month), 'year': int(year),
                            'verification_value': cvv, 'name': f'{firstName} {lastName}'}},
                        headers=_pci_headers, timeout=15,
                    )
                except Exception as _re_err:
                    _retok_resp = None
                    print(f'[ARTIFACTS] Re-tokenize request failed: {_re_err}', file=sys.stderr)
                if _retok_resp and _retok_resp.status_code in (200, 201):
                    try:
                        _new_tok = _retok_resp.json().get('id', '')
                        if _new_tok:
                            payment_token = _new_tok
                            print(f'[ARTIFACTS] Re-tokenized: new payment_token={payment_token[:20]}...', file=sys.stderr)
                            _retok_pl = _build_payment_line(
                                total_amount=str(total_price), currency=currency,
                                cc=cc, month=month, year=year, cvv=cvv,
                                payment_token=payment_token,
                                payment_method_identifier=payment_method_identifier,
                            )
                            pp['payment'] = {
                                'totalAmount': {'value': {'amount': str(total_price), 'currencyCode': currency}},
                                'paymentLines': [_retok_pl],
                            }
                            print(f'[ARTIFACTS] Rebuilt payment with fresh token+amount={total_price}', file=sys.stderr)
                    except Exception as _re:
                        print(f'[ARTIFACTS] Re-tokenize parse error: {_re}', file=sys.stderr)

            # After artifacts loop, extract the LATEST stableIds from the delivery linesV2
            # (Shopify rotates stableIds on every negotiate — must use the final ones)
            if server_delivery_lines:
                _final_stable_ids = []
                for _fdl in server_delivery_lines:
                    _tm = _fdl.get('targetMerchandiseLines') or {}
                    _lv2 = _tm.get('linesV2') or []
                    for _lv in _lv2:
                        _sid = _lv.get('stableId') if isinstance(_lv, dict) else None
                        if _sid and _sid not in _final_stable_ids:
                            _final_stable_ids.append(_sid)
                if _final_stable_ids:
                    stable_ids = _final_stable_ids
                    print(f'[ARTIFACTS] Final stable_ids from delivery linesV2: {stable_ids}', file=sys.stderr)

            # ── Merchandise: update stableIds from latest negotiate response ──────
            submit_merch_lines = []
            if stable_ids:
                for sid in stable_ids:
                    _ml2 = dict(merch_line); _ml2['stableId'] = sid; submit_merch_lines.append(_ml2)
            else:
                submit_merch_lines = [merch_line]
            # Keep full merchandise wrapper (required by NegotiationInput)
            pp['merchandise'] = {'merchandiseLines': submit_merch_lines}

            # ── Delivery: use server-confirmed lines, inject address if missing ──
            _submit_dl_list = []
            _ph = profile.get('phone', '+12025551234')
            for sdl in (server_delivery_lines or [delivery_line]):
                _sdl = dict(sdl)
                _dest = _sdl.get('destination') or {}
                _sa = dict((_dest.get('streetAddress') or {}) if _dest else {})
                if not _sa.get('firstName'):
                    _sa.update({'firstName': firstName, 'lastName': lastName, 'address1': street, 'address2': '',
                                'city': city, 'countryCode': country_code, 'zoneCode': state, 'postalCode': s_zip,
                                'phone': _ph})
                elif not _sa.get('phone'):
                    _sa['phone'] = _ph
                _sdl['destination'] = {'streetAddress': _sa}
                _submit_dl_list.append(_sdl)
            submit_delivery = _build_delivery_terms(delivery_lines=_submit_dl_list, no_delivery_required=[])

            _raw_submit_input = {
                'sessionInput': {'sessionToken': x_checkout_one_session_token},
                'queueToken': queue_token or '',
                'merchandise': pp.get('merchandise'),
                'delivery': submit_delivery,
                'payment': pp['payment'],
                'buyerIdentity': pp.get('buyerIdentity', {}),
                'taxes': pp.get('taxes', {'proposedTotalAmount': {'any': True}}),
                'checkpointData': '',
                # All proposal fields — Shopify fraud engine validates these match negotiate
                'note': pp.get('note'),
                'localizationExtension': pp.get('localizationExtension'),
                'scriptFingerprint': pp.get('scriptFingerprint'),
                'optionalDuties': pp.get('optionalDuties'),
                'cartMetafields': pp.get('cartMetafields', []),
                'memberships': pp.get('memberships'),
                'tip': pp.get('tip'),
            }
            # Strip None values — Shopify rejects unexpected None fields
            submit_input = {k: v for k, v in _raw_submit_input.items() if v is not None}

            attempt_token = str(uuid.uuid4())
            submit_data = {
                'query': MUTATION_SUBMIT,
                'variables': {
                    'input': submit_input,
                    'attemptToken': attempt_token,
                    'metafields': [],
                    'analytics': {
                        'requestUrl': checkout_url,
                        'pageId': f'{random.randint(0x10000000,0x99999999):08x}-{random.randint(0x1000,0x9999):04x}-{random.randint(0x1000,0x9999):04x}-{random.randint(0x1000,0x9999):04x}-{random.randint(0x100000000000,0x999999999999):012x}',
                    },
                },
                'operationName': 'SubmitForCompletion',
            }

            submit_resp = retry_on_429(
                lambda: session.post(graphql_url, params={'operationName': 'SubmitForCompletion'}, headers=checkout_web_headers, json=submit_data, timeout=30, allow_redirects=True),
                step_name="submit",
            )
            if submit_resp.status_code != 200:
                return False, f"SUBMIT_BLOCKED: HTTP {submit_resp.status_code}", gateway, total_price, currency
            refresh_session_token(submit_resp)

            try:
                submit_json = submit_resp.json()
                # Log raw keys so we can see actual field names
                _raw_data = submit_json.get('data') or {}
                print(f'[STEP11] raw_data_keys={list(_raw_data.keys())}', file=sys.stderr)
                if _raw_data:
                    _first_key = list(_raw_data.keys())[0] if _raw_data else 'submitForCompletion'
                    submit_result = _raw_data.get('submitForCompletion') or _raw_data.get(_first_key) or {}
                else:
                    submit_result = {}
                    # Check for errors
                    _errs = submit_json.get('errors') or []
                    if _errs:
                        print(f'[STEP11] GQL errors: {_errs[:2]}', file=sys.stderr)
                        return False, f"SUBMIT_GQL_ERROR: {_errs[0].get('message','unknown')[:100]}", gateway, total_price, currency
                submit_typename = submit_result.get('__typename', '')
                print(f'[STEP11] submit_typename={submit_typename} keys={list(submit_result.keys())[:5]}', file=sys.stderr)
            except Exception as e:
                return False, f"SUBMIT_JSON_ERROR: {e}", gateway, total_price, currency

            if submit_typename == 'SubmitFailed':
                reason = submit_result.get('reason', 'SUBMIT_FAILED')
                return False, reason or 'SUBMIT_FAILED', gateway, total_price, currency

            if submit_typename == 'CheckpointDenied':
                return False, "CAPTCHA_BLOCK: CheckpointDenied", gateway, total_price, currency

            if submit_typename == 'TooManyAttempts':
                return False, "TOO_MANY_ATTEMPTS", gateway, total_price, currency

            if submit_typename == 'TooManyRequests':
                return False, "RATE_LIMITED", gateway, total_price, currency

            if submit_typename == 'Throttled':
                queue_token = submit_result.get('queueToken', queue_token)

            # ── Explicit receipt extraction for SubmitSuccess / SubmittedForCompletion ──
            if submit_typename in ('SubmitSuccess', 'SubmittedForCompletion', 'SubmitAlreadyAccepted'):
                _s_receipt = submit_result.get('receipt') or {}
                _s_rtype = _s_receipt.get('__typename', '')
                print(f'[STEP11] receipt_type={_s_rtype}', file=sys.stderr)
                if _s_rtype == 'ProcessedReceipt':
                    return True, "ORDER_PLACED", gateway, total_price, currency
                if _s_rtype == 'FailedReceipt':
                    pe = _s_receipt.get('processingError') or {}
                    if pe.get('hasOffsitePaymentMethod'):
                        return True, "3DS_REQUIRED", gateway, total_price, currency
                    err_msg = _extract_payment_error_response(pe)
                    return False, err_msg or "CARD_DECLINED", gateway, total_price, currency
                if _s_rtype == 'ActionRequiredReceipt':
                    return True, "3DS_REQUIRED", gateway, total_price, currency
                if _s_rtype in ('ProcessingReceipt', 'WaitingReceipt'):
                    receipt_id = _s_receipt.get('id')
                    # Fall through to poll loop
                elif _s_rtype == '':
                    # Empty receipt — server is still processing, poll with empty receipt_id
                    print(f'[STEP11] WARNING: empty receipt in {submit_typename}, will try poll', file=sys.stderr)
                    # Try to create a poll loop with None id
                    receipt_id = _s_receipt.get('id') or 'POLL_ONLY'

            if submit_typename == 'SubmitRejected':
                errors = submit_result.get('errors') or []
                err_codes = [e.get('code', '') for e in errors]
                print(f'[STEP11] SubmitRejected codes={err_codes} msgs={[e.get("localizedMessage","")[:60] for e in errors[:3]]}', file=sys.stderr)

                # Extract latest delivery lines + stable IDs from rejected sellerProposal
                _rej_seller = submit_result.get('sellerProposal') or {}
                _rej_delivery = _rej_seller.get('delivery') or {}
                _rej_dl_list = _rej_delivery.get('deliveryLines') or [] if _rej_delivery.get('__typename') == 'FilledDeliveryTerms' else []

                # Only retry on DELIVERY_DELIVERY_LINE_DETAIL_CHANGED / TAX errors (not fatal)
                # ARTIFACT_DISSATISFACTION = bot-detection/IP trust issue — NOT a dead site
                # It means Shopify's fraud engine rejected based on datacenter IP score.
                # On residential IP / Railway this resolves. Treat as retryable (site alive).
                _retryable_codes = {
                    'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED', 'TAX_NEW_TAX_MUST_BE_ACCEPTED',
                    'REQUIRED_ARTIFACTS_UNAVAILABLE', 'ARTIFACT_DISSATISFACTION',
                    'MERCHANDISE_CART_UPDATED_BASED_ON_COUNTRY',
                    'PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH',
                    'PAYMENTS_PAYMENT_FLEXIBILITY_TERMS_ID_MISMATCH',
                    'MERCHANDISE_SIGNATURE_MISMATCH',
                    'WAITING_PENDING_TERMS', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE',
                    'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT',
                }
                _fatal_codes = set(err_codes) - _retryable_codes - {'VALIDATION_CUSTOM', 'PAYMENTS_PHONE_NUMBER_DOES_NOT_MATCH_EXPECTED_PATTERN'}
                if _fatal_codes:
                    return False, (
                    "PROCESSING_ERROR" if err_codes and set(err_codes) <= {'ARTIFACT_DISSATISFACTION', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'WAITING_PENDING_TERMS', 'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT', 'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED'}
                    else f"SUBMIT_REJECTED: {', '.join(err_codes) or 'unknown'}"
                ), gateway, total_price, currency

                if not _rej_dl_list:
                    # When only gateway/artifact/payment errors and no delivery lines to retry with,
                    # classify correctly: gateway_unavailable = IP trust issue = PROCESSING_ERROR
                    _ip_block_codes = {'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'ARTIFACT_DISSATISFACTION',
                                       'WAITING_PENDING_TERMS', 'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT'}
                    _remaining = set(err_codes) - _ip_block_codes - {'DELIVERY_PHONE_NUMBER_REQUIRED'}
                    if not _remaining:
                        return False, 'PROCESSING_ERROR', gateway, total_price, currency
                    return False, (
                        f"SUBMIT_REJECTED: {', '.join(err_codes) or 'unknown'}"
                    ), gateway, total_price, currency

                # Rebuild delivery from the server's rejected sellerProposal
                _retry_dls = []
                _retry_stable_ids = []
                for _rdl in _rej_dl_list:
                    _rdl_entry = {
                        'deliveryMethodTypes': _rdl.get('deliveryMethodTypes') or ['SHIPPING'],
                        'targetMerchandiseLines': {'any': True},
                    }
                    # Strategy handle
                    _rds = _rdl.get('selectedDeliveryStrategy') or {}
                    if _rds.get('__typename') == 'CompleteDeliveryStrategy':
                        _rdl_entry['selectedDeliveryStrategy'] = {'deliveryStrategyByHandle': {'handle': _rds.get('handle','shipping'), 'customDeliveryRate': False}}
                    elif _rds.get('__typename') == 'DeliveryStrategyReference':
                        _rdl_entry['selectedDeliveryStrategy'] = {'deliveryStrategyByHandle': {'handle': _rds.get('handle','shipping'), 'customDeliveryRate': False}}
                    else:
                        _rdl_entry['selectedDeliveryStrategy'] = {'deliveryStrategyByHandle': {'handle': 'shipping', 'customDeliveryRate': False}}
                    # Total price
                    _rtot = _rdl.get('totalAmount') or {}
                    if _rtot.get('__typename') == 'AnyConstraint':
                        _rdl_entry['expectedTotalPrice'] = {'any': True}
                    else:
                        _ram, _rac = _extract_money(_rtot)
                        _rdl_entry['expectedTotalPrice'] = {'value': {'amount': _ram or '0', 'currencyCode': _rac or 'USD'}} if _ram and _ram != '0' else {'any': True}
                    # Destination
                    _rdl_entry['destination'] = {'streetAddress': {'firstName': firstName, 'lastName': lastName, 'address1': street, 'address2': '', 'city': city, 'countryCode': country_code, 'zoneCode': state, 'postalCode': s_zip, 'phone': phone}}
                    _retry_dls.append(_rdl_entry)
                    # Extract updated stableIds from targetMerchandise
                    _rtm = _rdl.get('targetMerchandise') or {}
                    if _rtm.get('__typename') == 'FilledMerchandiseLineTargetCollection':
                        for _rl in (_rtm.get('linesV2') or []):
                            if _rl.get('stableId'): _retry_stable_ids.append(_rl['stableId'])

                if _retry_stable_ids: stable_ids = _retry_stable_ids
                # Re-negotiate with corrected delivery to get fresh tokens
                pp['delivery'] = _build_delivery_terms(_retry_dls, [])
                _reneg_data = {'query': QUERY_PROPOSAL, 'variables': {'input': {'purchaseProposal': pp, 'queueToken': queue_token or ''}}, 'operationName': 'Proposal'}
                _reneg_resp = retry_on_429(lambda: session.post(graphql_url, params={'operationName': 'Proposal'}, headers=checkout_web_headers, json=_reneg_data, timeout=20, allow_redirects=True), step_name='reneg_after_reject')
                refresh_session_token(_reneg_resp)
                if _reneg_resp.status_code == 200:
                    try:
                        _rp = _parse_negotiate_response(_reneg_resp.json())
                        if _rp['queue_token']: queue_token = _rp['queue_token']
                        if _rp['session_token']:
                            x_checkout_one_session_token = _rp['session_token']
                            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                        if _rp['stable_ids']: stable_ids = _rp['stable_ids']
                        if _rp['checkout_total'] and _rp['checkout_total'] != '0':
                            total_price = _rp['checkout_total']; currency = _rp['checkout_total_currency']
                            pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                            for _pl in pp['payment'].get('paymentLines', []): _pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                        if _rp['server_delivery_lines']: _retry_dls = _rp['server_delivery_lines']
                        print(f'[RENEG_AFTER_REJECT] stable_ids={stable_ids} total={total_price}', file=sys.stderr)
                    except Exception as _re: print(f'[RENEG_AFTER_REJECT] error: {_re}', file=sys.stderr)

                # Retry submit with corrected delivery + fresh stableIds
                _retry_ml = []
                for _sid in (stable_ids or []):
                    _ml2 = dict(merch_line); _ml2['stableId'] = _sid; _retry_ml.append(_ml2)
                if not _retry_ml: _retry_ml = [merch_line]
                _retry_submit_dls = []
                for _sdl in (_retry_dls or [delivery_line]):
                    _fdl = dict(_sdl)
                    _dest = _fdl.get('destination') or {}; _sa = (_dest.get('streetAddress') or {}) if _dest else {}
                    if not _sa.get('firstName'): _sa.update({'firstName': firstName, 'lastName': lastName, 'address1': street, 'address2': '', 'city': city, 'countryCode': country_code, 'zoneCode': state, 'postalCode': s_zip, 'phone': phone}); _fdl['destination'] = {'streetAddress': _sa}
                    _retry_submit_dls.append(_fdl)
                _retry_input = {'sessionInput': {'sessionToken': x_checkout_one_session_token}, 'queueToken': queue_token or '', 'merchandise': {'merchandiseLines': _retry_ml}, 'delivery': _build_delivery_terms(_retry_submit_dls, []), 'payment': pp['payment'], 'buyerIdentity': pp.get('buyerIdentity', {}), 'taxes': pp.get('taxes', {'proposedTotalAmount': {'any': True}})}
                _retry_attempt = str(uuid.uuid4())
                _retry_sub = retry_on_429(lambda: session.post(graphql_url, params={'operationName': 'SubmitForCompletion'}, headers=checkout_web_headers, json={'query': MUTATION_SUBMIT, 'variables': {'input': _retry_input, 'attemptToken': _retry_attempt}, 'operationName': 'SubmitForCompletion'}, timeout=30, allow_redirects=True), step_name='submit_retry')
                refresh_session_token(_retry_sub)
                print(f'[SUBMIT_RETRY] HTTP={_retry_sub.status_code}', file=sys.stderr)
                if _retry_sub.status_code == 200:
                    try:
                        _rs_j = _retry_sub.json()
                        _rs_r = _dget(_rs_j, 'data', 'submitForCompletion') or {}
                        _rs_t = _rs_r.get('__typename', '')
                        print(f'[SUBMIT_RETRY] typename={_rs_t}', file=sys.stderr)
                        if _rs_t in ('SubmitSuccess', 'SubmittedForCompletion'):
                            _rs_receipt = _rs_r.get('receipt') or {}
                            _rs_rtype = _rs_receipt.get('__typename', '')
                            if _rs_rtype == 'ProcessedReceipt': return True, 'ORDER_PLACED', gateway, total_price, currency
                            if _rs_rtype == 'FailedReceipt':
                                _rs_pe = _rs_receipt.get('processingError') or {}
                                if _rs_pe.get('hasOffsitePaymentMethod'): return True, '3DS_REQUIRED', gateway, total_price, currency
                                return False, _extract_payment_error_response(_rs_pe) or 'CARD_DECLINED', gateway, total_price, currency
                            if _rs_rtype == 'ActionRequiredReceipt': return True, '3DS_REQUIRED', gateway, total_price, currency
                            if _rs_rtype in ('ProcessingReceipt', 'WaitingReceipt'):
                                receipt_id = _rs_receipt.get('id')
                        elif _rs_t == 'SubmitFailed': return False, _rs_r.get('reason','SUBMIT_FAILED') or 'SUBMIT_FAILED', gateway, total_price, currency
                        elif _rs_t == 'CheckpointDenied': return False, 'CAPTCHA_BLOCK', gateway, total_price, currency
                        elif _rs_t == 'SubmitRejected':
                            _rs_errs = [e.get('code','') for e in (_rs_r.get('errors') or [])]
                            _rs_ip_block = {'ARTIFACT_DISSATISFACTION', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE',
                                            'WAITING_PENDING_TERMS', 'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT',
                                            'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED'}
                            return False, (
                            "PROCESSING_ERROR" if _rs_errs and set(_rs_errs) <= _rs_ip_block
                            else f"SUBMIT_REJECTED: {', '.join(_rs_errs) or 'unknown'}"
                        ), gateway, total_price, currency
                        # If we got a receipt_id, fall through to poll loop
                        if receipt_id: pass
                        elif _rs_t not in ('SubmitSuccess','SubmittedForCompletion'): return False, (
                    "PROCESSING_ERROR" if err_codes and set(err_codes) <= {'ARTIFACT_DISSATISFACTION', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'WAITING_PENDING_TERMS', 'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT', 'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED'}
                    else f"SUBMIT_REJECTED: {', '.join(err_codes)}"
                ), gateway, total_price, currency
                    except Exception as _rse:
                        return False, f"SUBMIT_RETRY_ERROR: {_rse}", gateway, total_price, currency
                else:
                    return False, (
                    "PROCESSING_ERROR" if err_codes and set(err_codes) <= {'ARTIFACT_DISSATISFACTION', 'PAYMENTS_PROPOSED_GATEWAY_UNAVAILABLE', 'WAITING_PENDING_TERMS', 'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT', 'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED'}
                    else f"SUBMIT_REJECTED: {', '.join(err_codes) or 'unknown'}"
                ), gateway, total_price, currency

            receipt_obj = None
            for key in ('receipt',):
                inner = submit_result.get(key)
                if inner:
                    receipt_obj = inner
                    break

            if receipt_obj:
                receipt_typename = receipt_obj.get('__typename', '')
                if receipt_typename == 'ProcessedReceipt':
                    return True, "ORDER_PLACED", gateway, total_price, currency
                if receipt_typename == 'FailedReceipt':
                    pe = receipt_obj.get('processingError') or {}
                    if pe.get('hasOffsitePaymentMethod'):
                        return True, "3DS_REQUIRED", gateway, total_price, currency
                    err_msg = _extract_payment_error_response(pe)
                    return False, err_msg or "CARD_DECLINED", gateway, total_price, currency
                if receipt_typename == 'ActionRequiredReceipt':
                    return True, "3DS_REQUIRED", gateway, total_price, currency
                if receipt_typename == 'WaitingReceipt':
                    receipt_id = receipt_obj.get('id')
                if receipt_typename == 'ProcessingReceipt':
                    receipt_id = receipt_obj.get('id')

        # ======== STEP 12: PollForReceipt ========
        if receipt_id:
            # POLL_ONLY means we proceed with poll but don't need a specific receipt_id
            if receipt_id == 'POLL_ONLY':
                receipt_id = None
            max_polls = 12
            poll_delay = 2.5
            for poll_num in range(max_polls):
                time.sleep(poll_delay)
                poll_data = {
                    'query': QUERY_POLL,
                    'variables': {},
                    'operationName': 'PollForReceipt',
                }
                poll_resp = retry_on_429(
                    lambda: session.post(graphql_url, params={'operationName': 'PollForReceipt'}, headers=checkout_web_headers, json=poll_data, timeout=20),
                    step_name=f"poll_{poll_num}",
                )
                refresh_session_token(poll_resp)
                if poll_resp.status_code != 200:
                    continue
                try:
                    poll_json = poll_resp.json()
                    receipt = _dget(poll_json, 'data', 'receipt') or {}
                    r_type = receipt.get('__typename', '')
                    print(f'[STEP12] poll {poll_num+1}/{max_polls} receipt_type={r_type}', file=sys.stderr)
                    if r_type == 'ProcessedReceipt':
                        return True, "ORDER_PLACED", gateway, total_price, currency
                    if r_type == 'FailedReceipt':
                        pe = receipt.get('processingError') or {}
                        if pe.get('hasOffsitePaymentMethod'):
                            return True, "3DS_REQUIRED", gateway, total_price, currency
                        err_msg = _extract_payment_error_response(pe)
                        return False, err_msg or "CARD_DECLINED", gateway, total_price, currency
                    if r_type == 'ActionRequiredReceipt':
                        return True, "3DS_REQUIRED", gateway, total_price, currency
                    if r_type == 'ReceiptNotFound':
                        return False, "RECEIPT_NOT_FOUND", gateway, total_price, currency
                    if r_type == 'ProcessingReceipt':
                        next_poll_ms = receipt.get('pollDelay', 2500)
                        poll_delay = min(next_poll_ms / 1000.0, 5.0)
                        continue
                    if r_type == 'WaitingReceipt':
                        next_poll_ms = receipt.get('pollDelay', 2500)
                        poll_delay = min(next_poll_ms / 1000.0, 5.0)
                        continue
                except Exception:
                    continue

            return False, "POLL_TIMEOUT: Receipt not resolved after max polls", gateway, total_price, currency

        return False, "SUBMIT_NO_RECEIPT: No receipt obtained", gateway, total_price, currency

    except requests.exceptions.ProxyError as e:
        return False, f"PROXY_ERROR: {str(e)[:120]}", gateway, total_price, currency
    except requests.exceptions.SSLError as e:
        return False, f"SSL_ERROR: {str(e)[:120]}", gateway, total_price, currency
    except requests.exceptions.ConnectionError as e:
        return False, f"CONNECTION_ERROR: {str(e)[:120]}", gateway, total_price, currency
    except requests.exceptions.Timeout as e:
        return False, f"TIMEOUT: {str(e)[:120]}", gateway, total_price, currency
    except RuntimeError as e:
        return False, str(e)[:200], gateway, total_price, currency
    except Exception as e:
        return False, f"ERROR: {type(e).__name__}: {str(e)[:150]}", gateway, total_price, currency
