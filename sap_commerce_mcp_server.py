import logging
import os

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from token_vault import vault   # ← server-side token store
from app.models.sap_commerce import (
    CartCard, ProductCard, extract_image_url, get_base_media_url, strip_html,
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL      = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID       = os.getenv("SAP_SITE_ID", "electronics")
CLIENT_ID     = os.getenv("SAP_CLIENT_ID", "mobile_android")
CLIENT_SECRET = os.getenv("SAP_CLIENT_SECRET", "secret")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_PORT      = int(os.getenv("MCP_PORT", "8005"))
MCP_HOST      = os.getenv("MCP_HOST", "127.0.0.1")
_BASE_MEDIA   = get_base_media_url(BASE_URL)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [MCP] %(message)s")
logger = logging.getLogger("sap_mcp")

# ── Register static token in vault (dev mode) ────────────────────────────────
_STATIC_TOKEN = os.getenv("SAP_STATIC_TOKEN")
STATIC_SESSION_ID = None
if _STATIC_TOKEN:
    _static_username = os.getenv("SAP_STATIC_USERNAME", "lang-graph-user")
    STATIC_SESSION_ID = vault.store(
        access_token=_STATIC_TOKEN,
        username=_static_username,
        expires_in=999_999,  # effectively never expires
    )
    logger.info("Registered static token in vault | session=%s | user=%s",
                STATIC_SESSION_ID, _static_username)

# ── HTTP client ───────────────────────────────────────────────────────────────
_http = httpx.Client(timeout=30.0, verify=False)  # verify=False for local dev SAP

def _auth_base() -> str:
    return BASE_URL.replace("/occ/v2", "")

def _h(token: str) -> dict:
    return {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "Authorization": f"Bearer {token}",
    }

def _resolve(session_id: str) -> tuple[str, str]:
    """
    Resolve session_id → (access_token, user_id).
    Raises ValueError if session is invalid/expired.
    Never logs or returns the raw token.
    """
    token = vault.get_token(session_id)
    if not token:
        raise ValueError(f"Invalid or expired session: {session_id}. "
                          "Please login again or get a guest token.")
    user_id = vault.get_user_id(session_id)
    return token, user_id


# ── MCP Server ────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="SAP Commerce Cloud",
    instructions="Full SAP Commerce OCC v2 API — auth, catalog, cart, checkout, orders.",
)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP 1 — Auth  (these return session_id, not token)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def account_login(username: str, password: str) -> dict:
    """
    Authenticate a customer with email + password.
    Returns session_id — pass this to all other tools instead of a token.
    The session_id is an opaque reference; the real token stays server-side.
    """
    url  = f"{_auth_base()}/authorizationserver/oauth/token"
    resp = _http.post(url, data={
        "grant_type":    "password",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username":      username,
        "password":      password,
    })
    if resp.status_code == 200:
        d          = resp.json()
        session_id = vault.store(
            access_token  = d["access_token"],
            refresh_token = d.get("refresh_token"),
            username      = username,
            expires_in    = d.get("expires_in", 3600),
        )
        logger.info("account_login OK | user=%s | session=%s", username, session_id)
        return {
            "success":    True,
            "session_id": session_id,      # ← LLM only sees this
            "username":   username,
            "user_id":    "current",
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def guest_token() -> dict:
    """
    Get an anonymous session for guest shopping.
    Returns session_id — pass this to all cart and checkout tools.
    """
    url  = f"{_auth_base()}/authorizationserver/oauth/token"
    resp = _http.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code == 200:
        d          = resp.json()
        session_id = vault.store(
            access_token = d["access_token"],
            user_id      = "anonymous",
            expires_in   = d.get("expires_in", 3600),
        )
        logger.info("guest_token OK | session=%s", session_id)
        return {
            "success":    True,
            "session_id": session_id,      # ← LLM only sees this
            "user_id":    "anonymous",
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def logout(session_id: str) -> dict:
    """Invalidate a session. Call this when the user logs out."""
    vault.revoke(session_id)
    logger.info("logout | session=%s revoked", session_id)
    return {"success": True, "message": "Session revoked."}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 2 — Product Catalog  (no auth needed for browsing)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_products(
    query: str,
    page_size: int = 5,
    current_page: int = 0,
    sort: str = "relevance",
    session_id: str = "",
) -> dict:
    """
    Search SAP Commerce product catalog.
    session_id is optional for catalog browsing.
    sort: relevance | topRated | name-asc | name-desc | price-asc | price-desc
    """
    logger.info("search_products | query=%s", query)
    token = vault.get_token(session_id) if session_id else None
    headers = _h(token) if token else {"Accept": "application/json"}

    url    = f"{BASE_URL}/{SITE_ID}/products/search"
    params = {
        "query":       query,
        "pageSize":    page_size,
        "currentPage": current_page,
        "sort":        sort,
        "fields":      "products(code,name,summary,price(FULL),stock(FULL),averageRating,images(DEFAULT))",
    }
    resp = _http.get(url, params=params, headers=headers)
    if resp.status_code == 200:
        d = resp.json()
        products = [
            ProductCard.from_sap_product(p, _BASE_MEDIA)
            for p in d.get("products", [])
        ]
        return {
            "success":  True,
            "total":    d.get("pagination", {}).get("totalResults", 0),
            "products": [p.to_tool_dict() for p in products],
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def get_product_details(product_code: str, session_id: str = "") -> dict:
    """Get full details for a product by its code."""
    token   = vault.get_token(session_id) if session_id else None
    headers = _h(token) if token else {"Accept": "application/json"}
    resp    = _http.get(f"{BASE_URL}/{SITE_ID}/products/{product_code}",
                        params={"fields": "FULL"}, headers=headers)
    if resp.status_code == 200:
        p = resp.json()
        return {
            "success":     True,
            "code":        p.get("code"),
            "name":        strip_html(p.get("name", "")),
            "description": strip_html(p.get("description", "")),
            "price":       p.get("price", {}).get("formattedValue"),
            "priceValue":  p.get("price", {}).get("value"),
            "stock":       p.get("stock", {}).get("stockLevelStatus", "unknown"),
            "rating":      p.get("averageRating"),
            "image_url":   extract_image_url(p.get("images", []), _BASE_MEDIA),
            "categories":  [c.get("name") for c in p.get("categories", [])],
        }
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 3 — Cart  (session_id required)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_cart(session_id: str) -> dict:
    """
    Create a new shopping cart.
    Requires session_id from guest_token() or account_login().
    Returns cart_id — store this and pass to add_to_cart, checkout tools.
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts",
        headers=_h(token), json={},
    )
    if resp.status_code in (200, 201):
        d = resp.json()
        code = d.get("code")
        guid = d.get("guid")
        # SAP Commerce requires GUID for anonymous cart URLs, code for authenticated
        effective_cart_id = guid if user_id == "anonymous" else code
        logger.info("create_cart OK | cart=%s guid=%s effective=%s | user=%s",
                    code, guid, effective_cart_id, user_id)
        return {
            "success":   True,
            "cart_id":   effective_cart_id,
            "cart_code": code,
            "cart_guid": guid,
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def add_to_cart(
    session_id: str,
    cart_id: str,
    product_code: str,
    quantity: int = 1,
) -> dict:
    """
    Add a product to the shopping cart.
    Requires session_id and cart_id from create_cart().
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries",
        headers=_h(token),
        json={"product": {"code": product_code}, "quantity": quantity},
    )
    if resp.status_code in (200, 201):
        d = resp.json()
        logger.info("add_to_cart OK | cart=%s | product=%s | qty=%d",
                    cart_id, product_code, quantity)
        return {
            "success":      True,
            "entry_number": d.get("entry", {}).get("entryNumber"),
            "quantity":     d.get("quantityAdded"),
            "status":       d.get("statusCode", "success"),
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_cart(session_id: str, cart_id: str) -> dict:
    """Get current cart contents with item images, quantities, and price breakdown."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}",
        params={"fields": "FULL"}, headers=_h(token),
    )
    if resp.status_code == 200:
        cart = CartCard.from_sap_cart(resp.json(), _BASE_MEDIA)
        return cart.to_tool_dict()
    return {"success": False, "error": resp.text}


@mcp.tool()
def remove_cart_entry(session_id: str, cart_id: str, entry_number: int) -> dict:
    """Remove an item from the cart by entry number."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.delete(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries/{entry_number}",
        headers=_h(token),
    )
    if resp.status_code in (200, 204):
        return {"success": True, "removed": True}
    return {"success": False, "error": resp.text}


@mcp.tool()
def update_cart_entry(
    session_id: str,
    cart_id: str,
    entry_number: int,
    quantity: int,
) -> dict:
    """
    Update the quantity of a cart entry.
    Set quantity to 0 to remove the item.
    After updating, call get_cart to show the updated cart to the user.
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if quantity <= 0:
        resp = _http.delete(
            f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries/{entry_number}",
            headers=_h(token),
        )
        if resp.status_code in (200, 204):
            logger.info("update_cart_entry | removed entry %d from cart %s",
                        entry_number, cart_id)
            return {"success": True, "removed": True, "entry_number": entry_number}
        return {"success": False, "error": resp.text}

    resp = _http.patch(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries/{entry_number}",
        headers=_h(token),
        json={"quantity": quantity},
    )
    if resp.status_code in (200, 204):
        logger.info("update_cart_entry | cart=%s entry=%d qty=%d",
                    cart_id, entry_number, quantity)
        return {
            "success": True,
            "entry_number": entry_number,
            "quantity": quantity,
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def apply_voucher(session_id: str, cart_id: str, voucher_code: str) -> dict:
    """Apply a promotional voucher/coupon code to the cart."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/vouchers",
        headers=_h(token), params={"voucherId": voucher_code},
    )
    if resp.status_code in (200, 201):
        return {"success": True, "voucher_applied": voucher_code}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 3b — User Profile  (session_id required)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_user_addresses(session_id: str) -> dict:
    """Get saved delivery addresses for the logged-in user."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/addresses",
        params={"fields": "FULL"}, headers=_h(token),
    )
    if resp.status_code == 200:
        data = resp.json()
        addresses = []
        for a in data.get("addresses", []):
            addresses.append({
                "id": a.get("id"),
                "firstName": a.get("firstName", ""),
                "lastName": a.get("lastName", ""),
                "line1": a.get("line1", ""),
                "line2": a.get("line2", ""),
                "town": a.get("town", ""),
                "postalCode": a.get("postalCode", ""),
                "country": a.get("country", {}).get("isocode", ""),
                "region": a.get("region", {}).get("isocode", "") if a.get("region") else "",
                "defaultAddress": a.get("defaultAddress", False),
                "formattedAddress": a.get("formattedAddress", ""),
            })
        return {"success": True, "addresses": addresses}
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_user_payment_details(session_id: str) -> dict:
    """Get saved payment methods for the logged-in user."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/paymentdetails",
        params={"fields": "FULL"}, headers=_h(token),
    )
    if resp.status_code == 200:
        data = resp.json()
        payments = []
        for p in data.get("payments", []):
            payments.append({
                "id": p.get("id"),
                "cardType": p.get("cardType", {}).get("name", ""),
                "cardNumber": p.get("cardNumber", ""),
                "expiryMonth": p.get("expiryMonth", ""),
                "expiryYear": p.get("expiryYear", ""),
                "defaultPayment": p.get("defaultPaymentInfo", False),
                "accountHolderName": p.get("accountHolderName", ""),
            })
        return {"success": True, "payments": payments}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 4 — Checkout  (session_id required)
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_delivery_modes(session_id: str, cart_id: str) -> dict:
    """Get available shipping options for the cart."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/deliverymodes",
        headers=_h(token),
    )
    if resp.status_code == 200:
        modes = resp.json().get("deliveryModes", [])
        return {
            "success": True,
            "modes": [
                {
                    "code": m.get("code"),
                    "name": m.get("name"),
                    "cost": m.get("deliveryCost", {}).get("formattedValue", "Free"),
                }
                for m in modes
            ],
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def set_delivery_address(
    session_id: str,
    cart_id: str,
    first_name: str,
    last_name: str,
    line1: str,
    town: str,
    postal_code: str,
    country_isocode: str,
    line2: str = "",
) -> dict:
    """
    Set delivery address on the cart.
    country_isocode: 'DE' Germany, 'US' United States, 'GB' UK, etc.
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/addresses/delivery",
        headers=_h(token),
        json={
            "firstName": first_name,
            "lastName":  last_name,
            "line1":     line1,
            "line2":     line2,
            "town":      town,
            "postalCode": postal_code,
            "country":   {"isocode": country_isocode},
        },
    )
    if resp.status_code in (200, 201):
        logger.info("set_delivery_address OK | cart=%s | %s %s",
                    cart_id, town, country_isocode)
        return {
            "success":         True,
            "address_set":     True,
            "address_summary": f"{first_name} {last_name}, {line1}, {town}, {postal_code}, {country_isocode}",
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def set_delivery_mode(
    session_id: str,
    cart_id: str,
    delivery_mode_code: str = "standard-gross",
) -> dict:
    """
    Set shipping method. Call get_delivery_modes first to see options.
    Common values: 'standard-gross', 'premium-gross'
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.put(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/deliverymode",
        headers=_h(token),
        params={"deliveryModeId": delivery_mode_code},
    )
    if resp.status_code in (200, 204):
        return {"success": True, "delivery_mode": delivery_mode_code}
    return {"success": False, "error": resp.text}


@mcp.tool()
def set_payment_details(
    session_id: str,
    cart_id: str,
    account_holder_name: str,
    card_number: str,
    card_type: str,
    expiry_month: str,
    expiry_year: str,
    cvn: str,
    billing_first_name: str,
    billing_last_name: str,
    billing_line1: str,
    billing_town: str,
    billing_postal_code: str,
    billing_country_isocode: str,
) -> dict:
    """
    Set credit/debit card payment on the cart.
    card_type: 'visa', 'master', 'amex'.
    Must call set_delivery_address and set_delivery_mode first.
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/paymentdetails",
        headers=_h(token),
        json={
            "accountHolderName": account_holder_name,
            "cardNumber":        card_number,
            "cardType":          {"code": card_type},
            "expiryMonth":       expiry_month,
            "expiryYear":        expiry_year,
            "cvn":               cvn,
            "billingAddress": {
                "firstName": billing_first_name,
                "lastName":  billing_last_name,
                "line1":     billing_line1,
                "town":      billing_town,
                "postalCode": billing_postal_code,
                "country":   {"isocode": billing_country_isocode},
            },
        },
    )
    if resp.status_code in (200, 201):
        logger.info("set_payment_details OK | cart=%s | holder=%s",
                    cart_id, account_holder_name)
        return {"success": True, "payment_set": True}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 5 — Orders
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def place_order(session_id: str, cart_id: str, security_code: str = "") -> dict:
    """
    Place the order. ONLY call after explicit human confirmation.
    Requires: delivery address + delivery mode + payment all set on cart.
    Returns order_code.
    """
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.post(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders",
        headers=_h(token),
        params={"cartId": cart_id, "securityCode": security_code, "fields": "FULL"},
    )
    if resp.status_code in (200, 201):
        d = resp.json()
        logger.info("place_order OK | order=%s | user=%s", d.get("code"), user_id)
        return {
            "success":    True,
            "order_code": d.get("code"),
            "status":     d.get("statusDisplay"),
            "total":      d.get("totalPrice", {}).get("formattedValue"),
            "created":    d.get("created"),
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_order(session_id: str, order_code: str) -> dict:
    """Get details of a placed order by order code."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders/{order_code}",
        params={"fields": "FULL"}, headers=_h(token),
    )
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":    True,
            "order_code": d.get("code"),
            "status":     d.get("statusDisplay"),
            "total":      d.get("totalPrice", {}).get("formattedValue"),
            "created":    d.get("created"),
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_order_history(session_id: str, page_size: int = 5) -> dict:
    """Get order history for the logged-in customer."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders",
        params={"fields": "FULL", "pageSize": page_size},
        headers=_h(token),
    )
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success": True,
            "total":   d.get("pagination", {}).get("totalResults", 0),
            "orders": [
                {
                    "code":   o.get("code"),
                    "status": o.get("statusDisplay"),
                    "total":  o.get("total", {}).get("formattedValue"),
                    "placed": o.get("placed"),
                }
                for o in d.get("orders", [])
            ],
        }
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 6 — User Account
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_user_profile(session_id: str) -> dict:
    """Get the logged-in customer's profile."""
    try:
        token, user_id = _resolve(session_id)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resp = _http.get(
        f"{BASE_URL}/{SITE_ID}/users/{user_id}",
        params={"fields": "FULL"}, headers=_h(token),
    )
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":   True,
            "firstName": d.get("firstName"),
            "lastName":  d.get("lastName"),
            "email":     d.get("displayUid"),
        }
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 7 — Health
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def server_health() -> dict:
    """Check connectivity between MCP server and SAP Commerce."""
    import time
    try:
        start = time.time()
        r     = _http.get(f"{BASE_URL}/{SITE_ID}/catalogs", timeout=5)
        ms    = round((time.time() - start) * 1000)
        result = {"success": True, "reachable": True,
                  "status_code": r.status_code, "response_ms": ms}
        if STATIC_SESSION_ID:
            result["static_session_id"] = STATIC_SESSION_ID
        return result
    except Exception as e:
        return {"success": False, "reachable": False, "error": str(e)}


@mcp.tool()
def get_static_session() -> dict:
    """Get the pre-configured static session_id for dev/testing.
    Returns the session_id that is already registered in the token vault."""
    if STATIC_SESSION_ID:
        return {"success": True, "session_id": STATIC_SESSION_ID}
    return {"success": False, "error": "No static token configured"}


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    logger.info("Starting SAP Commerce MCP Server (Secure Edition)")
    logger.info("  SAP URL   : %s", BASE_URL)
    logger.info("  Site ID   : %s", SITE_ID)
    logger.info("  Transport : %s", MCP_TRANSPORT)
    logger.info("  Security  : Token Vault enabled — tokens never sent to LLM")

    if MCP_TRANSPORT in ("sse", "http", "streamable_http"):
        logger.info("  Listening : %s:%d", MCP_HOST, MCP_PORT)
        mcp.run(transport=MCP_TRANSPORT, host=MCP_HOST, port=MCP_PORT)
    else:
        mcp.run()