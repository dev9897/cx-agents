from __future__ import annotations

import json
import logging
import os
import random
import ssl
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncIterator, Optional, TypedDict

from dotenv import load_dotenv

# ── Load .env BEFORE any local imports — sap_commerce_tools reads env vars
# at module level when creating the shared httpx.Client, so .env must be
# populated before that import runs.
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
    trim_messages,
)
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver          # swap → AsyncSqliteSaver in prod
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt                 # Human-in-the-loop

# ── Local modules ────────────────────────────────────────────────────────────
from agent_config import CONFIG
from security_layer import (
    SecurityMiddleware, audit, detect_prompt_injection,
    rate_limiter, sanitise_input, scrub_pii,
)
from mcp_client import get_tools_sync, get_mcp_session_id
ALL_TOOLS = get_tools_sync()
_MCP_SESSION_ID = get_mcp_session_id()
# ── Observability ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, CONFIG.observability.log_level),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sap_agent")

# Force DEBUG on the tools logger so token diagnostics always show in dev
if os.getenv("SAP_STATIC_TOKEN"):
    logging.getLogger("sap_agent.tools").setLevel(logging.DEBUG)

# Enable LangSmith tracing if configured
if CONFIG.observability.tracing_backend == "langsmith":
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGCHAIN_PROJECT", CONFIG.observability.langsmith_project)


# ─────────────────────────────────────────────────────────────────────────────
# SSL DIAGNOSTIC HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _is_ssl_error(exc: BaseException) -> bool:
    """Return True if the exception chain contains an SSL certificate error."""
    cause = exc.__cause__ or exc
    return (
        isinstance(cause, ssl.SSLError)
        or "CERTIFICATE_VERIFY_FAILED" in str(cause)
        or "SSL" in type(cause).__name__.upper()
    )


def _log_ssl_error(exc: BaseException, context: str, url: str = "") -> None:
    """
    Emit a structured, actionable log entry for SSL failures so they are easy
    to find in log aggregators (grep for 'SSL_ERROR').
    """
    cause = exc.__cause__ or exc
    logger.error(
        "🔒 SSL_ERROR | context=%s | url=%s | error=%s\n"
        "   ── Diagnosis ────────────────────────────────────────────\n"
        "   OpenSSL version : %s\n"
        "   CA file         : %s\n"
        "   CA path         : %s\n"
        "   ── Likely fixes ─────────────────────────────────────────\n"
        "   1. Corporate proxy with SSL inspection → add proxy CA cert:\n"
        "         cp proxy-ca.crt /usr/local/share/ca-certificates/\n"
        "         update-ca-certificates\n"
        "   2. Missing/outdated CA bundle:\n"
        "         pip install --upgrade certifi\n"
        "         export SSL_CERT_FILE=$(python -m certifi)\n"
        "         export REQUESTS_CA_BUNDLE=$(python -m certifi)\n"
        "   3. SAP uses self-signed cert → set SAP_SSL_VERIFY=false (dev only)\n"
        "   ─────────────────────────────────────────────────────────",
        context,
        url or "(unknown)",
        cause,
        ssl.OPENSSL_VERSION,
        ssl.get_default_verify_paths().cafile,
        ssl.get_default_verify_paths().capath,
        exc_info=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OVERLOAD / RATE-LIMIT RETRY
# ─────────────────────────────────────────────────────────────────────────────

# How many times to retry on 529 overloaded_error before giving up
_OVERLOAD_MAX_RETRIES = int(os.getenv("ANTHROPIC_OVERLOAD_RETRIES", "4"))
# Base delay in seconds for exponential backoff (doubles each attempt + jitter)
_OVERLOAD_BASE_DELAY  = float(os.getenv("ANTHROPIC_OVERLOAD_BASE_DELAY", "2.0"))


def _is_overload_error(exc: BaseException) -> bool:
    """Return True if Anthropic responded with an overloaded_error (HTTP 529)."""
    err_str = str(exc)
    return (
        "overloaded_error" in err_str
        or "Overloaded" in err_str
        or getattr(exc, "status_code", None) == 529
    )


def _llm_invoke_with_retry(llm, messages, config):
    """
    Invoke the LLM with exponential backoff for overloaded_error (529).

    Retry schedule (default): 2s, 4s, 8s, 16s  → total wait up to ~30s.
    Each delay has ±25% jitter to avoid thundering-herd on shared infra.
    All other exceptions are re-raised immediately.
    """
    last_exc = None
    for attempt in range(_OVERLOAD_MAX_RETRIES + 1):
        try:
            return llm.invoke(messages, config=config)
        except Exception as exc:
            if not _is_overload_error(exc):
                raise  # non-overload errors bubble up immediately

            last_exc = exc
            if attempt == _OVERLOAD_MAX_RETRIES:
                break  # exhausted retries

            delay = _OVERLOAD_BASE_DELAY * (2 ** attempt)
            jitter = delay * 0.25 * (2 * random.random() - 1)   # ±25%
            wait   = round(delay + jitter, 2)

            logger.warning(
                "⚠️  Anthropic overloaded | attempt=%d/%d | retrying in %.1fs",
                attempt + 1, _OVERLOAD_MAX_RETRIES, wait,
            )
            time.sleep(wait)

    logger.error(
        "❌ Anthropic still overloaded after %d retries — giving up",
        _OVERLOAD_MAX_RETRIES,
    )
    raise last_exc


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 1 — Claude API via ChatAnthropic (correct model, caching, streaming)
# ─────────────────────────────────────────────────────────────────────────────

# API-level rate limiter (respect Anthropic tier limits)
_anthropic_rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.5,     # 30 RPM = 0.5/s (adjust to your tier)
    check_every_n_seconds=0.1,
    max_bucket_size=10,
)

_llm = ChatAnthropic(
    base_url="https://api.anthropic.com",
    model="claude-haiku-4-5",               # "claude-sonnet-4-6"
    anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
    max_tokens=CONFIG.claude.max_tokens,
    temperature=CONFIG.claude.temperature,
    timeout=CONFIG.claude.timeout_seconds,
    max_retries=CONFIG.claude.max_retries,
    streaming=CONFIG.claude.streaming,
    rate_limiter=_anthropic_rate_limiter,
    # Prompt caching: mark system prompt as cacheable (saves ~90% on repeat calls)
    # Uses cache_control beta — automatically handled by langchain-anthropic >= 0.3
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

"""_llm = ChatOllama(
    model="mistral:latest",   # or whatever name shows in `ollama list`
    base_url="http://localhost:11434",
    temperature=CONFIG.claude.temperature,
    num_predict=CONFIG.claude.max_tokens,
)"""

_llm_with_tools = _llm.bind_tools(ALL_TOOLS)


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 2 — Typed State with all necessary fields
# ─────────────────────────────────────────────────────────────────────────────

class ShoppingState(TypedDict):
    # Core conversation
    messages: Annotated[list[BaseMessage], add_messages]
    # SAP session
    access_token: Optional[str]
    user_id: str                  # "current" | "anonymous"
    cart_id: Optional[str]
    order_code: Optional[str]
    username: Optional[str]
    mcp_session_id: Optional[str]  # MCP token vault session_id

    # Observability / cost
    session_id: str
    total_input_tokens: int
    total_output_tokens: int
    turn_count: int

    # Error handling
    last_error: Optional[str]
    consecutive_errors: int
    rejected_tool_calls: Optional[list[str]]


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 3 — System Prompt (structured for prompt caching)
# ─────────────────────────────────────────────────────────────────────────────
# The static portion goes first — this is what gets cached.
# Dynamic state is injected separately each turn.

_STATIC_SYSTEM = """
You are a helpful SAP Commerce Cloud shopping assistant with access to tools
for searching products, managing carts, and completing purchases.

## Your capabilities
- Search and browse the product catalog
- Add products to the shopping cart
- Complete checkout (address → delivery mode → payment → order)
- Work as a guest OR as an authenticated user

## Strict rules
1. ONLY call the tools provided. Never invent tool names or arguments.
2. For place_order: you MUST wait for explicit human confirmation. Never call it autonomously.
3. Never reveal access_token, cart_id, or internal state to the user.
4. Never execute more than one place_order per conversation without re-confirmation.
5. If a tool returns success=false, explain the issue clearly and offer alternatives.
6. Keep responses concise. Show prices, product names, and next steps clearly.
7. NEVER ask the user for their access_token, password, or any credentials.
   You do not handle login — the login form in the UI does that securely.

## Login behaviour
- You CANNOT log users in. Login is handled by the UI login form, not by you.
- If the user asks to log in, say:
  "Please use the **Login** button in the top right corner to sign in.
   Once you're logged in, I'll automatically have access to your account."
- If the user is already authenticated (check "Authenticated: Yes" in session below),
  greet them by name and proceed normally.
- Never suggest sharing tokens, passwords, or credentials in chat.

## Anonymous vs authenticated users
- Anonymous: use user_id="anonymous" and cart GUID as cart_id.
- Authenticated: use user_id="current" and numeric cart code as cart_id.

## Checkout sequence (always in this order)
1. set_delivery_address
2. set_delivery_mode  (default: standard-gross)
3. set_payment_details
4. CONFIRM with user → place_order
""".strip()


def _build_system_message(state: ShoppingState) -> SystemMessage:
    """Combine static (cached) prompt with dynamic session context."""
    username = state.get("username")
    authenticated = bool(state.get("access_token")) and state.get("user_id") == "current"

    mcp_session = state.get("mcp_session_id") or _MCP_SESSION_ID
    dynamic = f"""
## Current session
- Authenticated : {"Yes — logged in as " + username if authenticated else "No (guest)"}
- User ID       : {state.get("user_id", "anonymous")}
- Cart ID       : {state.get("cart_id") or "Not created yet"}
- Session ID    : {mcp_session or "Not available — call account_login or guest_token first"}
- Turn          : {state.get("turn_count", 0)}

IMPORTANT: When calling tools that require session_id, always use: {mcp_session}
""".strip()
    return SystemMessage(content=_STATIC_SYSTEM + "\n\n" + dynamic)


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 4 — Context Window Management (trim old messages)
# ─────────────────────────────────────────────────────────────────────────────

def _trim_context(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Prevent context overflow while keeping tool call / tool result pairs intact.

    Anthropic's API requires that every ToolMessage (tool_result) is immediately
    preceded by the AIMessage (tool_use) that produced it. trim_messages() can
    split these pairs when truncating, causing a 400 BadRequestError.

    Strategy:
    1. Always keep the last MAX_MESSAGES messages as a hard fallback.
    2. Walk forward from the trim point and skip forward until we land on a
       HumanMessage — never start mid tool-call/result pair.
    """
    max_msgs = CONFIG.resilience.max_messages_in_context

    if len(messages) <= max_msgs:
        # Even if we don't trim, validate the message structure
        validated = _validate_tool_message_pairs(messages)
        logger.debug(
            "_trim_context | no trimming needed | original=%d → validated=%d",
            len(messages), len(validated)
        )
        return validated

    # Take the last max_msgs messages
    trimmed = messages[-max_msgs:]

    # Walk forward until we find a safe start point (HumanMessage).
    # This ensures we never start with an orphaned ToolMessage or AIMessage
    # that has tool_calls without the preceding context.
    for i, msg in enumerate(trimmed):
        if isinstance(msg, HumanMessage):
            trimmed = trimmed[i:]
            break

    # Final safety check: if the first message is a ToolMessage, something
    # is still wrong — drop messages until it isn't.
    while trimmed and isinstance(trimmed[0], ToolMessage):
        trimmed = trimmed[1:]

    # Validate tool call/result pairs
    safe = _validate_tool_message_pairs(trimmed)

    logger.debug(
        "_trim_context | original=%d → trimmed=%d → safe=%d",
        len(messages), len(trimmed), len(safe),
    )
    return safe


def _validate_tool_message_pairs(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Ensure every AIMessage with tool_calls is followed by corresponding ToolMessages,
    and every ToolMessage has a preceding AIMessage with matching tool_call.
    """
    safe = []
    pending_tool_calls = {}  # tool_call_id -> tool_call

    for i, msg in enumerate(messages):
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                # Track tool calls that need results
                for tc in msg.tool_calls:
                    pending_tool_calls[tc["id"]] = tc
                safe.append(msg)
            else:
                safe.append(msg)

        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id and tool_call_id in pending_tool_calls:
                # This ToolMessage matches a pending tool call
                safe.append(msg)
                del pending_tool_calls[tool_call_id]
            else:
                logger.warning(
                    "_validate_tool_message_pairs | dropping orphaned ToolMessage tool_call_id=%s",
                    tool_call_id or "?",
                )

        else:
            # HumanMessage, SystemMessage, etc.
            safe.append(msg)

    # Check for unmatched tool calls
    if pending_tool_calls:
        logger.error(
            "_validate_tool_message_pairs | found %d unmatched tool_calls: %s | cleaning up...",
            len(pending_tool_calls),
            list(pending_tool_calls.keys())
        )
        # Remove AIMessages with unmatched tool calls to prevent API errors
        final_safe = []
        for msg in safe:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                # Keep only tool calls that have results
                matched_tool_calls = [
                    tc for tc in msg.tool_calls
                    if tc["id"] not in pending_tool_calls
                ]
                if matched_tool_calls:
                    # Create new message with only matched tool calls
                    new_msg = AIMessage(
                        content=msg.content,
                        tool_calls=matched_tool_calls,
                        id=msg.id,
                    )
                    final_safe.append(new_msg)
                    logger.debug(
                        "_validate_tool_message_pairs | kept %d/%d tool calls for message %s",
                        len(matched_tool_calls), len(msg.tool_calls), msg.id
                    )
                else:
                    # If no tool calls remain, create message without tool calls
                    new_msg = AIMessage(
                        content=msg.content,
                        id=msg.id,
                    )
                    final_safe.append(new_msg)
                    logger.debug(
                        "_validate_tool_message_pairs | removed all tool calls from message %s",
                        msg.id
                    )
            else:
                final_safe.append(msg)

        logger.info(
            "_validate_tool_message_pairs | cleanup complete | removed %d unmatched tool calls",
            len(pending_tool_calls)
        )
        return final_safe

    return safe


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 5 — Token / Cost Tracking
# ─────────────────────────────────────────────────────────────────────────────

class TokenTracker:
    # Approximate pricing for claude-sonnet-4-6 (check docs.anthropic.com for latest)
    INPUT_COST_PER_1K  = 0.003   # USD
    OUTPUT_COST_PER_1K = 0.015   # USD
    CACHE_READ_PER_1K  = 0.0003  # 90% cheaper

    @staticmethod
    def update(state: ShoppingState, response: AIMessage) -> dict:
        usage = response.usage_metadata or {}
        in_tokens  = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        cached     = usage.get("cache_read_input_tokens", 0)

        new_in  = state.get("total_input_tokens", 0) + in_tokens
        new_out = state.get("total_output_tokens", 0) + out_tokens

        # Cost estimate
        cost = (
            (in_tokens  / 1000) * TokenTracker.INPUT_COST_PER_1K +
            (out_tokens / 1000) * TokenTracker.OUTPUT_COST_PER_1K +
            (cached     / 1000) * TokenTracker.CACHE_READ_PER_1K
        )

        # Session budget alert
        if new_in > CONFIG.cost.session_token_budget:
            logger.warning("⚠️  Session token budget exceeded: %d tokens", new_in)

        logger.info(
            "tokens | session=%s in=%d out=%d cached=%d cost_usd=%.4f",
            state.get("session_id", "?"), new_in, new_out, cached, cost,
        )

        return {
            "total_input_tokens": new_in,
            "total_output_tokens": new_out,
        }


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 6 — Circuit Breaker for SAP API calls
# ─────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self):
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at > CONFIG.resilience.circuit_breaker_timeout:
            self._failures = 0
            self._opened_at = None
            logger.info("Circuit breaker reset (half-open)")
            return False
        return True

    def record_success(self):
        self._failures = 0
        self._opened_at = None

    def record_failure(self):
        self._failures += 1
        if self._failures >= CONFIG.resilience.circuit_breaker_threshold:
            self._opened_at = time.time()
            logger.error("Circuit breaker OPENED after %d failures", self._failures)


sap_circuit_breaker = CircuitBreaker()


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH NODES
# ─────────────────────────────────────────────────────────────────────────────

def agent_node(state: ShoppingState, config: RunnableConfig) -> dict:
    """
    Main reasoning node.
    - Builds context-aware system prompt
    - Trims context window
    - Calls Claude with tool binding
    - Tracks tokens
    """
    session_id = state.get("session_id", "?")

    # Check circuit breaker
    if sap_circuit_breaker.is_open:
        logger.warning(
            "agent_node | session=%s | circuit breaker is OPEN — skipping LLM call",
            session_id,
        )
        return {
            "messages": [AIMessage(content=(
                "I'm having trouble connecting to the store right now. "
                "Please try again in a moment."
            ))],
            "last_error": "circuit_breaker_open",
        }

    system_msg = _build_system_message(state)
    trimmed    = _trim_context(state["messages"])
    all_msgs   = [system_msg] + trimmed

    # Debug tool call/result pairing before sending to Claude
    tool_call_debug = []
    for i, msg in enumerate(all_msgs):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            tool_call_debug.append(f"AI#{i}: {[tc['id'][:8] for tc in msg.tool_calls]}")
        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", "?")
            tool_call_debug.append(f"Tool#{i}: {tool_call_id[:8] if tool_call_id != '?' else '?'}")

    logger.debug(
        "agent_node | session=%s | turn=%d | messages_in_context=%d | tool_flow=%s",
        session_id,
        state.get("turn_count", 0),
        len(all_msgs),
        " → ".join(tool_call_debug) if tool_call_debug else "no_tools",
    )

    # Additional debugging for unmatched tool calls
    unmatched_calls = []
    pending_calls = {}
    for msg in all_msgs:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                pending_calls[tc["id"]] = tc["name"]
        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id and tool_call_id in pending_calls:
                del pending_calls[tool_call_id]

    if pending_calls:
        logger.error(
            "agent_node | session=%s | CRITICAL: About to send unmatched tool calls to Claude: %s | EMERGENCY CLEANUP",
            session_id, list(pending_calls.keys())
        )
        # Log the actual messages being sent for debugging
        for i, msg in enumerate(all_msgs[-10:]):  # Last 10 messages
            if isinstance(msg, AIMessage) and msg.tool_calls:
                logger.error("agent_node | msg[%d] AIMessage tool_calls: %s", i, [tc["id"] for tc in msg.tool_calls])
            elif isinstance(msg, ToolMessage):
                logger.error("agent_node | msg[%d] ToolMessage tool_call_id: %s", i, getattr(msg, "tool_call_id", "?"))

        # EMERGENCY: Remove problematic tool calls to prevent API error
        cleaned_msgs = []
        for msg in all_msgs:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                # Remove unmatched tool calls
                safe_tool_calls = [tc for tc in msg.tool_calls if tc["id"] not in pending_calls]
                if safe_tool_calls:
                    cleaned_msgs.append(AIMessage(
                        content=msg.content,
                        tool_calls=safe_tool_calls,
                        id=msg.id,
                    ))
                else:
                    # Remove tool calls entirely
                    cleaned_msgs.append(AIMessage(
                        content=msg.content,
                        id=msg.id,
                    ))
            else:
                cleaned_msgs.append(msg)

        all_msgs = cleaned_msgs
        logger.warning("agent_node | session=%s | Emergency cleanup applied - removed %d unmatched tool calls",
                      session_id, len(pending_calls))

    try:
        response = _llm_invoke_with_retry(_llm_with_tools, all_msgs, config)

        # Token tracking
        token_updates = TokenTracker.update(state, response)

        sap_circuit_breaker.record_success()

        logger.debug(
            "agent_node | session=%s | tool_calls=%s",
            session_id,
            [tc["name"] for tc in (response.tool_calls or [])],
        )

        return {
            "messages": [response],
            "turn_count": state.get("turn_count", 0) + 1,
            "last_error": None,
            "consecutive_errors": 0,
            **token_updates,
        }

    except Exception as exc:
        # ── Classify the error before touching the circuit breaker ────────────
        if _is_overload_error(exc):
            # Overload is an Anthropic-side capacity issue, NOT a SAP failure.
            # Do NOT trip the circuit breaker — just surface a friendly message.
            logger.error(
                "agent_node | Anthropic overloaded (529) after %d retries | session=%s",
                _OVERLOAD_MAX_RETRIES, session_id,
            )
            audit("API_ERROR", session_id, {
                "error": "overloaded_error",
                "context": "anthropic_llm_call",
                "retries_exhausted": _OVERLOAD_MAX_RETRIES,
            })
            return {
                "messages": [AIMessage(content=(
                    "The AI service is currently under heavy load. "
                    "Please wait a moment and try again."
                ))],
                "last_error": "overloaded_error",
                "consecutive_errors": state.get("consecutive_errors", 0),
                # Don't increment consecutive_errors — not a code bug
            }

        if _is_ssl_error(exc):
            _log_ssl_error(exc, context="anthropic_llm_call", url="https://api.anthropic.com")
            audit("API_ERROR", session_id, {
                "error": str(exc.__cause__ or exc),
                "context": "anthropic_llm_call",
                "ssl": True,
            })
        else:
            logger.exception(
                "agent_node | LLM call failed | session=%s | error=%s",
                session_id, exc,
            )

        # Only trip circuit breaker for genuine failures (not overload/SSL)
        sap_circuit_breaker.record_failure()

        consecutive = state.get("consecutive_errors", 0) + 1
        if consecutive >= 3:
            msg = "I'm experiencing repeated issues. Please contact support."
        else:
            msg = f"I hit a snag ({type(exc).__name__}). Let me try again — could you rephrase?"

        return {
            "messages": [AIMessage(content=msg)],
            "last_error": str(exc),
            "consecutive_errors": consecutive,
        }


def human_approval_node(state: ShoppingState) -> dict:
    """
    FACTOR 7 — Human-in-the-loop (LangGraph interrupt).
    Pauses graph execution before place_order and waits for explicit approval.
    In production: integrate with your UI via LangGraph Cloud / Server API.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    rejected_tool_calls = []

    for tc in last.tool_calls:
        if tc["name"] == "place_order":
            # Pause and send to client for approval
            cart_id = tc["args"].get("cart_id", "?")
            approval = interrupt({
                "type": "order_confirmation",
                "message": f"Ready to place order for cart {cart_id}. Confirm?",
                "tool_call_id": tc["id"],
                "args": tc["args"],
            })
            if not approval.get("approved"):
                audit("ORDER_REJECTED", state.get("session_id", "?"), tc["args"])
                rejected_tool_calls.append(tc["id"])
            else:
                audit("ORDER_APPROVED", state.get("session_id", "?"), tc["args"])

    # Store rejected tool calls in state for the tools node to handle
    if rejected_tool_calls:
        return {"rejected_tool_calls": rejected_tool_calls}

    return {}


def state_sync_node(state: ShoppingState) -> dict:
    """Scan tool results and persist important state fields."""
    updates: dict = {}
    session_id = state.get("session_id", "?")

    for msg in reversed(state["messages"]):
        if not isinstance(msg, ToolMessage):
            break
        try:
            result = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "state_sync_node | session=%s | could not parse ToolMessage as JSON: %r",
                session_id, msg.content[:200],
            )
            continue

        if not result.get("success"):
            # Check if the tool failure was SSL-related
            error_str = str(result.get("error", ""))
            if "CERTIFICATE_VERIFY_FAILED" in error_str or "SSL" in error_str.upper():
                logger.error(
                    "🔒 SSL_ERROR | context=tool_result | session=%s | tool_call_id=%s | error=%s\n"
                    "   This means a SAP Commerce API call failed due to SSL.\n"
                    "   Check the tool that produced tool_call_id=%s for the exact URL.",
                    session_id, msg.tool_call_id, error_str, msg.tool_call_id,
                )
                audit("API_ERROR", session_id, {
                    "error": error_str,
                    "context": "sap_tool_call",
                    "tool_call_id": msg.tool_call_id,
                    "ssl": True,
                })
            else:
                logger.warning(
                    "state_sync_node | session=%s | tool_call_id=%s | success=false | error=%s",
                    session_id, msg.tool_call_id, error_str or result,
                )
            sap_circuit_breaker.record_failure()
            continue

        sap_circuit_breaker.record_success()
        if "access_token" in result:
            updates["access_token"] = result["access_token"]
        if "username" in result:
            updates["username"] = result["username"]
            updates["user_id"] = "current"
        # create_cart returns cart_id (guid for anonymous, code for authenticated)
        if result.get("cart_id"):
            updates["cart_id"] = result["cart_id"]
        # Also persist user_id returned by create_cart (anonymous vs current)
        if result.get("user_id") and "username" not in result:
            updates["user_id"] = result["user_id"]
        if "order_code" in result:
            updates["order_code"] = result["order_code"]

    if updates:
        logger.debug("state_sync_node | session=%s | state updates=%s", session_id, list(updates.keys()))

    return updates


# Pre-built ToolNode
_raw_tool_node = ToolNode(ALL_TOOLS)


def tool_node_with_injection(state: ShoppingState) -> dict:
    """
    Injects access_token from session state into every tool call.
    Claude never needs to pass the token — it comes from state automatically.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return _raw_tool_node.invoke(state)

    access_token = state.get("access_token") or ""

    # Ensure rejected_tool_calls is always a list, never None
    rejected_tool_calls = state.get("rejected_tool_calls")
    if rejected_tool_calls is None:
        rejected_tool_calls = []

    # Create tool results for rejected calls first
    tool_results = []
    for tool_call in last.tool_calls:
        if tool_call["id"] in rejected_tool_calls:
            tool_results.append(ToolMessage(
                content=json.dumps({"success": False, "reason": "User cancelled order."}),
                tool_call_id=tool_call["id"],
            ))

    # Process remaining (non-rejected) tool calls
    patched_calls = []
    for tc in last.tool_calls:
        if tc["id"] not in rejected_tool_calls:
            args = dict(tc.get("args", {}))
            # ALWAYS override access_token from state — Claude may pass
            # user_id ("current") as the token by mistake.
            if access_token:
                args["access_token"] = access_token
                logger.debug("tool_injection | %s | injected access_token (len=%d)", tc.get("name"), len(access_token))
            patched_calls.append({**tc, "args": args})

    # If all tool calls were rejected, just return the rejection messages
    if not patched_calls:
        return {"messages": tool_results, "rejected_tool_calls": None}

    # Execute non-rejected tool calls
    patched_msg = AIMessage(
        content=last.content,
        tool_calls=patched_calls,
        id=last.id,
    )
    patched_state = {**state, "messages": state["messages"][:-1] + [patched_msg]}
    result = _raw_tool_node.invoke(patched_state)

    # Add rejected tool results to the beginning of the results
    if tool_results:
        result["messages"] = tool_results + result.get("messages", [])

    # Clear rejected tool calls from state
    result["rejected_tool_calls"] = None
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING
# ─────────────────────────────────────────────────────────────────────────────

def route_after_agent(state: ShoppingState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        # Check if any tool requires human approval
        for tc in last.tool_calls:
            if tc["name"] == "place_order":
                return "human_approval"
        return "tools"
    return "sync"


def route_after_sync(state: ShoppingState) -> str:
    last = state["messages"][-1]
    if isinstance(last, ToolMessage):
        return "agent"
    return END


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────

def build_production_graph():
    g = StateGraph(ShoppingState)

    g.add_node("agent",          agent_node)
    g.add_node("human_approval", human_approval_node)
    g.add_node("tools",          tool_node_with_injection)
    g.add_node("sync",           state_sync_node)

    g.add_edge(START, "agent")

    g.add_conditional_edges("agent", route_after_agent, {
        "human_approval": "human_approval",
        "tools":          "tools",
        "sync":           "sync",
    })

    g.add_edge("human_approval", "tools")
    g.add_edge("tools", "sync")

    g.add_conditional_edges("sync", route_after_sync, {
        "agent": "agent",
        END:     END,
    })

    # FACTOR 8 — Checkpointing: persist state across turns (survives restarts)
    # In production: replace MemorySaver with AsyncSqliteSaver or RedisCheckpointer
    checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer, interrupt_before=[])

production_graph = build_production_graph()


def new_session(user_id: str = "anonymous") -> tuple[ShoppingState, str]:
    """
    Create a fresh session. Returns (initial_state, thread_id).

    Always bootstraps an anonymous SAP OAuth token so that tool calls
    work immediately without requiring a login step.
    """
    thread_id = str(uuid.uuid4())

    # Allow a hardcoded token for dev/testing — set SAP_STATIC_TOKEN in .env
    # to skip the OAuth flow entirely. Must be a password-grant token, not client_credentials.
    # Never use this in production.
    static_token ="8ZLSDZxna5k5IbrkAc-OAhcWs_A"
    if static_token:
        logger.warning(
            "⚠️  new_session | Using SAP_STATIC_TOKEN — dev mode only | session=%s",
            thread_id,
        )
        access_token     = static_token
        resolved_user_id = "current"
        resolved_username = os.getenv("SAP_STATIC_USERNAME", "lang-graph-user")
    else:
        logger.warning("new_session | Could not obtain SAP token | session=%s", thread_id)

    init_state = ShoppingState(
        messages=[],
        access_token=access_token,
        user_id=resolved_user_id,
        cart_id=None,
        order_code=None,
        username=resolved_username,
        mcp_session_id=_MCP_SESSION_ID,
        session_id=thread_id,
        total_input_tokens=0,
        total_output_tokens=0,
        turn_count=0,
        last_error=None,
        consecutive_errors=0,
    )

    logger.info(
        "new_session | session=%s | user_id=%s | token_len=%d | token_preview=%s",
        thread_id,
        resolved_user_id,
        len(access_token) if access_token else 0,
        (access_token[:12] + "...") if access_token and len(access_token) > 12 else repr(access_token),
    )
    logger.debug(
        "new_session | full init_state keys=%s | access_token in state=%r | user_id in state=%r",
        list(init_state.keys()),
        init_state.get("access_token", "MISSING")[:10] if init_state.get("access_token") else "NONE",
        init_state.get("user_id", "MISSING"),
    )

    audit("SESSION_START", thread_id, {
        "user_id": resolved_user_id,
        "token_ok": bool(access_token and len(access_token) > 20),
    })
    return init_state, thread_id


def run_turn(user_message: str, thread_id: str,
             state: ShoppingState,
             approval_response: Optional[dict] = None) -> ShoppingState:
    """
    Run one conversation turn with full security + observability pipeline.

    approval_response: pass {"approved": True/False} when resuming after interrupt.
    """
    # ── Security middleware ──────────────────────────────────────────────────
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

    # ── LangGraph config (thread = session for checkpointing) ────────────────
    lg_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    logger.debug(
        "run_turn | thread=%s | approval_response=%s | message_len=%d",
        thread_id,
        bool(approval_response),
        len(clean),
    )

    # ── Invoke ───────────────────────────────────────────────────────────────
    logger.debug(
        "run_turn | thread=%s | access_token_len=%d | user_id=%s",
        thread_id,
        len(state.get("access_token") or ""),
        state.get("user_id"),
    )
    try:
        if approval_response:
            # Resume after human interrupt — must use Command(resume=...)
            # so LangGraph continues from the interrupted node, not restart.
            new_state = production_graph.invoke(
                Command(resume=approval_response), config=lg_config
            )
        else:
            state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
            new_state = production_graph.invoke(state, config=lg_config)

        # LangGraph MemorySaver merges checkpoint state on every invoke.
        # Explicitly restore access_token from our session store so it is
        # never overwritten by a stale checkpoint value.
        if state.get("access_token"):
            new_state["access_token"] = state["access_token"]
            new_state["user_id"]      = state.get("user_id", "current")

    except Exception as exc:
        if _is_ssl_error(exc):
            _log_ssl_error(exc, context="graph_invoke", url="(SAP or Anthropic)")
            audit("API_ERROR", thread_id, {
                "error": str(exc.__cause__ or exc),
                "context": "graph_invoke",
                "ssl": True,
            })
        else:
            logger.exception("run_turn | graph.invoke failed | thread=%s", thread_id)
            audit("API_ERROR", thread_id, {"error": str(exc)})
        raise

    audit("TURN_COMPLETE", thread_id, {
        "turn": new_state.get("turn_count"),
        "input_tokens": new_state.get("total_input_tokens"),
        "output_tokens": new_state.get("total_output_tokens"),
    })

    return new_state


def get_last_ai_message(state: ShoppingState) -> str:
    """
    Extract the last visible AI response as a plain string.

    Claude sometimes returns content as a list of content blocks:
        [{'type': 'text', 'text': '...', 'index': 0}, ...]
    This helper always coerces that to a str so Pydantic serialisation
    in api_server.py never sees a list.
    """
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            content = msg.content
            # Already a plain string — most common case
            if isinstance(content, str):
                return content
            # List of content blocks (e.g. when tool_use is mixed in)
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
            # Fallback — stringify whatever we got
            logger.warning(
                "get_last_ai_message | unexpected content type=%s value=%r",
                type(content).__name__, str(content)[:200],
            )
            return str(content)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 9 — Streaming (async)
# ─────────────────────────────────────────────────────────────────────────────

async def stream_turn(user_message: str, thread_id: str,
                      state: ShoppingState) -> AsyncIterator[str]:
    """
    Async streaming turn — yields text chunks as they arrive from Claude.
    Wire into FastAPI / WebSocket for real-time UX.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# FACTOR 10 — Graceful degradation / fallback
# ─────────────────────────────────────────────────────────────────────────────

_FALLBACK_RESPONSES = {
    "search":   "I can't search right now. Try visiting the store directly.",
    "checkout": "Checkout is temporarily unavailable. Your cart is saved.",
    "generic":  "I'm having trouble right now. Please try again in a moment.",
}


def get_fallback(intent: str = "generic") -> str:
    return _FALLBACK_RESPONSES.get(intent, _FALLBACK_RESPONSES["generic"])


# ─────────────────────────────────────────────────────────────────────────────
# CLI demo
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🛒  SAP Commerce Agent (Production)  — type 'quit' to exit\n")
    session_state, tid = new_session("demo-user-001")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() in ("quit", "exit"):
            break

        session_state = run_turn(user_input, tid, session_state)
        reply = get_last_ai_message(session_state)
        print(f"\nAssistant: {reply}\n")

        # Print cost summary every 5 turns
        if session_state.get("turn_count", 0) % 5 == 0:
            print(f"  [Tokens: in={session_state['total_input_tokens']} "
                  f"out={session_state['total_output_tokens']}]\n")