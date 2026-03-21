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


class QuickCheckoutPlaceRequest(BaseModel):
    session_id: str
    security_code: str = ""


@router.post("/prepare")
def quick_checkout_prepare(req: QuickCheckoutPrepareRequest):
    """Set delivery address + delivery mode on cart. Returns order summary for confirmation."""
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

    # Set delivery address
    if address:
        addr_result = sap_client.set_delivery_address(cart_id, address, access_token)
        if not addr_result.get("success"):
            return {"success": False, "step": "address", "error": addr_result.get("error", "Failed to set address")}
    else:
        return {"success": False, "step": "address", "error": "No delivery address selected"}

    # Set delivery mode
    mode_result = sap_client.set_delivery_mode(cart_id, "standard-gross", access_token)
    if not mode_result.get("success"):
        return {"success": False, "step": "delivery_mode", "error": mode_result.get("error", "Failed to set delivery mode")}

    # Fetch updated cart with totals
    cart_result = sap_client.get_cart(cart_id, access_token)
    if not cart_result.get("success"):
        return {"success": False, "step": "cart", "error": "Failed to fetch cart summary"}

    # Resolve selected payment for display
    payments = state.get("sap_payment_details") or []
    payment = payments[req.payment_index] if req.payment_index < len(payments) else None

    audit("CHECKOUT_PREPARED", req.session_id, {"cart_id": cart_id})

    return {
        "success": True,
        "cart": cart_result,
        "address": address,
        "payment": payment,
    }


@router.post("/place")
def quick_checkout_place(req: QuickCheckoutPlaceRequest):
    """Place the order directly via SAP Commerce."""
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[req.session_id]
    cart_id = state.get("cart_id")
    access_token = state.get("access_token", "")

    if not cart_id:
        raise HTTPException(status_code=400, detail="No cart in session")
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = sap_client.place_order(cart_id, access_token, security_code=req.security_code)

    if result.get("success"):
        state["order_code"] = result.get("order_code")
        state["cart_id"] = None
        _sessions[req.session_id] = state
        audit("ORDER_PLACED", req.session_id, {
            "order_code": result.get("order_code"),
            "total": result.get("total"),
        })

    return result
