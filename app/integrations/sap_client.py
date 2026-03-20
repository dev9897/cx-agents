"""
SAP Commerce Cloud OCC REST API client.

All SAP HTTP communication goes through this module.
Business logic belongs in services/, not here.
"""

import logging
import os
import ssl
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sap_agent.sap_client")

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID = os.getenv("SAP_SITE_ID", "electronics")
CLIENT_ID = os.getenv("SAP_CLIENT_ID", "mobile_android")
CLIENT_SECRET = os.getenv("SAP_CLIENT_SECRET", "secret")
SSL_VERIFY = os.getenv("SAP_SSL_VERIFY", "true").lower() != "false"

if not SSL_VERIFY:
    logger.warning("SAP_SSL_VERIFY=false — SSL verification DISABLED (dev only)")

# ── Shared HTTP client ───────────────────────────────────────────────────────

_client = httpx.Client(timeout=10.0, verify=SSL_VERIFY)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _headers(token: Optional[str] = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("_headers | NO token — request will be unauthenticated")
    return h


def _auth_url() -> str:
    return BASE_URL.replace("/occ/v2", "") + "/authorizationserver/oauth/token"


def _handle_http_error(exc: httpx.HTTPError, tool_name: str, url: str) -> dict:
    cause = str(exc.__cause__ or exc)

    if "CERTIFICATE_VERIFY_FAILED" in cause or isinstance(
        getattr(exc, "__cause__", None), ssl.SSLError
    ):
        logger.error("SSL_ERROR | tool=%s | url=%s | error=%s", tool_name, url, cause)
        return {"success": False, "error": f"SSL certificate error reaching SAP ({cause})"}

    if isinstance(exc, httpx.ConnectError):
        logger.error("Connection error | tool=%s | url=%s | %s", tool_name, url, cause)
        return {"success": False, "error": f"Cannot connect to SAP at {url}: {cause}"}

    if isinstance(exc, httpx.TimeoutException):
        logger.error("Timeout | tool=%s | url=%s", tool_name, url)
        return {"success": False, "error": f"SAP request timed out: {url}"}

    if isinstance(exc, httpx.HTTPStatusError):
        logger.error(
            "HTTP %d | tool=%s | url=%s | body=%s",
            exc.response.status_code, tool_name, url, exc.response.text[:300],
        )
        return {"success": False, "error": exc.response.text, "status_code": exc.response.status_code}

    logger.exception("Unexpected HTTP error | tool=%s | url=%s", tool_name, url)
    return {"success": False, "error": str(exc)}


def _safe_request(method: str, url: str, tool_name: str, **kwargs) -> Optional[httpx.Response]:
    logger.debug("SAP %s | tool=%s | url=%s", method.upper(), tool_name, url)
    resp = _client.request(method, url, **kwargs)
    logger.debug("SAP %s | tool=%s | status=%d", method.upper(), tool_name, resp.status_code)
    return resp


def _resolve_user(user_id: str) -> str:
    return user_id or "current"


# ── Auth ─────────────────────────────────────────────────────────────────────

def account_login(username: str, password: str) -> dict:
    url = _auth_url()
    logger.info("account_login | user=%s", username)
    try:
        resp = _safe_request("POST", url, "account_login", data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
        })
        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "username": username,
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "account_login", url)


def get_user_profile(access_token: str) -> dict:
    """Fetch current user's profile from SAP Commerce (includes email)."""
    url = f"{BASE_URL}/{SITE_ID}/users/current"
    try:
        resp = _safe_request("GET", url, "get_user_profile",
                             params={"fields": "uid,name,firstName,lastName,displayUid"},
                             headers=_headers(access_token))
        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "email": data.get("displayUid") or data.get("uid", ""),
                "name": data.get("name", ""),
                "firstName": data.get("firstName", ""),
                "lastName": data.get("lastName", ""),
            }
        return {"success": False, "error": f"Could not fetch profile (HTTP {resp.status_code})"}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_user_profile", url)


def server_account_login(username: str, password: str) -> dict:
    """Server-side only login — NOT exposed to the LLM."""
    url = _auth_url()
    logger.info("server_account_login | user=%s", username)
    try:
        resp = _safe_request("POST", url, "server_account_login", data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
        })
        if resp.status_code == 200:
            data = resp.json()
            access_token = data["access_token"]
            # Fetch the real email from SAP user profile
            profile = get_user_profile(access_token)
            email = profile.get("email", "") if profile.get("success") else ""
            return {
                "success": True,
                "access_token": access_token,
                "refresh_token": data.get("refresh_token"),
                "username": username,
                "email": email,
                "first_name": profile.get("firstName", ""),
                "last_name": profile.get("lastName", ""),
            }
        return {"success": False, "error": "Invalid credentials", "status_code": resp.status_code}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "server_account_login", url)


# ── Products ─────────────────────────────────────────────────────────────────

def search_products(query: str, page_size: int = 5, current_page: int = 0,
                    sort: str = "relevance", access_token: Optional[str] = None) -> dict:
    url = f"{BASE_URL}/{SITE_ID}/products/search"
    params = {
        "query": query, "pageSize": page_size, "currentPage": current_page,
        "sort": sort,
        "fields": "products(code,name,summary,price(FULL),images(DEFAULT),stock(FULL),averageRating)",
    }
    try:
        resp = _safe_request("GET", url, "search_products",
                             params=params, headers=_headers(access_token))
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("products", [])
            return {
                "success": True,
                "total": data.get("pagination", {}).get("totalResults", 0),
                "products": [
                    {
                        "code": p.get("code"),
                        "name": p.get("name"),
                        "summary": p.get("summary", ""),
                        "price": p.get("price", {}).get("formattedValue", "N/A"),
                        "priceValue": p.get("price", {}).get("value"),
                        "stock": p.get("stock", {}).get("stockLevelStatus", "unknown"),
                        "rating": p.get("averageRating"),
                    }
                    for p in products
                ],
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "search_products", url)


def get_product_details(product_code: str, access_token: Optional[str] = None) -> dict:
    url = f"{BASE_URL}/{SITE_ID}/products/{product_code}"
    try:
        resp = _safe_request("GET", url, "get_product_details",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            p = resp.json()
            return {
                "success": True,
                "code": p.get("code"),
                "name": p.get("name"),
                "description": p.get("description", ""),
                "price": p.get("price", {}).get("formattedValue"),
                "priceValue": p.get("price", {}).get("value"),
                "stock": p.get("stock", {}),
                "categories": [c.get("name") for c in p.get("categories", [])],
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_product_details", url)


# ── Cart ─────────────────────────────────────────────────────────────────────

def create_cart(access_token: str, user_id: str = "current") -> dict:
    static_username = os.getenv("SAP_STATIC_USERNAME", "").strip()
    resolved_user = static_username if static_username else user_id
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts"
    try:
        resp = _safe_request("POST", url, "create_cart",
                             headers=_headers(access_token), json={})
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success": True,
                "cart_id": data.get("code"),
                "cart_guid": data.get("guid"),
                "cart_code": data.get("code"),
                "user_id": resolved_user,
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "create_cart", url)


def add_to_cart(cart_id: str, product_code: str, quantity: int = 1,
                access_token: str = "", user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/entries"
    try:
        resp = _safe_request("POST", url, "add_to_cart",
                             headers=_headers(access_token),
                             json={"product": {"code": product_code}, "quantity": quantity})
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success": True,
                "entry_number": data.get("entry", {}).get("entryNumber"),
                "quantity": data.get("quantityAdded"),
                "status": data.get("statusCode", "success"),
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "add_to_cart", url)


def get_cart(cart_id: str, access_token: str = "", user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}"
    try:
        resp = _safe_request("GET", url, "get_cart",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            data = resp.json()
            return {
                "success": True,
                "cart_id": data.get("code"),
                "total": data.get("totalPrice", {}).get("formattedValue"),
                "totalValue": data.get("totalPrice", {}).get("value"),
                "subTotal": data.get("subTotal", {}).get("value"),
                "deliveryCost": data.get("deliveryCost", {}).get("value"),
                "totalTax": data.get("totalTax", {}).get("value"),
                "currency": data.get("totalPrice", {}).get("currencyIso", "USD"),
                "entries": [
                    {
                        "entry_number": e.get("entryNumber"),
                        "product_code": e.get("product", {}).get("code"),
                        "product_name": e.get("product", {}).get("name"),
                        "quantity": e.get("quantity"),
                        "total": e.get("totalPrice", {}).get("formattedValue"),
                        "totalValue": e.get("totalPrice", {}).get("value"),
                        "basePrice": e.get("basePrice", {}).get("value"),
                    }
                    for e in data.get("entries", [])
                ],
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_cart", url)


# ── Checkout ─────────────────────────────────────────────────────────────────

def set_delivery_address(cart_id: str, address: dict,
                         access_token: str = "", user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/addresses/delivery"
    payload = {
        "firstName": address.get("firstName"),
        "lastName": address.get("lastName"),
        "line1": address.get("line1"),
        "line2": address.get("line2", ""),
        "town": address.get("town"),
        "postalCode": address.get("postalCode"),
        "country": {"isocode": address.get("country", "US")},
    }
    try:
        resp = _safe_request("POST", url, "set_delivery_address",
                             headers=_headers(access_token), json=payload)
        if resp.status_code in (200, 201):
            return {"success": True, "address_set": True}
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_delivery_address", url)


def set_delivery_mode(cart_id: str, delivery_mode_code: str = "standard-gross",
                      access_token: str = "", user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/deliverymode"
    try:
        resp = _safe_request("PUT", url, "set_delivery_mode",
                             headers=_headers(access_token),
                             params={"deliveryModeId": delivery_mode_code})
        if resp.status_code in (200, 204):
            return {"success": True, "delivery_mode": delivery_mode_code}
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_delivery_mode", url)


def get_delivery_modes(cart_id: str, access_token: str = "",
                       user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/deliverymodes"
    try:
        resp = _safe_request("GET", url, "get_delivery_modes",
                             headers=_headers(access_token))
        if resp.status_code == 200:
            return {"success": True, "modes": resp.json().get("deliveryModes", [])}
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_delivery_modes", url)


def set_payment_details(cart_id: str, payment: dict,
                        access_token: str = "", user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/carts/{cart_id}/paymentdetails"
    billing = payment.get("billingAddress", {})
    payload = {
        "accountHolderName": payment.get("accountHolderName"),
        "cardNumber": payment.get("cardNumber"),
        "cardType": {"code": payment.get("cardType", "visa")},
        "expiryMonth": payment.get("expiryMonth"),
        "expiryYear": payment.get("expiryYear"),
        "cvn": payment.get("cvn"),
        "billingAddress": {
            "firstName": billing.get("firstName"),
            "lastName": billing.get("lastName"),
            "line1": billing.get("line1"),
            "town": billing.get("town"),
            "postalCode": billing.get("postalCode"),
            "country": {"isocode": billing.get("country", "US")},
        },
    }
    try:
        resp = _safe_request("POST", url, "set_payment_details",
                             headers=_headers(access_token), json=payload)
        if resp.status_code in (200, 201):
            return {"success": True, "payment_set": True}
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "set_payment_details", url)


# ── Orders ───────────────────────────────────────────────────────────────────

def place_order(cart_id: str, access_token: str = "",
                user_id: str = "current", security_code: str = "") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/orders"
    try:
        resp = _safe_request("POST", url, "place_order",
                             headers=_headers(access_token),
                             params={"cartId": cart_id,
                                     "securityCode": security_code,
                                     "fields": "FULL"})
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "success": True,
                "order_code": data.get("code"),
                "status": data.get("statusDisplay"),
                "total": data.get("totalPrice", {}).get("formattedValue"),
                "totalValue": data.get("totalPrice", {}).get("value"),
                "created": data.get("created"),
            }
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "place_order", url)


def get_order(order_code: str, access_token: str = "",
              user_id: str = "current") -> dict:
    resolved_user = _resolve_user(user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{resolved_user}/orders/{order_code}"
    try:
        resp = _safe_request("GET", url, "get_order",
                             params={"fields": "FULL"}, headers=_headers(access_token))
        if resp.status_code == 200:
            return {"success": True, "order": resp.json()}
        return {"success": False, "error": resp.text}
    except httpx.HTTPError as e:
        return _handle_http_error(e, "get_order", url)
