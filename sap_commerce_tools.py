"""
SAP Commerce Cloud API Tools for LangGraph Shopping Agent
Wraps SAP Commerce Cloud OCC REST API endpoints
"""

import logging
import os
import ssl
from typing import Optional

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool

# ─────────────────────────────────────────────
# Load .env FIRST — this file may be imported before main.py calls load_dotenv()
# ─────────────────────────────────────────────
load_dotenv()

logger = logging.getLogger("sap_agent.tools")

# ─────────────────────────────────────────────
# Configuration — override via environment vars
# ─────────────────────────────────────────────
BASE_URL      = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID       = os.getenv("SAP_SITE_ID", "electronics")
CLIENT_ID     = os.getenv("SAP_CLIENT_ID", "mobile_android")
CLIENT_SECRET = os.getenv("SAP_CLIENT_SECRET", "secret")
SSL_VERIFY    = os.getenv("SAP_SSL_VERIFY", "true").lower() != "false"

logger.debug(
    "sap_commerce_tools init | SAP_SSL_VERIFY env='%s' → verify=%s | BASE_URL=%s",
    os.getenv("SAP_SSL_VERIFY", "(not set)"),
    SSL_VERIFY,
    BASE_URL,
)

if not SSL_VERIFY:
    logger.warning(
        "⚠️  SAP_SSL_VERIFY=false — SSL certificate verification is DISABLED. "
        "Only use this in development."
    )

# ─────────────────────────────────────────────
# Shared HTTP client
# Created after env is loaded so verify= is correct.
# ─────────────────────────────────────────────
_client = httpx.Client(timeout=10.0, verify=SSL_VERIFY)


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _headers(token: Optional[str] = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
        logger.debug("_headers | token_len=%d | token_preview=%s...%s",
                     len(token), token[:10], token[-6:])
    else:
        logger.warning("_headers | NO token provided — request will be unauthenticated")
    return h


def _auth_url() -> str:
    """SAP OAuth token endpoint derived from BASE_URL."""
    return BASE_URL.replace("/occ/v2", "") + "/authorizationserver/oauth/token"


def _handle_http_error(exc: httpx.HTTPError, tool_name: str, url: str) -> dict:
    """
    Unified HTTP error handler. Detects SSL errors and logs them with
    actionable context. Returns a dict with success=False so the agent
    can relay a clear message to the user.
    """
    cause = str(exc.__cause__ or exc)

    if "CERTIFICATE_VERIFY_FAILED" in cause or isinstance(getattr(exc, "__cause__", None), ssl.SSLError):
        logger.error(
            "🔒 SSL_ERROR | tool=%s | url=%s | error=%s\n"
            "   OpenSSL : %s\n"
            "   CA file : %s\n"
            "   CA path : %s\n"
            "   Fix     : Set SAP_SSL_VERIFY=false (dev) or add the SAP CA cert:\n"
            "             cp sap-ca.crt /usr/local/share/ca-certificates/ && update-ca-certificates",
            tool_name, url, cause,
            ssl.OPENSSL_VERSION,
            ssl.get_default_verify_paths().cafile,
            ssl.get_default_verify_paths().capath,
        )
        return {"success": False, "error": f"SSL certificate error reaching SAP ({cause})"}

    if isinstance(exc, httpx.ConnectError):
        logger.error("❌ Connection error | tool=%s | url=%s | %s", tool_name, url, cause)
        return {"success": False, "error": f"Cannot connect to SAP at {url}: {cause}"}

    if isinstance(exc, httpx.TimeoutException):
        logger.error("⏱️  Timeout | tool=%s | url=%s", tool_name, url)
        return {"success": False, "error": f"SAP request timed out: {url}"}

    if isinstance(exc, httpx.HTTPStatusError):
        logger.error(
            "❌ HTTP %d | tool=%s | url=%s | body=%s",
            exc.response.status_code, tool_name, url, exc.response.text[:300],
        )
        return {"success": False, "error": exc.response.text, "status_code": exc.response.status_code}

    logger.exception("❌ Unexpected HTTP error | tool=%s | url=%s", tool_name, url)
    return {"success": False, "error": str(exc)}


def _safe_request(method: str, url: str, tool_name: str, **kwargs) -> Optional[httpx.Response]:
    """
    Execute an HTTP request and log the call.
    Returns the Response on success, or raises so the caller can call _handle_http_error.
    """
    logger.debug("→ SAP %s | tool=%s | url=%s", method.upper(), tool_name, url)
    try:
        resp = _client.request(method, url, **kwargs)
        logger.debug("← SAP %s | tool=%s | status=%d", method.upper(), tool_name, resp.status_code)
        return resp
    except httpx.HTTPError:
        raise  # re-raise so caller can call _handle_http_error


def _resolve_user(user_id: str) -> str:
    """
    Returns the SAP user path segment.
    SAP accepts 'current' for any password-grant (user) token.
    Always use a password-grant token in SAP_STATIC_TOKEN, not client_credentials.
    """
    return user_id or "current"


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

@tool
def account_login(username: str, password: str) -> dict:
    """
    Authenticate a customer against SAP Commerce Cloud.
    Returns an access_token to be stored in agent state.
    """
    url = _auth_url()
    logger.info("account_login | user=%s | url=%s", username, url)
    try:
        resp = _safe_request("POST", url, "account_login", data={
            "grant_type":    "password",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username":      username,
            "password":      password,
        })
        if resp.status_code == 200:
            data = resp.json()
            logger.info("account_login | success | user=%s", username)
            return {
                "success":       True,
                "access_token":  data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "username":      username,
            }
        logger.warning("account_login | failed | status=%d | body=%s", resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "account_login", url)


def server_account_login(username: str, password: str) -> dict:
    """
    Server-side only login — NOT a LangChain tool, never exposed to the LLM.

    Called directly by api_server.py's POST /auth/login endpoint.
    Credentials are received over HTTPS, used once to get a token, and
    never stored or logged. Only the resulting access_token is kept in
    session state.
    """
    url = _auth_url()
    # Never log the password — not even at DEBUG level
    logger.info("server_account_login | user=%s | url=%s", username, url)
    try:
        resp = _safe_request("POST", url, "server_account_login", data={
            "grant_type":    "password",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username":      username,
            "password":      password,
        })
        if resp.status_code == 200:
            data = resp.json()
            logger.info("server_account_login | success | user=%s", username)
            return {
                "success":       True,
                "access_token":  data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "username":      username,
            }
        logger.warning(
            "server_account_login | failed | user=%s | status=%d",
            username, resp.status_code,
        )
        return {"success": False, "error": "Invalid credentials", "status_code": resp.status_code}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "server_account_login", url)

# ─────────────────────────────────────────────
# Product Search
# ─────────────────────────────────────────────

@tool
def search_products(
    query: str,
    page_size: int = 5,
    current_page: int = 0,
    sort: str = "relevance",
    access_token: Optional[str] = None,
) -> dict:
    """
    Search for products in SAP Commerce Cloud catalog.
    Returns a list of products with code, name, price, and stock info.
    """
    url = f"{BASE_URL}/{SITE_ID}/products/search"
    logger.info("search_products | query=%r | url=%s", query, url)
    params = {
        "query":       query,
        "pageSize":    page_size,
        "currentPage": current_page,
        "sort":        sort,
        "fields":      "products(code,name,summary,price(FULL),images(DEFAULT),stock(FULL),averageRating)",
    }
    try:
        resp = _safe_request("GET", url, "search_products",
                             params=params, headers=_headers(access_token))
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("products", [])
            logger.info("search_products | found=%d total=%d",
                        len(products), data.get("pagination", {}).get("totalResults", 0))
            return {
                "success": True,
                "total":   data.get("pagination", {}).get("totalResults", 0),
                "products": [
                    {
                        "code":       p.get("code"),
                        "name":       p.get("name"),
                        "summary":    p.get("summary", ""),
                        "price":      p.get("price", {}).get("formattedValue", "N/A"),
                        "priceValue": p.get("price", {}).get("value"),
                        "stock":      p.get("stock", {}).get("stockLevelStatus", "unknown"),
                        "rating":     p.get("averageRating"),
                    }
                    for p in products
                ],
            }
        logger.warning("search_products | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "search_products", url)


@tool
def get_product_details(
    product_code: str,
    access_token: Optional[str] = None,
) -> dict:
    """Get full product details by product code."""
    url = f"{BASE_URL}/{SITE_ID}/products/{product_code}"
    logger.info("get_product_details | code=%s | url=%s", product_code, url)
    try:
        resp = _safe_request("GET", url, "get_product_details",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            p = resp.json()
            return {
                "success":     True,
                "code":        p.get("code"),
                "name":        p.get("name"),
                "description": p.get("description", ""),
                "price":       p.get("price", {}).get("formattedValue"),
                "stock":       p.get("stock", {}),
                "categories":  [c.get("name") for c in p.get("categories", [])],
            }
        logger.warning("get_product_details | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_product_details", url)


# ─────────────────────────────────────────────
# Cart
# ─────────────────────────────────────────────

@tool
def create_cart(access_token: str, user_id: str = "current") -> dict:
    """
    Create a new shopping cart for the authenticated user.
    Returns cart code to use in all subsequent cart operations.
    """
    # SAP rejects "current" for client_credentials tokens.
    # Use the real username from env if configured.
    static_username = os.getenv("SAP_STATIC_USERNAME", "").strip()
    resolved_user   = static_username if static_username else user_id

    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts"
    logger.info("create_cart | user=%s | url=%s", resolved_user, url)
    logger.debug("create_cart | token_len=%d | token=%s...%s",
                 len(access_token) if access_token else 0,
                 access_token[:15] if access_token else "NONE",
                 access_token[-8:] if access_token else "NONE")
    try:
        resp = _safe_request("POST", url, "create_cart",
                             headers=_headers(access_token), json={})
        if resp.status_code in (200, 201):
            data = resp.json()
            cart_code = data.get("code")
            cart_guid = data.get("guid")
            logger.info("create_cart | success | cart_code=%s | cart_guid=%s", cart_code, cart_guid)
            return {
                "success":   True,
                "cart_id":   cart_code,    # authenticated users always use numeric code
                "cart_guid": cart_guid,
                "cart_code": cart_code,
                "user_id":   resolved_user,
            }
        logger.warning("create_cart | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "create_cart", url)


@tool
def add_to_cart(
    cart_id: str,
    product_code: str,
    quantity: int = 1,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """Add a product to the shopping cart. Requires cart_id and product_code."""
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/entries"
    logger.info("add_to_cart | user=%s | cart=%s | product=%s | qty=%d",
                resolved_user, cart_id, product_code, quantity)
    try:
        resp = _safe_request("POST", url, "add_to_cart",
                             headers=_headers(access_token),
                             json={"product": {"code": product_code}, "quantity": quantity})
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success":      True,
                "entry_number": data.get("entry", {}).get("entryNumber"),
                "quantity":     data.get("quantityAdded"),
                "status":       data.get("statusCode", "success"),
            }
        logger.warning("add_to_cart | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "add_to_cart", url)


@tool
def get_cart(
    cart_id: str,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """Retrieve current cart contents."""
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}"
    logger.info("get_cart | user=%s | cart=%s", resolved_user, cart_id)
    try:
        resp = _safe_request("GET", url, "get_cart",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            data = resp.json()
            entries = data.get("entries", [])
            return {
                "success": True,
                "cart_id": data.get("code"),
                "total":   data.get("totalPrice", {}).get("formattedValue"),
                "entries": [
                    {
                        "entry_number": e.get("entryNumber"),
                        "product_code": e.get("product", {}).get("code"),
                        "product_name": e.get("product", {}).get("name"),
                        "quantity":     e.get("quantity"),
                        "total":        e.get("totalPrice", {}).get("formattedValue"),
                    }
                    for e in entries
                ],
            }
        logger.warning("get_cart | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_cart", url)


# ─────────────────────────────────────────────
# Checkout
# ─────────────────────────────────────────────

@tool
def set_delivery_address(
    cart_id: str,
    address: dict,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """
    Set delivery address on the cart.
    address dict keys: firstName, lastName, line1, line2, town, postalCode, country (isocode e.g. 'DE')
    """
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/addresses/delivery"
    logger.info("set_delivery_address | user=%s | cart=%s", resolved_user, cart_id)
    payload = {
        "firstName":  address.get("firstName"),
        "lastName":   address.get("lastName"),
        "line1":      address.get("line1"),
        "line2":      address.get("line2", ""),
        "town":       address.get("town"),
        "postalCode": address.get("postalCode"),
        "country":    {"isocode": address.get("country", "US")},
    }
    try:
        resp = _safe_request("POST", url, "set_delivery_address",
                             headers=_headers(access_token), json=payload)
        if resp.status_code in (200, 201):
            return {"success": True, "address_set": True}
        logger.warning("set_delivery_address | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_delivery_address", url)


@tool
def set_delivery_mode(
    cart_id: str,
    delivery_mode_code: str = "standard-gross",
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """Set delivery/shipping mode. Common codes: standard-gross, premium-gross."""
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/deliverymode"
    logger.info("set_delivery_mode | user=%s | cart=%s | mode=%s",
                resolved_user, cart_id, delivery_mode_code)
    try:
        resp = _safe_request("PUT", url, "set_delivery_mode",
                             headers=_headers(access_token),
                             params={"deliveryModeId": delivery_mode_code})
        if resp.status_code in (200, 204):
            return {"success": True, "delivery_mode": delivery_mode_code}
        logger.warning("set_delivery_mode | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_delivery_mode", url)


@tool
def set_payment_details(
    cart_id: str,
    payment: dict,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """
    Set payment details (credit card) on the cart.
    payment dict keys: accountHolderName, cardNumber, cardType (visa/master),
                       expiryMonth, expiryYear, cvn,
                       billingAddress (same keys as delivery address dict)
    """
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/paymentdetails"
    logger.info("set_payment_details | user=%s | cart=%s", resolved_user, cart_id)
    billing = payment.get("billingAddress", {})
    payload = {
        "accountHolderName": payment.get("accountHolderName"),
        "cardNumber":        payment.get("cardNumber"),
        "cardType":          {"code": payment.get("cardType", "visa")},
        "expiryMonth":       payment.get("expiryMonth"),
        "expiryYear":        payment.get("expiryYear"),
        "cvn":               payment.get("cvn"),
        "billingAddress": {
            "firstName":  billing.get("firstName"),
            "lastName":   billing.get("lastName"),
            "line1":      billing.get("line1"),
            "town":       billing.get("town"),
            "postalCode": billing.get("postalCode"),
            "country":    {"isocode": billing.get("country", "US")},
        },
    }
    try:
        resp = _safe_request("POST", url, "set_payment_details",
                             headers=_headers(access_token), json=payload)
        if resp.status_code in (200, 201):
            return {"success": True, "payment_set": True}
        logger.warning("set_payment_details | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_payment_details", url)


# ─────────────────────────────────────────────
# Place Order
# ─────────────────────────────────────────────

@tool
def place_order(
    cart_id: str,
    access_token: str = "",
    user_id: str = "current",
    security_code: str = "",
) -> dict:
    """
    Place the order. Cart must have delivery address, delivery mode,
    and payment details set before calling this. Returns order code and status.
    """
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/orders"
    logger.info("place_order | user=%s | cart=%s", resolved_user, cart_id)
    try:
        resp = _safe_request("POST", url, "place_order",
                             headers=_headers(access_token),
                             params={"cartId": cart_id,
                                     "securityCode": security_code,
                                     "fields": "FULL"})
        if resp.status_code in (200, 201):
            data = resp.json()
            logger.info("place_order | success | order_code=%s", data.get("code"))
            return {
                "success":    True,
                "order_code": data.get("code"),
                "status":     data.get("statusDisplay"),
                "total":      data.get("totalPrice", {}).get("formattedValue"),
                "created":    data.get("created"),
            }
        logger.warning("place_order | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "place_order", url)


@tool
def get_order(
    order_code: str,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """Retrieve order details by order code."""
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/orders/{order_code}"
    logger.info("get_order | order=%s | user=%s", order_code, resolved_user)
    try:
        resp = _safe_request("GET", url, "get_order",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            return {"success": True, "order": resp.json()}
        logger.warning("get_order | failed | status=%d | body=%s",
                       resp.status_code, resp.text[:200])
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_order", url)


# ─────────────────────────────────────────────
# Exports
# NOTE: account_login is intentionally excluded from ALL_TOOLS.
# Credentials must never pass through the LLM. Authentication is handled
# directly by api_server.py's /auth/login endpoint which calls
# _server_account_login() and stores the token in session state.
# ─────────────────────────────────────────────

ALL_TOOLS = [
    search_products,
    get_product_details,
    create_cart,
    add_to_cart,
    get_cart,
    set_delivery_address,
    set_delivery_mode,
    set_payment_details,
    place_order,
    get_order,
]