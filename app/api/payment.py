"""
Payment routes — card management, address management, Stripe config.

These endpoints power the Settings panel where users save cards and addresses.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import CONFIG
from app.services import payment_service

logger = logging.getLogger("sap_agent.api.payment")

router = APIRouter(prefix="/payment", tags=["Payment"])

_sessions: dict = {}


def set_session_store(sessions: dict) -> None:
    global _sessions
    _sessions = sessions


# ── Request Models ──────────────────────────────────────────────────────────

class SetupIntentRequest(BaseModel):
    session_id: str
    email: str
    name: Optional[str] = None


class SaveAddressRequest(BaseModel):
    session_id: str
    email: str
    address: dict


class RemoveCardRequest(BaseModel):
    session_id: str


# ── Config (publishable key for frontend) ───────────────────────────────────

@router.get("/config")
def get_payment_config():
    """Return Stripe publishable key for frontend Stripe.js initialization."""
    return {
        "publishable_key": CONFIG.stripe.publishable_key,
        "configured": bool(CONFIG.stripe.publishable_key and CONFIG.stripe.secret_key),
    }


# ── SetupIntent (card saving) ──────────────────────────────────────────────

@router.post("/setup-intent")
def create_setup_intent(req: SetupIntentRequest):
    """Create a SetupIntent for saving a card via Stripe Elements."""
    result = payment_service.create_card_setup(req.email, req.name)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Setup failed"))
    return {
        "client_secret": result["client_secret"],
        "customer_id": result["customer_id"],
    }


# ── Card Management ─────────────────────────────────────────────────────────

@router.get("/cards")
def list_cards(email: str):
    """List saved payment methods for a user."""
    cards = payment_service.list_saved_cards(email)
    return {"cards": cards}


@router.delete("/cards/{payment_method_id}")
def remove_card(payment_method_id: str):
    """Remove a saved card."""
    ok = payment_service.remove_card(payment_method_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not remove card")
    return {"success": True}


# ── Address Management ──────────────────────────────────────────────────────

@router.get("/addresses")
def list_addresses(email: str):
    """List saved addresses for a user."""
    addresses = payment_service.list_saved_addresses(email)
    return {"addresses": addresses}


@router.post("/addresses")
def save_address(req: SaveAddressRequest):
    """Save a new address."""
    address = payment_service.save_address(req.email, req.address)
    return {"success": True, "address": address}


@router.delete("/addresses/{address_id}")
def remove_address(address_id: str, email: str):
    """Remove a saved address."""
    ok = payment_service.remove_address(email, address_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Address not found")
    return {"success": True}
