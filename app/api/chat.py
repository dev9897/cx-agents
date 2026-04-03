"""Chat routes — message send, order approval, SSE streaming."""

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.middleware.audit import audit
from app.services.agent_service import (
    get_fallback, get_last_ai_message, new_session, run_turn, stream_turn_events,
)

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
    image_url: Optional[str] = None


class CartEntry(BaseModel):
    entry_number: int = 0
    product_code: str = ""
    product_name: str = ""
    quantity: int = 1
    base_price: Optional[str] = None
    base_price_value: Optional[float] = None
    total: Optional[str] = None
    total_value: Optional[float] = None
    image_url: Optional[str] = None


class Cart(BaseModel):
    cart_id: str = ""
    entries: list[CartEntry] = []
    item_count: int = 0
    sub_total: Optional[str] = None
    delivery_cost: Optional[str] = None
    total_tax: Optional[str] = None
    total: Optional[str] = None
    total_value: Optional[float] = None
    currency: str = "USD"


class ProductDetail(BaseModel):
    code: str = ""
    name: str = ""
    description: Optional[str] = None
    price: Optional[str] = None
    price_value: Optional[float] = None
    stock: Optional[str] = None
    rating: Optional[float] = None
    image_url: Optional[str] = None
    categories: list[str] = []


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
    cart: Optional[Cart] = None
    product_detail: Optional[ProductDetail] = None
    saved_addresses: list[dict] = []
    sap_payment_details: list[dict] = []


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
            image_url=p.get("image_url"),
        ))
    return products


def _extract_product_detail(state: dict) -> Optional[ProductDetail]:
    """Extract structured product detail from the last get_product_details tool result."""
    raw = state.get("last_product_detail")
    if not raw or not isinstance(raw, dict) or not raw.get("code"):
        return None
    return ProductDetail(
        code=raw.get("code", ""),
        name=raw.get("name", ""),
        description=raw.get("description"),
        price=raw.get("price"),
        price_value=raw.get("priceValue"),
        stock=raw.get("stock"),
        rating=raw.get("rating"),
        image_url=raw.get("image_url"),
        categories=raw.get("categories", []),
    )


def _extract_cart(state: dict) -> Optional[Cart]:
    """Extract structured cart data from last get_cart tool result."""
    raw = state.get("last_cart_data")
    if not raw or not isinstance(raw, dict) or not raw.get("cart_id"):
        return None
    entries = []
    for e in raw.get("entries", []):
        if not isinstance(e, dict):
            continue
        entries.append(CartEntry(
            entry_number=e.get("entry_number", 0),
            product_code=e.get("product_code", ""),
            product_name=e.get("product_name", ""),
            quantity=e.get("quantity", 1),
            base_price=e.get("base_price"),
            base_price_value=e.get("base_price_value"),
            total=e.get("total"),
            total_value=e.get("total_value"),
            image_url=e.get("image_url"),
        ))
    return Cart(
        cart_id=raw.get("cart_id", ""),
        entries=entries,
        item_count=raw.get("item_count", len(entries)),
        sub_total=raw.get("subTotal"),
        delivery_cost=raw.get("deliveryCost"),
        total_tax=raw.get("totalTax"),
        total=raw.get("total"),
        total_value=raw.get("totalValue"),
        currency=raw.get("currency", "USD"),
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("", response_model=ChatResponse)
def chat(req: ChatRequest):
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
    else:
        state, thread_id = new_session(req.user_id)
        _sessions[thread_id] = state

    prev_order_code = state.get("order_code")

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
    cart = _extract_cart(new_state)
    product_detail = _extract_product_detail(new_state)
    awaiting = bool(new_state.get("__interrupt__"))
    is_auth = bool(new_state.get("access_token")) and new_state.get("user_id") == "current"

    # Clear structured data after consuming so they don't repeat on next turn
    new_state["last_search_results"] = None
    new_state["last_cart_data"] = None
    new_state["last_product_detail"] = None

    return ChatResponse(
        session_id=thread_id,
        reply=reply,
        turn=new_state.get("turn_count", 0),
        tokens_used=new_state.get("total_input_tokens", 0),
        cart_id=new_state.get("cart_id"),
        # Only send order_code when it's newly placed this turn (not stale from previous)
        order_code=new_state.get("order_code") if new_state.get("order_code") != prev_order_code else None,
        awaiting_approval=awaiting,
        username=new_state.get("username") if is_auth else None,
        authenticated=is_auth,
        stripe_payment_url=new_state.get("stripe_payment_url"),
        checkout_status=new_state.get("checkout_status"),
        suggestions=suggestions,
        products=products,
        cart=cart,
        product_detail=product_detail,
        saved_addresses=new_state.get("saved_addresses") or [],
        sap_payment_details=new_state.get("sap_payment_details") or [],
    )


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming endpoint — streams thinking status + text tokens + final structured data."""
    if req.session_id and req.session_id in _sessions:
        state = _sessions[req.session_id]
        thread_id = req.session_id
    else:
        state, thread_id = new_session(req.user_id)
        _sessions[thread_id] = state

    prev_order_code = state.get("order_code")

    async def event_generator():
        full_reply = ""
        final_state = state

        async for event in stream_turn_events(req.message, thread_id, state):
            evt_type = event["event"]
            data = event["data"]

            if evt_type == "chunk":
                full_reply += data["text"]
                yield f"event: chunk\ndata: {json.dumps(data)}\n\n"
            elif evt_type == "status":
                yield f"event: status\ndata: {json.dumps(data)}\n\n"
            elif evt_type == "error":
                yield f"event: error\ndata: {json.dumps({'message': data})}\n\n"
                return
            elif evt_type == "state":
                final_state = data

        # Update session store with final state
        _sessions[thread_id] = final_state

        # Build structured response (same logic as POST /chat)
        if not full_reply:
            full_reply = get_last_ai_message(final_state) or ""
        reply, suggestions = _extract_suggestions(full_reply)
        products = _extract_products(final_state)
        cart = _extract_cart(final_state)
        product_detail = _extract_product_detail(final_state)
        awaiting = bool(final_state.get("__interrupt__"))
        is_auth = bool(final_state.get("access_token")) and final_state.get("user_id") == "current"

        # Clear structured data after consuming
        final_state["last_search_results"] = None
        final_state["last_cart_data"] = None
        final_state["last_product_detail"] = None

        done_data = {
            "session_id": thread_id,
            "reply": reply,
            "turn": final_state.get("turn_count", 0),
            "tokens_used": final_state.get("total_input_tokens", 0),
            "cart_id": final_state.get("cart_id"),
            "order_code": final_state.get("order_code") if final_state.get("order_code") != prev_order_code else None,
            "awaiting_approval": awaiting,
            "username": final_state.get("username") if is_auth else None,
            "authenticated": is_auth,
            "suggestions": [s.model_dump() for s in suggestions],
            "products": [p.model_dump() for p in products],
            "cart": cart.model_dump() if cart else None,
            "product_detail": product_detail.model_dump() if product_detail else None,
            "saved_addresses": final_state.get("saved_addresses") or [],
            "sap_payment_details": final_state.get("sap_payment_details") or [],
        }
        yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
