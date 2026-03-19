"""
api_server.py
=============
FastAPI wrapper around the production agent.

Routes:
  GET  /               — Chat UI (served from static/index.html)
  GET  /health         — Health check + circuit breaker status
  POST /chat           — Send a message, get agent reply
  POST /chat/approve   — Approve/reject a pending order confirmation
  WS   /chat/stream    — Real-time streaming via WebSocket
  GET  /docs           — Swagger UI (auto-generated)
"""

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from production_agent import (
    ShoppingState, get_fallback, get_last_ai_message,
    new_session, run_turn, sap_circuit_breaker, stream_turn,
)
from sap_commerce_tools import server_account_login
from security_layer import audit
from acp.routes import router as acp_router

logger = logging.getLogger("sap_agent.api")

app = FastAPI(title="SAP Commerce Shopping Agent", version="1.0.0")

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten to your domain in production
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# ── ACP (Agentic Commerce Protocol) ─────────────────────────────────────────
app.include_router(acp_router)

# ── Static / UI ───────────────────────────────────────────────────────────────
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_class=FileResponse, include_in_schema=False)
def root():
    """Serve the chat UI."""
    index = _STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return HTMLResponse(
        "<h2>SAP Commerce Agent</h2>"
        "<p>See <a href='/docs'>/docs</a> for the API.</p>"
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


# ── Session store (replace with Redis in production) ─────────────────────────
# Stores: session_id → ShoppingState
# thread_id == session_id — they are the same value, kept separate previously
# which caused the stored state to go stale.
_sessions: dict[str, ShoppingState] = {}


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    session_id: Optional[str] = None
    user_id: str = "anonymous"


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    turn: int
    tokens_used: int
    cart_id: Optional[str] = None
    order_code: Optional[str] = None
    awaiting_approval: bool = False
    username: Optional[str] = None
    authenticated: bool = False


class ApprovalRequest(BaseModel):
    session_id: str
    approved: bool


class LoginRequest(BaseModel):
    """
    Credentials are sent directly from the frontend over HTTPS to this
    endpoint — they never touch the LLM, conversation history, or logs.
    """
    username: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)
    session_id: Optional[str] = None   # attach login to an existing session


class LoginResponse(BaseModel):
    session_id: str
    username: str
    authenticated: bool
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "circuit_breaker": "open" if sap_circuit_breaker.is_open else "closed",
        "active_sessions": len(_sessions),
    }


@app.post("/auth/login", response_model=LoginResponse)
def login(req: LoginRequest):
    """
    Authenticate a user against SAP Commerce Cloud.

    Security design:
    - Credentials travel HTTPS frontend → this endpoint only.
    - They are used once to fetch a SAP OAuth token, then discarded.
    - The password is NEVER logged, stored, or passed to the LLM.
    - Only the access_token is kept (in server-side session state).
    - The LLM only sees "Authenticated: Yes" in its system prompt.
    """
    # Resolve or create a session
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
    else:
        state, thread_id = new_session(req.username)

    result = server_account_login(req.username, req.password)

    if not result.get("success"):
        audit("LOGIN_FAILED", thread_id, {"username": req.username})
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    # Inject token + user info directly into session state — no LLM involved
    state["access_token"] = result["access_token"]
    state["username"]     = result["username"]
    state["user_id"]      = "current"
    _sessions[thread_id]  = state

    audit("LOGIN_SUCCESS", thread_id, {"username": req.username})
    logger.info("login | success | session=%s | user=%s", thread_id, req.username)

    return LoginResponse(
        session_id=thread_id,
        username=req.username,
        authenticated=True,
        message=f"Welcome, {req.username}! You are now logged in.",
    )


@app.get("/auth/status")
def auth_status(session_id: str):
    """
    Returns the authentication state for a session.
    The frontend uses this to show/hide the Login button.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    state = _sessions[session_id]
    authenticated = bool(state.get("access_token")) and state.get("user_id") == "current"
    return {
        "session_id":    session_id,
        "authenticated": authenticated,
        "username":      state.get("username") if authenticated else None,
    }


@app.post("/auth/logout")
def logout(session_id: str):
    """
    Clears the access token from session state.
    The session itself persists so the cart (as guest) is preserved.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    state = _sessions[session_id]
    username = state.get("username", "unknown")
    state["access_token"] = None
    state["username"]     = None
    state["user_id"]      = "anonymous"
    _sessions[session_id] = state
    audit("LOGOUT", session_id, {"username": username})
    logger.info("logout | session=%s | user=%s", session_id, username)
    return {"session_id": session_id, "authenticated": False}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # ── Resolve or create session ────────────────────────────────────────────
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
        logger.debug("chat | resuming session=%s turn=%d", thread_id, state.get("turn_count", 0))
    else:
        state, thread_id = new_session(req.user_id)
        _sessions[thread_id] = state
        logger.info("chat | new session=%s user=%s", thread_id, req.user_id)

    # ── Run one agent turn ───────────────────────────────────────────────────
    try:
        new_state = run_turn(req.message, thread_id, state)
    except Exception as exc:
        logger.exception("chat | run_turn failed | session=%s", thread_id)
        audit("API_ERROR", thread_id, {"error": str(exc)})
        raise HTTPException(status_code=500, detail=get_fallback())

    # ── Persist updated state ────────────────────────────────────────────────
    _sessions[thread_id] = new_state

    # ── Serialise reply — get_last_ai_message always returns str ────────────
    reply = get_last_ai_message(new_state)
    if not reply:
        # Agent called tools but produced no visible text yet — happens when
        # tool calls are in flight and the graph hasn't returned to agent node.
        reply = "I'm working on that, one moment…"
        logger.debug("chat | no AI text in state yet | session=%s", thread_id)

    awaiting = bool(new_state.get("__interrupt__"))

    logger.info(
        "chat | session=%s | turn=%d | tokens=%d | awaiting_approval=%s",
        thread_id,
        new_state.get("turn_count", 0),
        new_state.get("total_input_tokens", 0),
        awaiting,
    )

    is_authenticated = bool(new_state.get("access_token")) and new_state.get("user_id") == "current"

    return ChatResponse(
        session_id=thread_id,
        reply=reply,
        turn=new_state.get("turn_count", 0),
        tokens_used=new_state.get("total_input_tokens", 0),
        cart_id=new_state.get("cart_id"),
        order_code=new_state.get("order_code"),
        awaiting_approval=awaiting,
        username=new_state.get("username") if is_authenticated else None,
        authenticated=is_authenticated,
    )


@app.post("/chat/approve")
def approve_order(req: ApprovalRequest):
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[req.session_id]
    thread_id = req.session_id

    logger.info("approve_order | session=%s | approved=%s", thread_id, req.approved)

    try:
        new_state = run_turn(
            user_message="",
            thread_id=thread_id,
            state=state,
            approval_response={"approved": req.approved},
        )
        _sessions[thread_id] = new_state
    except Exception as exc:
        logger.exception("approve_order | failed | session=%s", thread_id)
        audit("APPROVE_ERROR", thread_id, {"error": str(exc)})
        raise HTTPException(status_code=500, detail=get_fallback())

    return {
        "reply":       get_last_ai_message(new_state),
        "order_code":  new_state.get("order_code"),
        "session_id":  thread_id,
    }


@app.websocket("/chat/stream")
async def chat_stream(ws: WebSocket):
    await ws.accept()
    state, thread_id = new_session()
    _sessions[thread_id] = state
    await ws.send_json({"type": "session", "session_id": thread_id})
    logger.info("ws | new session=%s", thread_id)

    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            user_message = payload.get("message", "")
            if not user_message:
                continue

            logger.debug("ws | session=%s | message_len=%d", thread_id, len(user_message))

            async for chunk in stream_turn(user_message, thread_id, _sessions[thread_id]):
                await ws.send_json({"type": "chunk", "text": chunk})
            await ws.send_json({"type": "done"})

    except WebSocketDisconnect:
        audit("WS_DISCONNECT", thread_id, {})
        logger.info("ws | disconnected | session=%s", thread_id)
    except Exception as exc:
        logger.exception("ws | error | session=%s", thread_id)
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close()
