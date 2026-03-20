"""Chat routes — message send, order approval."""

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.middleware.audit import audit
from app.services.agent_service import get_fallback, get_last_ai_message, new_session, run_turn

logger = logging.getLogger("sap_agent.api.chat")

router = APIRouter(prefix="/chat", tags=["Chat"])

# Session store reference — set by main app
_sessions: dict = {}


def set_session_store(sessions: dict) -> None:
    global _sessions
    _sessions = sessions


# ── Models ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    session_id: Optional[str] = None
    user_id: str = "anonymous"


class Suggestion(BaseModel):
    label: str
    value: str
    primary: bool = False


class Product(BaseModel):
    code: Optional[str] = None
    name: str
    price: Optional[str] = None
    price_value: Optional[float] = None
    stock: Optional[str] = None
    rating: Optional[float] = None
    summary: Optional[str] = None
    category: Optional[str] = None


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
    stripe_payment_url: Optional[str] = None
    checkout_status: Optional[str] = None
    suggestions: list[Suggestion] = []
    products: list[Product] = []


class ApprovalRequest(BaseModel):
    session_id: str
    approved: bool


# ── Reply post-processing ────────────────────────────────────────────────────

_SUGGESTIONS_RE = re.compile(
    r'\[SUGGESTIONS\]\s*(\{.*?\})\s*\[/SUGGESTIONS\]',
    re.DOTALL,
)


def _extract_suggestions(reply: str) -> tuple[str, list[Suggestion]]:
    """Strip the [SUGGESTIONS] block from reply text and return (clean_reply, suggestions)."""
    m = _SUGGESTIONS_RE.search(reply)
    if not m:
        return reply, []
    clean = reply[:m.start()].rstrip() + reply[m.end():]
    clean = clean.rstrip()
    try:
        data = json.loads(m.group(1))
        raw = data.get("suggestions", []) if isinstance(data, dict) else []
        suggestions = [
            Suggestion(
                label=s.get("label", "")[:40],
                value=s.get("value", s.get("label", "")),
                primary=bool(s.get("primary", False)),
            )
            for s in raw
            if isinstance(s, dict) and s.get("label")
        ]
        return clean, suggestions[:6]
    except (json.JSONDecodeError, TypeError, KeyError):
        logger.warning("Failed to parse [SUGGESTIONS] JSON: %s", m.group(1)[:200])
        return clean, []


def _extract_products(state: dict) -> list[Product]:
    """Extract structured product data from the last search tool results."""
    raw = state.get("last_search_results")
    if not raw or not isinstance(raw, list):
        return []
    products = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        products.append(Product(
            code=p.get("code"),
            name=p["name"],
            price=p.get("price"),
            price_value=p.get("priceValue"),
            stock=p.get("stock"),
            rating=p.get("rating"),
            summary=p.get("summary"),
        ))
    return products


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
    else:
        state, thread_id = new_session(req.user_id)
        _sessions[thread_id] = state

    try:
        new_state = run_turn(req.message, thread_id, state)
    except Exception as exc:
        logger.exception("chat | run_turn failed | session=%s", thread_id)
        audit("API_ERROR", thread_id, {"error": str(exc)})
        raise HTTPException(status_code=500, detail=get_fallback())

    _sessions[thread_id] = new_state
    raw_reply = get_last_ai_message(new_state) or "I'm working on that, one moment..."
    reply, suggestions = _extract_suggestions(raw_reply)
    products = _extract_products(new_state)
    awaiting = bool(new_state.get("__interrupt__"))
    is_auth = bool(new_state.get("access_token")) and new_state.get("user_id") == "current"

    # Clear search results after consuming so they don't repeat on next turn
    new_state["last_search_results"] = None

    return ChatResponse(
        session_id=thread_id,
        reply=reply,
        turn=new_state.get("turn_count", 0),
        tokens_used=new_state.get("total_input_tokens", 0),
        cart_id=new_state.get("cart_id"),
        order_code=new_state.get("order_code"),
        awaiting_approval=awaiting,
        username=new_state.get("username") if is_auth else None,
        authenticated=is_auth,
        stripe_payment_url=new_state.get("stripe_payment_url"),
        checkout_status=new_state.get("checkout_status"),
        suggestions=suggestions,
        products=products,
    )


@router.post("/approve")
def approve_order(req: ApprovalRequest):
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    state = _sessions[req.session_id]
    try:
        new_state = run_turn("", req.session_id, state,
                             approval_response={"approved": req.approved})
        _sessions[req.session_id] = new_state
    except Exception as exc:
        logger.exception("approve_order | failed | session=%s", req.session_id)
        raise HTTPException(status_code=500, detail=get_fallback())

    raw_reply = get_last_ai_message(new_state) or ""
    reply, suggestions = _extract_suggestions(raw_reply)
    return {
        "reply": reply,
        "order_code": new_state.get("order_code"),
        "session_id": req.session_id,
        "suggestions": [s.model_dump() for s in suggestions],
    }
