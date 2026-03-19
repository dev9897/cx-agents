"""
LangChain tools — thin wrappers over service/integration functions.

These are the tools the LLM can call. They delegate to the integrations layer.
"""

from typing import Optional

from langchain_core.tools import tool

from app.integrations import sap_client


@tool
def search_products(query: str, page_size: int = 5, current_page: int = 0,
                    sort: str = "relevance", access_token: Optional[str] = None) -> dict:
    """Search for products in SAP Commerce Cloud catalog.
    Returns a list of products with code, name, price, and stock info."""
    return sap_client.search_products(query, page_size, current_page, sort, access_token)


@tool
def get_product_details(product_code: str, access_token: Optional[str] = None) -> dict:
    """Get full product details by product code."""
    return sap_client.get_product_details(product_code, access_token)


@tool
def create_cart(access_token: str, user_id: str = "current") -> dict:
    """Create a new shopping cart for the authenticated user.
    Returns cart code to use in all subsequent cart operations."""
    return sap_client.create_cart(access_token, user_id)


@tool
def add_to_cart(cart_id: str, product_code: str, quantity: int = 1,
                access_token: str = "", user_id: str = "current") -> dict:
    """Add a product to the shopping cart. Requires cart_id and product_code."""
    return sap_client.add_to_cart(cart_id, product_code, quantity, access_token, user_id)


@tool
def get_cart(cart_id: str, access_token: str = "", user_id: str = "current") -> dict:
    """Retrieve current cart contents."""
    return sap_client.get_cart(cart_id, access_token, user_id)


@tool
def set_delivery_address(cart_id: str, address: dict,
                         access_token: str = "", user_id: str = "current") -> dict:
    """Set delivery address on the cart.
    address dict keys: firstName, lastName, line1, line2, town, postalCode, country (isocode e.g. 'DE')"""
    return sap_client.set_delivery_address(cart_id, address, access_token, user_id)


@tool
def set_delivery_mode(cart_id: str, delivery_mode_code: str = "standard-gross",
                      access_token: str = "", user_id: str = "current") -> dict:
    """Set delivery/shipping mode. Common codes: standard-gross, premium-gross."""
    return sap_client.set_delivery_mode(cart_id, delivery_mode_code, access_token, user_id)


@tool
def initiate_checkout(cart_id: str, access_token: str = "",
                      user_id: str = "current") -> dict:
    """
    Initiate secure checkout for the cart. Returns a payment URL.
    The user should click the payment URL to complete payment on Stripe's secure page.
    Do NOT ask the user for any payment information — Stripe handles that.
    """
    # Get cart details to build line items for Stripe
    cart = sap_client.get_cart(cart_id, access_token, user_id)
    if not cart.get("success"):
        return {"success": False, "error": "Could not retrieve cart for checkout"}

    from app.integrations import stripe_client
    if not stripe_client.is_configured():
        return {
            "success": True,
            "message": "Stripe is not configured. In production, this would redirect to a secure payment page.",
            "payment_url": None,
            "checkout_status": "stripe_not_configured",
        }

    line_items = []
    for entry in cart.get("entries", []):
        line_items.append({
            "name": entry.get("product_name", entry.get("product_code", "Item")),
            "amount": int(round(float(entry.get("basePrice", 0)) * 100)),
            "currency": cart.get("currency", "usd").lower(),
            "quantity": entry.get("quantity", 1),
        })

    result = stripe_client.create_checkout_session(
        line_items=line_items,
        metadata={"cart_id": cart_id, "user_id": user_id},
    )

    if result.get("success"):
        return {
            "success": True,
            "payment_url": result["url"],
            "stripe_session_id": result["session_id"],
            "message": "Please click the payment link to complete your purchase securely.",
        }
    return {"success": False, "error": result.get("error", "Checkout creation failed")}


@tool
def place_order(cart_id: str, access_token: str = "",
                user_id: str = "current", security_code: str = "") -> dict:
    """Place the order. Cart must have delivery address, delivery mode,
    and payment set before calling this. Returns order code and status."""
    return sap_client.place_order(cart_id, access_token, user_id, security_code)


@tool
def get_order(order_code: str, access_token: str = "", user_id: str = "current") -> dict:
    """Retrieve order details by order code."""
    return sap_client.get_order(order_code, access_token, user_id)


def get_direct_sap_tools() -> list:
    """Return all LangChain tools (used as MCP fallback)."""
    return [
        search_products, get_product_details,
        create_cart, add_to_cart, get_cart,
        set_delivery_address, set_delivery_mode,
        initiate_checkout, place_order, get_order,
    ]
