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


@tool
def list_saved_cards(user_email: str = "") -> dict:
    """List the user's saved payment cards.
    Returns card IDs, brand, and last 4 digits.
    If no cards are saved, suggest the user add a card in Settings."""
    from app.services import payment_service
    if not user_email:
        return {"success": True, "cards": [], "message": "No email provided — cannot look up cards."}
    cards = payment_service.list_saved_cards(user_email)
    return {"success": True, "cards": cards}


@tool
def acp_checkout(
    cart_id: str,
    payment_method_id: str,
    buyer_first_name: str,
    buyer_last_name: str,
    buyer_email: str,
    address_first_name: str,
    address_last_name: str,
    address_line1: str,
    address_city: str,
    address_postal_code: str,
    address_country: str = "US",
    address_line2: str = "",
    delivery_mode: str = "standard-gross",
    access_token: str = "",
    user_id: str = "current",
) -> dict:
    """
    Complete a one-click purchase using a saved payment card via ACP.
    This charges the saved card and places the SAP order in one step.
    The user MUST have a saved card (payment_method_id like pm_xxx from list_saved_cards).
    You MUST get explicit user confirmation before calling this tool.
    """
    from acp.models import (
        ACPAddress, Buyer, Item, PaymentData,
    )
    from acp import service as acp_service

    # 1. Get cart to build ACP items
    cart = sap_client.get_cart(cart_id, access_token, user_id)
    if not cart.get("success"):
        return {"success": False, "error": "Could not retrieve cart"}

    items = []
    for entry in cart.get("entries", []):
        items.append(Item(
            id=entry.get("product_code", ""),
            quantity=entry.get("quantity", 1),
        ))

    if not items:
        return {"success": False, "error": "Cart is empty"}

    buyer = Buyer(
        first_name=buyer_first_name,
        last_name=buyer_last_name,
        full_name=f"{buyer_first_name} {buyer_last_name}",
        email=buyer_email,
    )

    address = ACPAddress(
        name=f"{address_first_name} {address_last_name}",
        line_one=address_line1,
        line_two=address_line2,
        city=address_city,
        postal_code=address_postal_code,
        country=address_country,
    )

    # 2. Create ACP checkout session
    try:
        session = acp_service.create_checkout_session(items, buyer, address)
    except Exception as e:
        return {"success": False, "error": f"Failed to create checkout session: {e}"}

    session_id = session.id

    # 3. Set delivery mode
    try:
        acp_service.update_checkout_session(
            session_id, fulfillment_option_id=delivery_mode,
        )
    except Exception as e:
        return {"success": False, "error": f"Failed to set delivery mode: {e}"}

    # 4. Complete with payment
    payment_data = PaymentData(token=payment_method_id)

    try:
        result = acp_service.complete_checkout(session_id, buyer, payment_data)
    except Exception as e:
        return {"success": False, "error": f"Checkout failed: {e}"}

    if result.status.value == "completed" and result.order:
        return {
            "success": True,
            "order_code": result.order.id,
            "order_url": result.order.permalink_url or "",
            "message": f"Order {result.order.id} placed successfully!",
        }

    # Extract error messages
    errors = [m.message for m in (result.messages or []) if m.message]
    return {
        "success": False,
        "error": errors[0] if errors else "Checkout did not complete",
        "status": result.status.value,
    }


def get_direct_sap_tools() -> list:
    """Return all LangChain tools (used as MCP fallback)."""
    return [
        search_products, get_product_details,
        create_cart, add_to_cart, get_cart,
        set_delivery_address, set_delivery_mode,
        initiate_checkout, place_order, get_order,
        list_saved_cards, acp_checkout,
    ]
