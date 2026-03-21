"""
Checkout routes — Stripe Checkout Session creation, webhook, status,
and direct quick checkout (2-click) for SAP Commerce.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.integrations import sap_client
from app.middleware.audit import audit
from app.services import checkout_service

logger = logging.getLogger("sap_agent.api.checkout")

router = APIRouter(prefix="/checkout", tags=["Checkout"])

_sessions: dict = {}


def set_session_store(sessions: dict) -> None:
    global _sessions
    _sessions = sessions


@router.post("/create")
def create_checkout(session_id: str):
    """Create a Stripe Checkout Session for the current cart."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[session_id]
    cart_id = state.get("cart_id")
    if not cart_id:
        raise HTTPException(status_code=400, detail="No cart in session")

    access_token = state.get("access_token", "")
    user_id = state.get("user_id", "current")
    email = None  # Could be fetched from user profile

    checkout = checkout_service.create_checkout(
        session_id=session_id,
        cart_id=cart_id,
        access_token=access_token,
        user_id=user_id,
        customer_email=email,
    )

    return {
        "checkout_id": checkout.id,
        "status": checkout.status.value,
        "payment_url": checkout.stripe_payment_url,
        "cart_summary": checkout.cart_summary.model_dump() if checkout.cart_summary else None,
        "error": checkout.error_message,
    }


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook endpoint — processes payment events."""
    from app.integrations import stripe_client

    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    result = stripe_client.construct_webhook_event(payload, sig_header)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    event = result["event"]
    logger.info("Stripe webhook: %s", event.type)

    if event.type == "checkout.session.completed":
        stripe_session_id = event.data.object.id
        checkout = checkout_service.handle_payment_success(stripe_session_id)
        if checkout:
            logger.info("Payment success → order %s", checkout.order_code)
        else:
            logger.error("Payment success but no checkout found for: %s", stripe_session_id)

    return {"status": "ok"}


@router.get("/status/{checkout_id}")
def get_checkout_status(checkout_id: str):
    """Poll the status of a checkout."""
    checkout = checkout_service.get_checkout_status(checkout_id)
    if not checkout:
        raise HTTPException(status_code=404, detail="Checkout not found")

    return {
        "checkout_id": checkout.id,
        "status": checkout.status.value,
        "order_code": checkout.order_code,
        "error": checkout.error_message,
        "cart_summary": checkout.cart_summary.model_dump() if checkout.cart_summary else None,
    }


@router.get("/success")
def checkout_success(session_id: str = ""):
    """Success redirect page after Stripe payment."""
    return {
        "status": "success",
        "message": "Payment completed! Your order is being processed.",
        "session_id": session_id,
    }


@router.get("/cancel")
def checkout_cancel():
    """Cancel redirect page."""
    return {
        "status": "canceled",
        "message": "Payment was canceled. Your cart is still saved.",
    }


# ── Quick Checkout (2-click, direct SAP calls) ─────────────────────────────

class QuickCheckoutPrepareRequest(BaseModel):
    session_id: str
    address_index: int = 0
    payment_index: int = 0
    payment_type: str = "sap"  # "sap" or "stripe"


class QuickCheckoutPlaceRequest(BaseModel):
    session_id: str
    payment_type: str = "sap"  # "sap" or "stripe"
    stripe_payment_method_id: Optional[str] = None
    security_code: str = ""


@router.post("/prepare")
def quick_checkout_prepare(req: QuickCheckoutPrepareRequest):
    """Set delivery address, delivery mode, and payment on cart. Returns order summary."""
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[req.session_id]
    cart_id = state.get("cart_id")
    access_token = state.get("access_token", "")

    if not cart_id:
        raise HTTPException(status_code=400, detail="No cart in session")
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Resolve selected address
    addresses = state.get("saved_addresses") or []
    address = addresses[req.address_index] if req.address_index < len(addresses) else None
    if not address:
        return {"success": False, "step": "address", "error": "No delivery address selected"}

    # Step 1: Set delivery address
    addr_result = sap_client.set_delivery_address(cart_id, address, access_token)
    if not addr_result.get("success"):
        return {"success": False, "step": "address", "error": addr_result.get("error", "Failed to set address")}

    # Step 2: Set delivery mode
    mode_result = sap_client.set_delivery_mode(cart_id, "standard-gross", access_token)
    if not mode_result.get("success"):
        return {"success": False, "step": "delivery_mode", "error": mode_result.get("error", "Failed to set delivery mode")}

    # Step 3: Set payment on cart
    if req.payment_type == "stripe":
        # Stripe flow: set placeholder payment on SAP cart (actual charge happens at /place)
        stripe_cards = state.get("saved_payment_methods") or []
        stripe_card = stripe_cards[req.payment_index] if req.payment_index < len(stripe_cards) else None
        pay_result = sap_client.set_payment_on_cart(cart_id, {}, address, access_token)
        if not pay_result.get("success"):
            return {"success": False, "step": "payment", "error": pay_result.get("error", "Failed to set payment")}
        payment_display = {
            "type": "stripe",
            "brand": stripe_card.get("brand", "") if stripe_card else "",
            "last4": stripe_card.get("last4", "") if stripe_card else "",
            "id": stripe_card.get("id", "") if stripe_card else "",
        }
    else:
        # SAP flow: set saved SAP payment details on cart
        sap_payments = state.get("sap_payment_details") or []
        sap_payment = sap_payments[req.payment_index] if req.payment_index < len(sap_payments) else None
        pay_result = sap_client.set_payment_on_cart(
            cart_id, sap_payment or {}, address, access_token)
        if not pay_result.get("success"):
            return {"success": False, "step": "payment", "error": pay_result.get("error", "Failed to set payment")}
        payment_display = {
            "type": "sap",
            "cardType": sap_payment.get("cardType", "") if sap_payment else "",
            "cardNumber": sap_payment.get("cardNumber", "") if sap_payment else "",
        }

    # Fetch updated cart with totals (now includes delivery cost)
    cart_result = sap_client.get_cart(cart_id, access_token)
    if not cart_result.get("success"):
        return {"success": False, "step": "cart", "error": "Failed to fetch cart summary"}

    audit("CHECKOUT_PREPARED", req.session_id, {"cart_id": cart_id, "payment_type": req.payment_type})

    return {
        "success": True,
        "cart": cart_result,
        "address": address,
        "payment": payment_display,
        "payment_type": req.payment_type,
    }


@router.post("/place")
def quick_checkout_place(req: QuickCheckoutPlaceRequest):
    """Place the order. For Stripe: charge card first, then place SAP order."""
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[req.session_id]
    cart_id = state.get("cart_id")
    access_token = state.get("access_token", "")

    if not cart_id:
        raise HTTPException(status_code=400, detail="No cart in session")
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Stripe flow: charge the saved card before placing SAP order
    if req.payment_type == "stripe" and req.stripe_payment_method_id:
        from app.services import payment_service

        user_email = state.get("user_email", "")
        stripe_customer_id = payment_service.get_stripe_customer_id(user_email) if user_email else None

        if not stripe_customer_id:
            return {"success": False, "error": "Stripe customer not found. Please add a card in Settings."}

        # Get cart total for charge amount
        cart_data = sap_client.get_cart(cart_id, access_token)
        total_value = cart_data.get("totalValue", 0) if cart_data.get("success") else 0
        if not total_value:
            return {"success": False, "error": "Cannot determine cart total for payment"}

        # Stripe amounts are in cents
        charge_amount = int(round(total_value * 100))
        charge_result = payment_service.charge_saved_card(
            customer_id=stripe_customer_id,
            payment_method_id=req.stripe_payment_method_id,
            amount=charge_amount,
            currency=cart_data.get("currency", "USD").lower(),
            metadata={"session_id": req.session_id, "cart_id": cart_id},
        )
        if not charge_result.get("success"):
            error_msg = charge_result.get("error", "Payment failed")
            return {"success": False, "error": f"Payment declined: {error_msg}"}

        logger.info("Stripe charge succeeded | PI=%s | session=%s",
                     charge_result.get("payment_intent_id"), req.session_id)

    # Place SAP order
    result = sap_client.place_order(cart_id, access_token, security_code=req.security_code)

    if result.get("success"):
        state["order_code"] = result.get("order_code")
        state["cart_id"] = None
        _sessions[req.session_id] = state
        audit("ORDER_PLACED", req.session_id, {
            "order_code": result.get("order_code"),
            "total": result.get("total"),
            "payment_type": req.payment_type,
        })

    return result
