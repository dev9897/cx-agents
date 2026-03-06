"""
sap_commerce_mcp_server.py
==========================
Your OWN MCP Server for SAP Commerce Cloud (OCC API v2).

Exposes all shopping endpoints as MCP tools — discoverable by ANY
MCP-compatible client (LangGraph, Claude Desktop, Cursor, etc.)

Transport options (set MCP_TRANSPORT in .env):
  stdio  — default, for Claude Desktop / local clients
  sse    — HTTP Server-Sent Events, for LangGraph over network
  streamable_http — modern HTTP transport (MCP spec 2025-03-26)

Run:
  python sap_commerce_mcp_server.py            # stdio
  MCP_TRANSPORT=sse python sap_commerce_mcp_server.py   # SSE on :8002
"""

import os
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL      = os.getenv("SAP_BASE_URL", "https://localhost:9002/occ/v2")
SITE_ID       = os.getenv("SAP_SITE_ID", "electronics")
CLIENT_ID     = os.getenv("SAP_CLIENT_ID", "personalizationSetupClient")
CLIENT_SECRET = os.getenv("SAP_CLIENT_SECRET", "YourSecurePassword")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")   # stdio | sse | streamable_http
MCP_PORT      = int(os.getenv("MCP_PORT", "8005"))
MCP_HOST      = os.getenv("MCP_HOST", "0.0.0.0")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [MCP] %(message)s")
logger = logging.getLogger("sap_mcp")

# ─────────────────────────────────────────────────────────────────────────────
# HTTP client (shared, reused across all tool calls)
# ─────────────────────────────────────────────────────────────────────────────
_http = httpx.Client(timeout=30.0, verify=True)

def _h(token: Optional[str] = None) -> dict:
    """Build request headers, optionally with Bearer token."""
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def _auth_base() -> str:
    """Base URL for the OAuth server (strips /occ/v2)."""
    return BASE_URL.replace("/occ/v2", "")

# ─────────────────────────────────────────────────────────────────────────────
# MCP Server instance
# ─────────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="SAP Commerce Cloud",
    description=(
        "Full SAP Commerce Cloud OCC v2 shopping API — "
        "authentication, product search, cart management, "
        "checkout, and order placement."
    ),
)

# ═════════════════════════════════════════════════════════════════════════════
# GROUP 1 — Authentication
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def account_login(username: str, password: str) -> dict:
    """
    Authenticate a registered customer with email + password.
    Returns access_token and refresh_token.
    Store the access_token and pass it to all subsequent tools.
    """
    logger.info("account_login: %s", username)
    url = f"{_auth_base()}/authorizationserver/oauth/token"
    resp = _http.post(url, data={
        "grant_type":    "password",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username":      username,
        "password":      password,
    })
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":       True,
            "access_token":  d["access_token"],
            "refresh_token": d.get("refresh_token"),
            "expires_in":    d.get("expires_in"),
            "username":      username,
        }
    logger.warning("account_login failed: %s", resp.text[:200])
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def guest_token() -> dict:
    """
    Obtain an anonymous/guest OAuth token via client_credentials flow.
    Use this when the customer is not logged in.
    Returns access_token — pass it to cart and checkout tools.
    """
    logger.info("guest_token: requesting anonymous token")
    url = f"{_auth_base()}/authorizationserver/oauth/token"
    resp = _http.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code == 200:
        d = resp.json()
        return {"success": True, "access_token": d["access_token"],
                "expires_in": d.get("expires_in")}
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def refresh_token(refresh_tok: str) -> dict:
    """
    Refresh an expired access_token using a refresh_token.
    Returns a new access_token.
    """
    url = f"{_auth_base()}/authorizationserver/oauth/token"
    resp = _http.post(url, data={
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_tok,
    })
    if resp.status_code == 200:
        d = resp.json()
        return {"success": True, "access_token": d["access_token"],
                "expires_in": d.get("expires_in")}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 2 — Product Catalog
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def search_products(
    query: str,
    page_size: int = 5,
    current_page: int = 0,
    sort: str = "relevance",
    access_token: Optional[str] = None,
) -> dict:
    """
    Search the SAP Commerce product catalog by keyword.
    Returns product list with code, name, price, stock, and rating.

    sort options: relevance | topRated | name-asc | name-desc | price-asc | price-desc
    """
    logger.info("search_products: query=%s", query)
    url = f"{BASE_URL}/{SITE_ID}/products/search"
    params = {
        "query":       query,
        "pageSize":    page_size,
        "currentPage": current_page,
        "sort":        sort,
        "fields":      "products(code,name,summary,price(FULL),images(DEFAULT),stock(FULL),averageRating)",
    }
    resp = _http.get(url, params=params, headers=_h(access_token))
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success": True,
            "total":   d.get("pagination", {}).get("totalResults", 0),
            "page":    current_page,
            "products": [
                {
                    "code":       p.get("code"),
                    "name":       p.get("name"),
                    "summary":    p.get("summary", ""),
                    "price":      p.get("price", {}).get("formattedValue", "N/A"),
                    "priceValue": p.get("price", {}).get("value"),
                    "currency":   p.get("price", {}).get("currencyIso"),
                    "stock":      p.get("stock", {}).get("stockLevelStatus", "unknown"),
                    "stockLevel": p.get("stock", {}).get("stockLevel"),
                    "rating":     p.get("averageRating"),
                }
                for p in d.get("products", [])
            ],
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def get_product_details(
    product_code: str,
    access_token: Optional[str] = None,
) -> dict:
    """
    Get full details for a single product by its product code.
    Returns description, price, stock level, categories, and images.
    """
    logger.info("get_product_details: %s", product_code)
    url = f"{BASE_URL}/{SITE_ID}/products/{product_code}"
    resp = _http.get(url, params={"fields": "FULL"}, headers=_h(access_token))
    if resp.status_code == 200:
        p = resp.json()
        return {
            "success":     True,
            "code":        p.get("code"),
            "name":        p.get("name"),
            "description": p.get("description", ""),
            "price":       p.get("price", {}).get("formattedValue"),
            "priceValue":  p.get("price", {}).get("value"),
            "currency":    p.get("price", {}).get("currencyIso"),
            "stock":       p.get("stock", {}),
            "categories":  [c.get("name") for c in p.get("categories", [])],
            "manufacturer":p.get("manufacturer"),
            "averageRating": p.get("averageRating"),
            "numberOfReviews": p.get("numberOfReviews"),
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def get_categories(access_token: Optional[str] = None) -> dict:
    """List all top-level product categories in the catalog."""
    url = f"{BASE_URL}/{SITE_ID}/catalogs"
    resp = _http.get(url, headers=_h(access_token))
    if resp.status_code == 200:
        return {"success": True, "catalogs": resp.json().get("catalogs", [])}
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_product_reviews(
    product_code: str,
    access_token: Optional[str] = None,
) -> dict:
    """Get customer reviews for a product."""
    url = f"{BASE_URL}/{SITE_ID}/products/{product_code}/reviews"
    resp = _http.get(url, headers=_h(access_token))
    if resp.status_code == 200:
        return {"success": True, "reviews": resp.json().get("reviews", [])}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 3 — Cart Management
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_cart(
    access_token: str,
    user_id: str = "anonymous",
) -> dict:
    """
    Create a new empty shopping cart.
    Returns cart_id and cart_guid — store both, pass cart_id to other cart tools.
    user_id: use 'current' for logged-in customers, 'anonymous' for guests.
    """
    logger.info("create_cart: user_id=%s", user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts"
    resp = _http.post(url, headers=_h(access_token), json={})
    if resp.status_code in (200, 201):
        d = resp.json()
        return {
            "success":   True,
            "cart_id":   d.get("code"),
            "cart_guid": d.get("guid"),
            "total":     d.get("totalPrice", {}).get("formattedValue", "0"),
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def add_to_cart(
    cart_id: str,
    product_code: str,
    quantity: int = 1,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Add a product to the shopping cart.
    product_code must be the SAP product code (e.g. '300938').
    Returns entry_number and quantity_added.
    """
    logger.info("add_to_cart: cart=%s product=%s qty=%d", cart_id, product_code, quantity)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries"
    payload = {"product": {"code": product_code}, "quantity": quantity}
    resp = _http.post(url, headers=_h(access_token), json=payload)
    if resp.status_code in (200, 201):
        d = resp.json()
        return {
            "success":      True,
            "entry_number": d.get("entry", {}).get("entryNumber"),
            "quantity":     d.get("quantityAdded"),
            "status":       d.get("statusCode", "success"),
            "message":      d.get("statusMessage", ""),
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def get_cart(
    cart_id: str,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Get full contents of the shopping cart including all line items,
    quantities, prices, and cart total.
    """
    logger.info("get_cart: cart=%s", cart_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}"
    resp = _http.get(url, params={"fields": "FULL"}, headers=_h(access_token))
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":  True,
            "cart_id":  d.get("code"),
            "total":    d.get("totalPrice", {}).get("formattedValue"),
            "subtotal": d.get("subTotal", {}).get("formattedValue"),
            "item_count": d.get("totalItems", 0),
            "entries": [
                {
                    "entry_number": e.get("entryNumber"),
                    "product_code": e.get("product", {}).get("code"),
                    "product_name": e.get("product", {}).get("name"),
                    "quantity":     e.get("quantity"),
                    "unit_price":   e.get("basePrice", {}).get("formattedValue"),
                    "total":        e.get("totalPrice", {}).get("formattedValue"),
                }
                for e in d.get("entries", [])
            ],
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def update_cart_entry(
    cart_id: str,
    entry_number: int,
    quantity: int,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Update quantity of an existing cart entry.
    Set quantity=0 to remove the item from the cart.
    """
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries/{entry_number}"
    resp = _http.patch(url, headers=_h(access_token),
                       json={"quantity": quantity})
    if resp.status_code in (200, 204):
        return {"success": True, "updated": True, "quantity": quantity}
    return {"success": False, "error": resp.text}


@mcp.tool()
def remove_cart_entry(
    cart_id: str,
    entry_number: int,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """Remove a specific item from the shopping cart by entry number."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/entries/{entry_number}"
    resp = _http.delete(url, headers=_h(access_token))
    if resp.status_code in (200, 204):
        return {"success": True, "removed": True}
    return {"success": False, "error": resp.text}


@mcp.tool()
def apply_voucher(
    cart_id: str,
    voucher_code: str,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """Apply a promotional voucher/coupon code to the cart."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/vouchers"
    resp = _http.post(url, headers=_h(access_token),
                      params={"voucherId": voucher_code})
    if resp.status_code in (200, 201):
        return {"success": True, "voucher_applied": voucher_code}
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 4 — Checkout
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_delivery_modes(
    cart_id: str,
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Get available delivery/shipping modes for the current cart.
    Returns list of modes with code, name, and cost.
    Call this before set_delivery_mode to show options to the customer.
    """
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/deliverymodes"
    resp = _http.get(url, headers=_h(access_token))
    if resp.status_code == 200:
        modes = resp.json().get("deliveryModes", [])
        return {
            "success": True,
            "modes": [
                {
                    "code":        m.get("code"),
                    "name":        m.get("name"),
                    "description": m.get("description", ""),
                    "cost":        m.get("deliveryCost", {}).get("formattedValue", "Free"),
                }
                for m in modes
            ],
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def set_delivery_address(
    cart_id: str,
    first_name: str,
    last_name: str,
    line1: str,
    town: str,
    postal_code: str,
    country_isocode: str,
    line2: str = "",
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Set the delivery/shipping address on the cart.
    country_isocode examples: 'DE' for Germany, 'US' for United States, 'GB' for UK.
    This must be called before set_delivery_mode and set_payment_details.
    """
    logger.info("set_delivery_address: cart=%s %s %s", cart_id, town, country_isocode)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/addresses/delivery"
    payload = {
        "firstName": first_name,
        "lastName":  last_name,
        "line1":     line1,
        "line2":     line2,
        "town":      town,
        "postalCode": postal_code,
        "country": {"isocode": country_isocode},
    }
    resp = _http.post(url, headers=_h(access_token), json=payload)
    if resp.status_code in (200, 201):
        return {"success": True, "address_set": True,
                "address_summary": f"{first_name} {last_name}, {line1}, {town}, {postal_code}, {country_isocode}"}
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def set_delivery_mode(
    cart_id: str,
    delivery_mode_code: str = "standard-gross",
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Set the delivery/shipping method on the cart.
    Common codes: 'standard-gross' (standard), 'premium-gross' (express).
    Call get_delivery_modes first to see all available options.
    Must be called after set_delivery_address.
    """
    logger.info("set_delivery_mode: cart=%s mode=%s", cart_id, delivery_mode_code)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/deliverymode"
    resp = _http.put(url, headers=_h(access_token),
                     params={"deliveryModeId": delivery_mode_code})
    if resp.status_code in (200, 204):
        return {"success": True, "delivery_mode": delivery_mode_code}
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def set_payment_details(
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
    access_token: str = "",
    user_id: str = "anonymous",
) -> dict:
    """
    Set payment (credit/debit card) details on the cart.
    card_type values: 'visa', 'master', 'amex', 'diners'.
    expiry_month: 2-digit month e.g. '03'.
    expiry_year: 4-digit year e.g. '2027'.
    Must be called after set_delivery_address and set_delivery_mode.
    """
    logger.info("set_payment_details: cart=%s holder=%s", cart_id, account_holder_name)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/carts/{cart_id}/paymentdetails"
    payload = {
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
    }
    resp = _http.post(url, headers=_h(access_token), json=payload)
    if resp.status_code in (200, 201):
        return {"success": True, "payment_set": True}
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 5 — Order
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def place_order(
    cart_id: str,
    access_token: str = "",
    user_id: str = "anonymous",
    security_code: str = "",
) -> dict:
    """
    Place the order. ONLY call this after explicit human confirmation.
    Prerequisites: delivery address + delivery mode + payment details must all be set.
    Returns order_code — save this for the customer.
    """
    logger.info("place_order: cart=%s user=%s", cart_id, user_id)
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders"
    resp = _http.post(url, headers=_h(access_token),
                      params={"cartId": cart_id,
                              "securityCode": security_code,
                              "fields": "FULL"})
    if resp.status_code in (200, 201):
        d = resp.json()
        return {
            "success":    True,
            "order_code": d.get("code"),
            "status":     d.get("statusDisplay"),
            "total":      d.get("totalPrice", {}).get("formattedValue"),
            "created":    d.get("created"),
            "entries":    len(d.get("entries", [])),
        }
    return {"success": False, "error": resp.text, "status_code": resp.status_code}


@mcp.tool()
def get_order(
    order_code: str,
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """Get full details of a placed order by order code."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders/{order_code}"
    resp = _http.get(url, params={"fields": "FULL"}, headers=_h(access_token))
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":    True,
            "order_code": d.get("code"),
            "status":     d.get("statusDisplay"),
            "total":      d.get("totalPrice", {}).get("formattedValue"),
            "created":    d.get("created"),
            "entries": [
                {
                    "product": e.get("product", {}).get("name"),
                    "quantity": e.get("quantity"),
                    "total": e.get("totalPrice", {}).get("formattedValue"),
                }
                for e in d.get("entries", [])
            ],
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_order_history(
    access_token: str,
    user_id: str = "current",
    page_size: int = 5,
) -> dict:
    """Get order history for the logged-in customer."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/orders"
    resp = _http.get(url,
                     params={"fields": "FULL", "pageSize": page_size},
                     headers=_h(access_token))
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success": True,
            "total":   d.get("pagination", {}).get("totalResults", 0),
            "orders": [
                {
                    "code":    o.get("code"),
                    "status":  o.get("statusDisplay"),
                    "total":   o.get("total", {}).get("formattedValue"),
                    "placed":  o.get("placed"),
                }
                for o in d.get("orders", [])
            ],
        }
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 6 — User Account
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_user_profile(
    access_token: str,
    user_id: str = "current",
) -> dict:
    """Get the logged-in customer's profile details."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}"
    resp = _http.get(url, params={"fields": "FULL"}, headers=_h(access_token))
    if resp.status_code == 200:
        d = resp.json()
        return {
            "success":   True,
            "uid":       d.get("uid"),
            "firstName": d.get("firstName"),
            "lastName":  d.get("lastName"),
            "email":     d.get("displayUid"),
            "currency":  d.get("currency", {}).get("isocode"),
            "language":  d.get("language", {}).get("isocode"),
        }
    return {"success": False, "error": resp.text}


@mcp.tool()
def get_saved_addresses(
    access_token: str,
    user_id: str = "current",
) -> dict:
    """Get saved delivery addresses for the logged-in customer."""
    url = f"{BASE_URL}/{SITE_ID}/users/{user_id}/addresses"
    resp = _http.get(url, params={"fields": "FULL"}, headers=_h(access_token))
    if resp.status_code == 200:
        addrs = resp.json().get("addresses", [])
        return {
            "success": True,
            "addresses": [
                {
                    "id":         a.get("id"),
                    "firstName":  a.get("firstName"),
                    "lastName":   a.get("lastName"),
                    "line1":      a.get("line1"),
                    "town":       a.get("town"),
                    "postalCode": a.get("postalCode"),
                    "country":    a.get("country", {}).get("isocode"),
                    "default":    a.get("defaultAddress", False),
                }
                for a in addrs
            ],
        }
    return {"success": False, "error": resp.text}


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 7 — Store / Misc
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_store_details(store_id: str) -> dict:
    """Get details of a physical store location by store ID."""
    url = f"{BASE_URL}/{SITE_ID}/stores/{store_id}"
    resp = _http.get(url, params={"fields": "FULL"})
    if resp.status_code == 200:
        return {"success": True, "store": resp.json()}
    return {"success": False, "error": resp.text}


@mcp.tool()
def find_stores_near(
    latitude: float,
    longitude: float,
    radius_km: int = 50,
) -> dict:
    """Find physical stores near a geographic location."""
    url = f"{BASE_URL}/{SITE_ID}/stores"
    resp = _http.get(url, params={
        "latitude":  latitude,
        "longitude": longitude,
        "radius":    radius_km * 1000,
        "fields":    "stores(name,address,openingHours)",
    })
    if resp.status_code == 200:
        return {"success": True, "stores": resp.json().get("stores", [])}
    return {"success": False, "error": resp.text}


@mcp.tool()
def server_health() -> dict:
    """
    Check connectivity between this MCP server and the SAP Commerce instance.
    Returns reachable=True/False and response time in ms.
    """
    import time
    try:
        start = time.time()
        r = _http.get(f"{BASE_URL}/{SITE_ID}/catalogs", timeout=5)
        ms = round((time.time() - start) * 1000)
        return {
            "success":     True,
            "reachable":   True,
            "status_code": r.status_code,
            "response_ms": ms,
            "sap_url":     BASE_URL,
            "site_id":     SITE_ID,
        }
    except Exception as e:
        return {"success": False, "reachable": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting SAP Commerce MCP Server")
    logger.info("  SAP URL   : %s", BASE_URL)
    logger.info("  Site ID   : %s", SITE_ID)
    logger.info("  Transport : %s", MCP_TRANSPORT)
    logger.info("  Listening : %s:%d", MCP_HOST, MCP_PORT)

    if MCP_TRANSPORT == "sse":
        asgi_app = mcp.sse_app()
        uvicorn.run(asgi_app, host=MCP_HOST, port=MCP_PORT, log_level="info")

    elif MCP_TRANSPORT in ("http", "streamable_http"):
        asgi_app = mcp.streamable_http_app()
        uvicorn.run(asgi_app, host=MCP_HOST, port=MCP_PORT, log_level="info")

    else:
        mcp.run()   # stdio