"""
Stripe integration — Checkout Sessions, webhooks, payment status.

Card data NEVER touches our server. Stripe handles all PCI-sensitive operations.
"""

import logging
from typing import Optional

from app.config import CONFIG

logger = logging.getLogger("sap_agent.stripe")

_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is None:
        import stripe
        stripe.api_key = CONFIG.stripe.secret_key
        _stripe = stripe
        logger.info("Stripe client initialised")
    return _stripe


def is_configured() -> bool:
    return bool(CONFIG.stripe.secret_key)


def create_checkout_session(
    line_items: list[dict],
    customer_email: Optional[str] = None,
    metadata: Optional[dict] = None,
    success_url: Optional[str] = None,
    cancel_url: Optional[str] = None,
) -> dict:
    """
    Create a Stripe Checkout Session.

    line_items format: [{"name": "...", "amount": 57488, "currency": "usd", "quantity": 1}]
    amount is in cents.

    Returns: {"success": True, "session_id": "cs_...", "url": "https://checkout.stripe.com/..."}
    """
    stripe = _get_stripe()

    stripe_items = []
    for item in line_items:
        stripe_items.append({
            "price_data": {
                "currency": item.get("currency", "usd"),
                "product_data": {"name": item["name"]},
                "unit_amount": item["amount"],
            },
            "quantity": item.get("quantity", 1),
        })

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=stripe_items,
            mode="payment",
            customer_email=customer_email,
            metadata=metadata or {},
            success_url=success_url or CONFIG.stripe.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url or CONFIG.stripe.cancel_url,
        )
        logger.info("Stripe checkout session created: %s", session.id)
        return {
            "success": True,
            "session_id": session.id,
            "url": session.url,
        }
    except Exception as e:
        logger.exception("Stripe create_checkout_session failed")
        return {"success": False, "error": str(e)}


def get_session_status(stripe_session_id: str) -> dict:
    """Check the payment status of a Stripe Checkout Session."""
    stripe = _get_stripe()
    try:
        session = stripe.checkout.Session.retrieve(stripe_session_id)
        return {
            "success": True,
            "status": session.payment_status,  # "paid", "unpaid", "no_payment_required"
            "amount_total": session.amount_total,
            "currency": session.currency,
            "customer_email": session.customer_details.email if session.customer_details else None,
            "metadata": dict(session.metadata) if session.metadata else {},
        }
    except Exception as e:
        logger.exception("Stripe get_session_status failed: %s", stripe_session_id)
        return {"success": False, "error": str(e)}


def construct_webhook_event(payload: bytes, sig_header: str) -> dict:
    """Verify and construct a Stripe webhook event."""
    stripe = _get_stripe()
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, CONFIG.stripe.webhook_secret,
        )
        return {"success": True, "event": event}
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature verification failed")
        return {"success": False, "error": "Invalid signature"}
    except Exception as e:
        logger.exception("Stripe webhook construction failed")
        return {"success": False, "error": str(e)}
