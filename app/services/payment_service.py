"""
Payment service — manages Stripe customers, saved cards, addresses, and charges.

Bridges session state with Stripe Customer/PaymentMethod objects.
Uses Redis for customer mapping and address storage.
"""

import logging
import uuid
from typing import Optional

from app.config import CONFIG
from app.integrations import redis_client, stripe_client

logger = logging.getLogger("sap_agent.payment_service")

_CUSTOMER_TTL = CONFIG.redis.order_history_ttl  # 30 days


# ── Stripe Customer Management ──────────────────────────────────────────────

def ensure_stripe_customer(email: str, name: Optional[str] = None) -> Optional[str]:
    """Get or create a Stripe Customer for this email. Returns customer_id."""
    # Check Redis cache first
    cached = redis_client.get_json(f"stripe_customer:{email}")
    if cached and cached.get("customer_id"):
        return cached["customer_id"]

    if not stripe_client.is_configured():
        logger.warning("Stripe not configured — cannot create customer")
        return None

    result = stripe_client.get_or_create_customer(email, name)
    if not result.get("success"):
        logger.error("Failed to create Stripe customer for %s: %s", email, result.get("error"))
        return None

    customer_id = result["customer_id"]
    redis_client.set_json(f"stripe_customer:{email}", {
        "customer_id": customer_id,
        "email": email,
        "name": name,
    }, ttl=_CUSTOMER_TTL)

    return customer_id


def get_stripe_customer_id(email: str) -> Optional[str]:
    """Look up existing Stripe customer ID from Redis cache."""
    cached = redis_client.get_json(f"stripe_customer:{email}")
    return cached.get("customer_id") if cached else None


# ── Card Management ─────────────────────────────────────────────────────────

def create_card_setup(email: str, name: Optional[str] = None) -> dict:
    """Create a SetupIntent for saving a card. Returns client_secret for Stripe Elements."""
    customer_id = ensure_stripe_customer(email, name)
    if not customer_id:
        return {"success": False, "error": "Could not create Stripe customer"}

    result = stripe_client.create_setup_intent(customer_id)
    if not result.get("success"):
        return result

    return {
        "success": True,
        "client_secret": result["client_secret"],
        "customer_id": customer_id,
    }


def list_saved_cards(email: str) -> list[dict]:
    """List saved payment methods for a user."""
    customer_id = get_stripe_customer_id(email)
    if not customer_id:
        return []

    result = stripe_client.list_payment_methods(customer_id)
    if not result.get("success"):
        return []

    return result.get("methods", [])


def remove_card(payment_method_id: str) -> bool:
    """Remove a saved card."""
    result = stripe_client.detach_payment_method(payment_method_id)
    return result.get("success", False)


# ── Charge Saved Card ───────────────────────────────────────────────────────

def charge_saved_card(customer_id: str, payment_method_id: str,
                      amount: int, currency: str = "usd",
                      metadata: Optional[dict] = None) -> dict:
    """
    Charge a saved card via PaymentIntent (off-session).

    amount: in minor currency units (cents for USD).
    Returns: {"success": True, "payment_intent_id": "pi_...", "status": "succeeded"}
    """
    result = stripe_client.create_payment_intent(
        customer_id=customer_id,
        payment_method_id=payment_method_id,
        amount=amount,
        currency=currency,
        metadata=metadata,
    )
    return result


def refund_charge(payment_intent_id: str) -> dict:
    """Refund a charge (used when SAP order fails after successful payment)."""
    return stripe_client.refund_payment_intent(payment_intent_id)


# ── Address Management ──────────────────────────────────────────────────────

def list_saved_addresses(email: str) -> list[dict]:
    """List saved addresses for a user."""
    data = redis_client.get_json(f"saved_addresses:{email}")
    return data if isinstance(data, list) else []


def save_address(email: str, address: dict) -> dict:
    """Save a new address. Returns the address with generated ID."""
    addresses = list_saved_addresses(email)
    address["id"] = address.get("id") or str(uuid.uuid4())[:8]
    addresses.append(address)
    redis_client.set_json(f"saved_addresses:{email}", addresses, ttl=_CUSTOMER_TTL)
    return address


def remove_address(email: str, address_id: str) -> bool:
    """Remove a saved address by ID."""
    addresses = list_saved_addresses(email)
    updated = [a for a in addresses if a.get("id") != address_id]
    if len(updated) == len(addresses):
        return False
    redis_client.set_json(f"saved_addresses:{email}", updated, ttl=_CUSTOMER_TTL)
    return True
