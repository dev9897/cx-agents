"""Authentication routes — login, status, logout."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.integrations.sap_client import server_account_login
from app.middleware.audit import audit
from app.services.agent_service import new_session

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
    authenticated: bool
    message: str


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
    _sessions[thread_id] = state

    audit("LOGIN_SUCCESS", thread_id, {"username": req.username})
    return LoginResponse(
        session_id=thread_id, username=req.username,
        authenticated=True, message=f"Welcome, {req.username}!",
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
