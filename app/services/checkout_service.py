"""
Checkout service — orchestrates the Stripe checkout flow.

Handles: create payment session, process webhooks, trigger SAP order placement.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.integrations import redis_client, sap_client, stripe_client
from app.middleware.audit import audit
from app.models.checkout import CartItem, CartSummary, CheckoutSession, CheckoutStatus

logger = logging.getLogger("sap_agent.checkout_service")

# In-memory fallback for checkout sessions
_checkout_sessions: dict[str, CheckoutSession] = {}


def _save_checkout(session: CheckoutSession) -> None:
    key = f"checkout:{session.id}"
    redis_client.set_json(key, session.model_dump(), ttl=3600)
    _checkout_sessions[session.id] = session


def _load_checkout(checkout_id: str) -> Optional[CheckoutSession]:
    data = redis_client.get_json(f"checkout:{checkout_id}")
    if data:
        return CheckoutSession(**data)
    return _checkout_sessions.get(checkout_id)


def create_checkout(
    session_id: str,
    cart_id: str,
    access_token: str,
    user_id: str = "current",
    customer_email: Optional[str] = None,
) -> CheckoutSession:
    """
    Create a Stripe Checkout Session from a SAP cart.

    1. Fetch cart from SAP
    2. Build Stripe line items
    3. Create Stripe Checkout Session
    4. Return checkout session with payment URL
    """
    checkout_id = f"chk_{uuid.uuid4().hex[:16]}"
    checkout = CheckoutSession(
        id=checkout_id,
        session_id=session_id,
        cart_id=cart_id,
        status=CheckoutStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    # Fetch cart from SAP
    cart = sap_client.get_cart(cart_id, access_token, user_id)
    if not cart.get("success"):
        checkout.status = CheckoutStatus.FAILED
        checkout.error_message = "Could not retrieve cart"
        _save_checkout(checkout)
        return checkout

    # Build cart summary
    items = []
    for entry in cart.get("entries", []):
        items.append(CartItem(
            product_code=entry.get("product_code", ""),
            product_name=entry.get("product_name", ""),
            quantity=entry.get("quantity", 1),
            unit_price=float(entry.get("basePrice", 0)),
            total_price=float(entry.get("totalValue", 0)),
        ))

    checkout.cart_summary = CartSummary(
        cart_id=cart_id,
        items=items,
        subtotal=float(cart.get("subTotal", 0)),
        shipping_cost=float(cart.get("deliveryCost", 0) or 0),
        tax=float(cart.get("totalTax", 0) or 0),
        total=float(cart.get("totalValue", 0) or 0),
        currency=cart.get("currency", "USD"),
    )

    # Create Stripe Checkout Session
    if not stripe_client.is_configured():
        checkout.status = CheckoutStatus.PAYMENT_CREATED
        checkout.error_message = "Stripe not configured — payment simulation mode"
        _save_checkout(checkout)
        logger.info("Checkout %s created (Stripe not configured)", checkout_id)
        return checkout

    stripe_items = []
    for item in items:
        stripe_items.append({
            "name": item.product_name or item.product_code,
            "amount": int(round(item.unit_price * 100)),
            "currency": checkout.cart_summary.currency.lower(),
            "quantity": item.quantity,
        })

    result = stripe_client.create_checkout_session(
        line_items=stripe_items,
        customer_email=customer_email,
        metadata={
            "checkout_id": checkout_id,
            "cart_id": cart_id,
            "session_id": session_id,
            "user_id": user_id,
        },
    )

    if result.get("success"):
        checkout.stripe_session_id = result["session_id"]
        checkout.stripe_payment_url = result["url"]
        checkout.status = CheckoutStatus.PAYMENT_CREATED
        audit("CHECKOUT_CREATED", session_id, {"checkout_id": checkout_id})
    else:
        checkout.status = CheckoutStatus.FAILED
        checkout.error_message = result.get("error", "Stripe session creation failed")

    _save_checkout(checkout)
    return checkout


def handle_payment_success(stripe_session_id: str) -> Optional[CheckoutSession]:
    """
    Called by Stripe webhook when payment succeeds.
    Places the order in SAP.
    """
    # Find checkout by Stripe session ID
    checkout = _find_by_stripe_session(stripe_session_id)
    if not checkout:
        logger.error("No checkout found for Stripe session: %s", stripe_session_id)
        return None

    checkout.status = CheckoutStatus.PAYMENT_COMPLETED
    checkout.updated_at = datetime.now(timezone.utc)

    # Place order in SAP
    # Get access token from the session (stored in Redis)
    token = _get_session_token(checkout.session_id)
    if not token:
        checkout.status = CheckoutStatus.FAILED
        checkout.error_message = "No access token available for order placement"
        _save_checkout(checkout)
        return checkout

    order_result = sap_client.place_order(checkout.cart_id, token)
    if order_result.get("success"):
        checkout.order_code = order_result["order_code"]
        checkout.status = CheckoutStatus.ORDER_PLACED
        audit("ORDER_PLACED_VIA_STRIPE", checkout.session_id, {
            "order_code": checkout.order_code,
            "checkout_id": checkout.id,
        })
    else:
        checkout.status = CheckoutStatus.FAILED
        checkout.error_message = order_result.get("error", "SAP order placement failed")
        checkout.retry_count += 1

    _save_checkout(checkout)
    return checkout


def get_checkout_status(checkout_id: str) -> Optional[CheckoutSession]:
    """Get the current status of a checkout."""
    return _load_checkout(checkout_id)


def _find_by_stripe_session(stripe_session_id: str) -> Optional[CheckoutSession]:
    """Find a checkout by its Stripe session ID."""
    # Check in-memory first
    for checkout in _checkout_sessions.values():
        if checkout.stripe_session_id == stripe_session_id:
            return checkout
    # Check Redis
    keys = redis_client.keys_by_pattern("checkout:*")
    for key in keys:
        data = redis_client.get_json(key)
        if data and data.get("stripe_session_id") == stripe_session_id:
            return CheckoutSession(**data)
    return None


def _get_session_token(session_id: str) -> Optional[str]:
    """Retrieve the SAP access token for a session."""
    data = redis_client.get_json(f"session_token:{session_id}")
    if data:
        return data.get("access_token")
    return None
