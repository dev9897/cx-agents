"""Authentication routes — login, status, logout."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.integrations.mcp_client import call_mcp_tool_sync
from app.integrations.sap_client import (
    server_account_login, get_user_addresses, get_user_payment_details,
)
from app.middleware.audit import audit
from app.services.agent_service import new_session, update_session_auth

logger = logging.getLogger("sap_agent.api.auth")

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Session store reference — set by main app
_sessions: dict = {}


def set_session_store(sessions: dict) -> None:
    global _sessions
    _sessions = sessions


# ── Models ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)
    session_id: Optional[str] = None


class LoginResponse(BaseModel):
    session_id: str
    username: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    authenticated: bool
    message: str
    saved_addresses: list[dict] = []
    sap_payment_details: list[dict] = []


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
    else:
        state, thread_id = new_session(req.username)

    result = server_account_login(req.username, req.password)
    if not result.get("success"):
        audit("LOGIN_FAILED", thread_id, {"username": req.username})
        raise HTTPException(status_code=401, detail="Invalid username or password")

    state["access_token"] = result["access_token"]
    state["username"] = result["username"]
    state["user_id"] = "current"
    state["user_email"] = result.get("email", "")

    # Register token in MCP vault so agent tools can resolve it
    mcp_session_id = None
    mcp_result = call_mcp_tool_sync("account_login", {
        "username": req.username,
        "password": req.password,
    })
    if mcp_result.get("success"):
        mcp_session_id = mcp_result["session_id"]
        state["mcp_session_id"] = mcp_session_id
        logger.info("MCP vault session created | thread=%s | mcp_session=%s", thread_id, mcp_session_id)
    else:
        logger.warning("MCP vault login failed: %s | thread=%s", mcp_result.get("error"), thread_id)

    # Pre-load saved cards so agent knows about them immediately
    saved_cards = []
    user_email = result.get("email", "")
    if user_email:
        try:
            from app.services import payment_service
            saved_cards = payment_service.list_saved_cards(user_email)
            state["saved_payment_methods"] = saved_cards
            logger.info("Loaded %d saved cards for %s", len(saved_cards), user_email)
        except Exception as e:
            logger.warning("Failed to load saved cards: %s", e)

    # Pre-load SAP addresses and payment details for 2-click checkout
    saved_addresses = []
    sap_payments = []
    access_token = result["access_token"]
    try:
        addr_result = get_user_addresses(access_token)
        if addr_result.get("success"):
            saved_addresses = addr_result.get("addresses", [])
            state["saved_addresses"] = saved_addresses
            logger.info("Loaded %d SAP addresses for %s", len(saved_addresses), user_email)
    except Exception as e:
        logger.warning("Failed to load SAP addresses: %s", e)

    try:
        pay_result = get_user_payment_details(access_token)
        if pay_result.get("success"):
            sap_payments = pay_result.get("payments", [])
            state["sap_payment_details"] = sap_payments
            logger.info("Loaded %d SAP payment methods for %s", len(sap_payments), user_email)
    except Exception as e:
        logger.warning("Failed to load SAP payment details: %s", e)

    _sessions[thread_id] = state

    # Update LangGraph checkpoint so graph nodes see the new token + MCP session + cards
    update_session_auth(
        thread_id,
        access_token=access_token,
        username=result["username"],
        email=user_email,
        mcp_session_id=mcp_session_id,
        saved_payment_methods=saved_cards or None,
        saved_addresses=saved_addresses or None,
        sap_payment_details=sap_payments or None,
    )

    audit("LOGIN_SUCCESS", thread_id, {"username": req.username})
    return LoginResponse(
        session_id=thread_id, username=req.username,
        email=result.get("email"),
        first_name=result.get("first_name"),
        last_name=result.get("last_name"),
        authenticated=True, message=f"Welcome, {req.username}!",
        saved_addresses=saved_addresses,
        sap_payment_details=sap_payments,
    )


@router.get("/status")
def auth_status(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    state = _sessions[session_id]
    authenticated = bool(state.get("access_token")) and state.get("user_id") == "current"
    return {
        "session_id": session_id,
        "authenticated": authenticated,
        "username": state.get("username") if authenticated else None,
    }


@router.post("/logout")
def logout(session_id: str):
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    state = _sessions[session_id]
    username = state.get("username", "unknown")
    state["access_token"] = None
    state["username"] = None
    state["user_id"] = "anonymous"
    _sessions[session_id] = state
    audit("LOGOUT", session_id, {"username": username})
    return {"session_id": session_id, "authenticated": False}
