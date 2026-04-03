"""
Agent service — session management, conversation turns, streaming.

This is the main entry point for running the shopping agent.
"""

import logging
import os
import uuid
from typing import AsyncIterator, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from app.agent.graph import production_graph, _MCP_SESSION_ID
from app.agent.state import ShoppingState
from app.middleware.audit import audit
from app.middleware.error_handler import is_ssl_error, log_ssl_error, sap_circuit_breaker
from app.middleware.logging_config import set_trace_context
from app.middleware.security import detect_prompt_injection, rate_limiter, sanitise_input

logger = logging.getLogger("sap_agent.agent_service")


def new_session(user_id: str = "anonymous") -> tuple[ShoppingState, str]:
    """Create a fresh session with a bootstrapped SAP token."""
    thread_id = str(uuid.uuid4())
    set_trace_context(thread_id)

    static_token = os.getenv("SAP_STATIC_TOKEN", "")
    if static_token:
        logger.warning("Using SAP_STATIC_TOKEN — dev mode only | session=%s", thread_id)
        access_token = static_token
        resolved_user_id = "current"
        resolved_username = os.getenv("SAP_STATIC_USERNAME", "lang-graph-user")
    else:
        access_token = ""
        resolved_user_id = user_id
        resolved_username = None
        logger.warning("No SAP token available | session=%s", thread_id)

    init_state = ShoppingState(
        messages=[],
        access_token=access_token,
        user_id=resolved_user_id,
        cart_id=None,
        order_code=None,
        username=resolved_username,
        user_email=None,
        mcp_session_id=_MCP_SESSION_ID,
        stripe_checkout_session_id=None,
        stripe_payment_url=None,
        checkout_status=None,
        saved_payment_methods=None,
        saved_addresses=None,
        sap_payment_details=None,
        last_search_results=None,
        last_cart_data=None,
        last_product_detail=None,
        session_id=thread_id,
        total_input_tokens=0,
        total_output_tokens=0,
        turn_count=0,
        last_error=None,
        consecutive_errors=0,
    )

    audit("SESSION_START", thread_id, {
        "user_id": resolved_user_id,
        "token_ok": bool(access_token and len(access_token) > 20),
    })
    return init_state, thread_id


def update_session_auth(thread_id: str, access_token: str, username: str,
                        user_id: str = "current", email: str = "",
                        mcp_session_id: Optional[str] = None,
                        saved_payment_methods: Optional[list] = None,
                        saved_addresses: Optional[list] = None,
                        sap_payment_details: Optional[list] = None) -> None:
    """Update the LangGraph checkpoint with auth credentials after login."""
    lg_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    update = {
        "access_token": access_token,
        "username": username,
        "user_id": user_id,
        "user_email": email,
    }
    if mcp_session_id:
        update["mcp_session_id"] = mcp_session_id
    if saved_payment_methods is not None:
        update["saved_payment_methods"] = saved_payment_methods
    if saved_addresses is not None:
        update["saved_addresses"] = saved_addresses
    if sap_payment_details is not None:
        update["sap_payment_details"] = sap_payment_details
    try:
        production_graph.update_state(lg_config, update)
        logger.info("Checkpoint updated with auth | thread=%s | user=%s | mcp_session=%s | cards=%d",
                    thread_id, username, mcp_session_id, len(saved_payment_methods or []))
    except Exception:
        # No checkpoint yet — that's fine, next invoke will create it with the token
        logger.debug("No checkpoint to update yet | thread=%s", thread_id)


def run_turn(user_message: str, thread_id: str, state: ShoppingState,
             approval_response: Optional[dict] = None) -> ShoppingState:
    """Run one conversation turn with security + observability."""
    trace_id = set_trace_context(thread_id)
    logger.info("turn_start | msg_len=%d | turn=%d",
                len(user_message), state.get("turn_count", 0))

    # Security checks
    is_malicious, reason = detect_prompt_injection(user_message)
    if is_malicious:
        audit("INJECTION_BLOCKED", thread_id, {"reason": reason})
        state["messages"] = state.get("messages", []) + [
            HumanMessage(content=user_message),
            AIMessage(content="I couldn't process that request. Please rephrase."),
        ]
        return state

    ok, reason = rate_limiter.check_message(thread_id)
    if not ok:
        audit("RATE_LIMITED", thread_id, {})
        state["messages"] = state.get("messages", []) + [
            AIMessage(content="You're sending messages too quickly. Please slow down.")
        ]
        return state

    clean = sanitise_input(user_message)
    lg_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    try:
        if approval_response:
            new_state = production_graph.invoke(
                Command(resume=approval_response), config=lg_config
            )
        else:
            state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
            new_state = production_graph.invoke(state, config=lg_config)

        # Preserve access_token from session store
        if state.get("access_token"):
            new_state["access_token"] = state["access_token"]
            new_state["user_id"] = state.get("user_id", "current")

    except Exception as exc:
        if is_ssl_error(exc):
            log_ssl_error(exc, "graph_invoke")
        else:
            logger.exception("run_turn | graph.invoke failed | thread=%s", thread_id)
        audit("API_ERROR", thread_id, {"error": str(exc)})
        raise

    audit("TURN_COMPLETE", thread_id, {
        "turn": new_state.get("turn_count"),
        "input_tokens": new_state.get("total_input_tokens"),
    })
    return new_state


def get_last_ai_message(state: ShoppingState) -> str:
    """Extract the last visible AI response as a plain string."""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "\n".join(p for p in parts if p).strip()
                if text:
                    return text
            return str(content)
    return ""


async def stream_turn(user_message: str, thread_id: str,
                      state: ShoppingState) -> AsyncIterator[str]:
    """Async streaming turn — yields text chunks for WebSocket."""
    set_trace_context(thread_id)
    clean = sanitise_input(user_message)
    state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
    lg_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    async for chunk in production_graph.astream(
        state, config=lg_config, stream_mode="messages"
    ):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg, meta = chunk
            if isinstance(msg, AIMessage) and msg.content:
                yield str(msg.content)


async def stream_turn_events(user_message: str, thread_id: str,
                             state: ShoppingState) -> AsyncIterator[dict]:
    """SSE streaming — yields structured events: status, chunk, error, state."""
    set_trace_context(thread_id)

    # Security checks (same as run_turn)
    is_malicious, reason = detect_prompt_injection(user_message)
    if is_malicious:
        audit("INJECTION_BLOCKED", thread_id, {"reason": reason})
        yield {"event": "error", "data": "I couldn't process that request. Please rephrase."}
        return

    ok, reason = rate_limiter.check_message(thread_id)
    if not ok:
        audit("RATE_LIMITED", thread_id, {})
        yield {"event": "error", "data": "You're sending messages too quickly. Please slow down."}
        return

    clean = sanitise_input(user_message)
    state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
    lg_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    yield {"event": "status", "data": {"phase": "thinking"}}

    try:
        async for chunk in production_graph.astream(
            state, config=lg_config, stream_mode="messages"
        ):
            if not (isinstance(chunk, tuple) and len(chunk) == 2):
                continue
            msg, meta = chunk
            if not isinstance(msg, AIMessage):
                continue

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    yield {"event": "status",
                           "data": {"phase": "tool", "tool": tc.get("name", "")}}
            elif msg.content:
                content = msg.content
                if isinstance(content, str) and content:
                    yield {"event": "chunk", "data": {"text": content}}
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                        elif isinstance(block, str):
                            text = block
                        else:
                            continue
                        if text:
                            yield {"event": "chunk", "data": {"text": text}}
    except Exception as exc:
        logger.exception("stream_turn_events | graph.astream failed | thread=%s", thread_id)
        yield {"event": "error", "data": str(exc)}
        return

    # Retrieve final state from checkpoint
    try:
        snapshot = await production_graph.aget_state(lg_config)
        final_state = dict(snapshot.values) if snapshot else state
        # Detect interrupt (human approval pending)
        if snapshot and snapshot.next:
            final_state["__interrupt__"] = True
    except Exception:
        logger.debug("Could not retrieve final state from checkpoint | thread=%s", thread_id)
        final_state = state

    # Preserve access token from session
    if state.get("access_token"):
        final_state["access_token"] = state["access_token"]
        final_state["user_id"] = state.get("user_id", "current")

    audit("TURN_COMPLETE", thread_id, {
        "turn": final_state.get("turn_count"),
        "input_tokens": final_state.get("total_input_tokens"),
    })
    yield {"event": "state", "data": final_state}


# ── Fallback responses ───────────────────────────────────────────────────────

_FALLBACK_RESPONSES = {
    "search": "I can't search right now. Try visiting the store directly.",
    "checkout": "Checkout is temporarily unavailable. Your cart is saved.",
    "generic": "I'm having trouble right now. Please try again in a moment.",
}


def get_fallback(intent: str = "generic") -> str:
    return _FALLBACK_RESPONSES.get(intent, _FALLBACK_RESPONSES["generic"])
