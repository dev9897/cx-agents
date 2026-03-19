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


# ── Customer Management ─────────────────────────────────────────────────────

def create_customer(email: str, name: Optional[str] = None,
                    metadata: Optional[dict] = None) -> dict:
    """Create a Stripe Customer object."""
    stripe = _get_stripe()
    try:
        params = {"email": email, "metadata": metadata or {}}
        if name:
            params["name"] = name
        customer = stripe.Customer.create(**params)
        logger.info("Stripe customer created: %s for %s", customer.id, email)
        return {"success": True, "customer_id": customer.id}
    except Exception as e:
        logger.exception("Stripe create_customer failed")
        return {"success": False, "error": str(e)}


def get_or_create_customer(email: str, name: Optional[str] = None) -> dict:
    """Idempotent: find existing customer by email, or create one."""
    stripe = _get_stripe()
    try:
        existing = stripe.Customer.list(email=email, limit=1)
        if existing.data:
            cust = existing.data[0]
            logger.info("Stripe customer found: %s for %s", cust.id, email)
            return {"success": True, "customer_id": cust.id}
        return create_customer(email, name)
    except Exception as e:
        logger.exception("Stripe get_or_create_customer failed")
        return {"success": False, "error": str(e)}


# ── SetupIntent (card saving) ───────────────────────────────────────────────

def create_setup_intent(customer_id: str) -> dict:
    """Create a SetupIntent for client-side card collection via Stripe Elements."""
    stripe = _get_stripe()
    try:
        intent = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
        )
        logger.info("SetupIntent created: %s for customer %s", intent.id, customer_id)
        return {
            "success": True,
            "setup_intent_id": intent.id,
            "client_secret": intent.client_secret,
        }
    except Exception as e:
        logger.exception("Stripe create_setup_intent failed")
        return {"success": False, "error": str(e)}


# ── PaymentMethod Management ────────────────────────────────────────────────

def list_payment_methods(customer_id: str) -> dict:
    """List saved cards for a customer."""
    stripe = _get_stripe()
    try:
        methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
        cards = []
        for pm in methods.data:
            card = pm.card
            cards.append({
                "id": pm.id,
                "brand": card.brand,
                "last4": card.last4,
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
            })
        return {"success": True, "methods": cards}
    except Exception as e:
        logger.exception("Stripe list_payment_methods failed")
        return {"success": False, "error": str(e)}


def detach_payment_method(payment_method_id: str) -> dict:
    """Remove a saved card from its customer."""
    stripe = _get_stripe()
    try:
        stripe.PaymentMethod.detach(payment_method_id)
        logger.info("PaymentMethod detached: %s", payment_method_id)
        return {"success": True}
    except Exception as e:
        logger.exception("Stripe detach_payment_method failed")
        return {"success": False, "error": str(e)}


# ── PaymentIntent (charge saved card) ───────────────────────────────────────

def create_payment_intent(customer_id: str, payment_method_id: str,
                          amount: int, currency: str = "usd",
                          metadata: Optional[dict] = None) -> dict:
    """
    Charge a saved card off-session.

    amount is in minor units (cents for USD).
    Returns payment_intent_id and status.
    """
    stripe = _get_stripe()
    try:
        intent = stripe.PaymentIntent.create(
            customer=customer_id,
            payment_method=payment_method_id,
            amount=amount,
            currency=currency,
            off_session=True,
            confirm=True,
            metadata=metadata or {},
        )
        logger.info("PaymentIntent created: %s status=%s amount=%d %s",
                     intent.id, intent.status, amount, currency)
        return {
            "success": True,
            "payment_intent_id": intent.id,
            "status": intent.status,  # "succeeded", "requires_action", etc.
            "amount": intent.amount,
            "currency": intent.currency,
        }
    except Exception as e:
        error_msg = str(e)
        # Check for 3DS/SCA requirement
        if hasattr(e, 'error') and hasattr(e.error, 'payment_intent'):
            pi = e.error.payment_intent
            if pi.status == "requires_action":
                logger.warning("PaymentIntent requires 3DS: %s", pi.id)
                return {
                    "success": False,
                    "error": "requires_3ds",
                    "payment_intent_id": pi.id,
                    "client_secret": pi.client_secret,
                }
        logger.exception("Stripe create_payment_intent failed")
        return {"success": False, "error": error_msg}


def refund_payment_intent(payment_intent_id: str) -> dict:
    """Refund a payment intent (used when SAP order placement fails after charge)."""
    stripe = _get_stripe()
    try:
        refund = stripe.Refund.create(payment_intent=payment_intent_id)
        logger.info("Refund created: %s for PI %s", refund.id, payment_intent_id)
        return {"success": True, "refund_id": refund.id, "status": refund.status}
    except Exception as e:
        logger.exception("Stripe refund failed for PI %s", payment_intent_id)
        return {"success": False, "error": str(e)}
