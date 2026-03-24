from __future__ import annotations

import json
import logging
import os
import re
import random
import ssl
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncIterator, Optional, TypedDict

from dotenv import load_dotenv


load_dotenv()

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

# Local modules
from agent_config import CONFIG
from security_layer import (
    audit,
    detect_prompt_injection,
    rate_limiter,
    sanitise_input,
)
from mcp_client import get_tools_sync, get_mcp_session_id
from qdrant_tool import semantic_search_products
from memory_history.user_memory import (
    ensure_user_collection,
    get_cf_recommendations,
    save_user_interaction,
)

# ── Tool registry ─────────────────────────────────────────────────────────────
ALL_TOOLS = get_tools_sync()
ALL_TOOLS.append(semantic_search_products)

_MCP_SESSION_ID = get_mcp_session_id()

# Warm up Qdrant collection once at startup — not on every request (fix #1)
try:
    ensure_user_collection()
except Exception as _qdrant_init_err:
    logging.getLogger("sap_agent").warning(
        "Qdrant collection init failed at startup: %s — will retry on first use",
        _qdrant_init_err,
    )


# ─────────────────────────────────────────────────────────────────────────────
# OBSERVABILITY
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, CONFIG.observability.log_level),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("sap_agent")

if os.getenv("SAP_STATIC_TOKEN"):
    logging.getLogger("sap_agent.tools").setLevel(logging.DEBUG)

if CONFIG.observability.tracing_backend == "langsmith":
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGCHAIN_PROJECT", CONFIG.observability.langsmith_project)


# ─────────────────────────────────────────────────────────────────────────────
# SSL DIAGNOSTIC HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _is_ssl_error(exc: BaseException) -> bool:
    cause = exc.__cause__ or exc
    return (
        isinstance(cause, ssl.SSLError)
        or "CERTIFICATE_VERIFY_FAILED" in str(cause)
        or "SSL" in type(cause).__name__.upper()
    )


def _log_ssl_error(exc: BaseException, context: str, url: str = "") -> None:
    cause = exc.__cause__ or exc
    logger.error(
        "SSL_ERROR | context=%s | url=%s | error=%s\n"
        "   OpenSSL version : %s\n"
        "   CA file         : %s\n"
        "   CA path         : %s\n"
        "   Likely fixes:\n"
        "   1. Corporate proxy with SSL inspection -> add proxy CA cert\n"
        "   2. Missing/outdated CA bundle: pip install --upgrade certifi\n"
        "   3. SAP uses self-signed cert -> set SAP_SSL_VERIFY=false (dev only)",
        context,
        url or "(unknown)",
        cause,
        ssl.OPENSSL_VERSION,
        ssl.get_default_verify_paths().cafile,
        ssl.get_default_verify_paths().capath,
        exc_info=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM RETRY (overload / transient errors)
# ─────────────────────────────────────────────────────────────────────────────

_OVERLOAD_MAX_RETRIES = int(os.getenv("LLM_OVERLOAD_RETRIES", "4"))
_OVERLOAD_BASE_DELAY  = float(os.getenv("LLM_OVERLOAD_BASE_DELAY", "2.0"))


def _is_overload_error(exc: BaseException) -> bool:
    err = str(exc)
    return (
        "overloaded_error" in err
        or "Overloaded" in err
        or getattr(exc, "status_code", None) == 529
    )


def _llm_invoke_with_retry(llm, messages, config):

    last_exc = None
    for attempt in range(_OVERLOAD_MAX_RETRIES + 1):
        try:
            return llm.invoke(messages, config=config)
        except Exception as exc:
            if not _is_overload_error(exc):
                raise
            last_exc = exc
            if attempt == _OVERLOAD_MAX_RETRIES:
                break
            delay  = _OVERLOAD_BASE_DELAY * (2 ** attempt)
            jitter = delay * 0.25 * (2 * random.random() - 1)
            wait   = round(delay + jitter, 2)
            logger.warning(
                "LLM overloaded | attempt=%d/%d | retrying in %.1fs",
                attempt + 1, _OVERLOAD_MAX_RETRIES, wait,
            )
            time.sleep(wait)

    logger.error("LLM still overloaded after %d retries — giving up", _OVERLOAD_MAX_RETRIES)
    raise last_exc


# ─────────────────────────────────────────────────────────────────────────────
# LLM SETUP
# ─────────────────────────────────────────────────────────────────────────────

_llm = ChatOllama(
    model=os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"),
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    temperature=CONFIG.claude.temperature,
    num_predict=CONFIG.claude.max_tokens,
)

_llm_with_tools = _llm.bind_tools(ALL_TOOLS)


# ─────────────────────────────────────────────────────────────────────────────
# TYPED STATE
# ─────────────────────────────────────────────────────────────────────────────

class ShoppingState(TypedDict):
    # Core conversation
    messages: Annotated[list[BaseMessage], add_messages]

    # SAP session
    access_token: Optional[str]
    user_id: str                    
    cart_id: Optional[str]
    order_code: Optional[str]
    username: Optional[str]
    mcp_session_id: Optional[str]

    # Observability / cost
    session_id: str
    total_input_tokens: int
    total_output_tokens: int
    turn_count: int

    # Error handling
    last_error: Optional[str]
    consecutive_errors: int
    rejected_tool_calls: Optional[list[str]]

    # Collaborative Filtering
    cf_recommendations: Optional[list[dict]]
    last_added_product: Optional[str]
    last_saved_query: Optional[str]  


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_STATIC_SYSTEM = """
You are a helpful SAP Commerce Cloud shopping assistant with access to tools
for searching products, managing carts, and completing purchases.

## Your capabilities
- Search and browse the product catalog
- Add products to the shopping cart
- Complete checkout (address -> delivery mode -> payment -> order)
- Work as a guest OR as an authenticated user

## Strict rules
1. ONLY call the tools provided. Never invent tool names or arguments.
2. For place_order: you MUST wait for explicit human confirmation. Never call it autonomously.
3. Never reveal access_token, cart_id, or internal state to the user.
4. Never execute more than one place_order per conversation without re-confirmation.
5. If a tool returns success=false, explain the issue clearly and offer alternatives.
6. Keep responses concise. Show prices, product names, and next steps clearly.
7. NEVER ask the user for their access_token, password, or any credentials.

## Login behaviour
- You CANNOT log users in. Login is handled by the UI login form, not by you.
- If the user asks to log in, say:
  "Please use the Login button in the top right corner to sign in.
   Once you're logged in, I'll automatically have access to your account."
- If already authenticated (check "Authenticated: Yes" in session below),
  greet them by name and proceed normally.
- Never suggest sharing tokens, passwords, or credentials in chat.

## Anonymous vs authenticated users
- Anonymous: use user_id="anonymous" and cart GUID as cart_id.
- Authenticated: use user_id="current" and numeric cart code as cart_id.

## Checkout sequence (always in this order)
1. set_delivery_address
2. set_delivery_mode  (default: standard-gross)
3. set_payment_details
4. CONFIRM with user -> place_order
""".strip()


def _build_system_message(state: ShoppingState) -> SystemMessage:
    """Combine static (cache-friendly) prompt with dynamic session context."""
    username      = state.get("username")
    authenticated = bool(state.get("access_token")) and state.get("user_id") == "current"
    mcp_session   = state.get("mcp_session_id") or _MCP_SESSION_ID

    # Inject all CF recommendations (up to 5) for maximum LLM signal (#9)
    cf_recs    = state.get("cf_recommendations") or []
    cf_section = ""
    if cf_recs:
        items      = ", ".join(r["item"] for r in cf_recs[:5])
        cf_section = f"\n- CF suggestions (similar users liked): {items}"

    dynamic = f"""
## Current session
- Authenticated : {"Yes — logged in as " + username if authenticated else "No (guest)"}
- User ID       : {state.get("user_id", "anonymous")}
- Cart ID       : {state.get("cart_id") or "Not created yet"}
- Session ID    : {mcp_session or "Not available — call account_login or guest_token first"}
- Turn          : {state.get("turn_count", 0)}
{cf_section}

IMPORTANT: When calling tools that require session_id, always use: {mcp_session}
If CF suggestions are present, proactively mention them when relevant.
""".strip()

    return SystemMessage(content=_STATIC_SYSTEM + "\n\n" + dynamic)


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT WINDOW MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _trim_context(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Prevent context overflow while keeping tool call / tool result pairs intact.
    Strategy:
    1. Keep the last MAX_MESSAGES messages.
    2. Walk forward to the first HumanMessage so we never start mid-pair.
    3. Drop any leading ToolMessages that have no preceding AIMessage.
    4. Run _validate_tool_message_pairs as a final safety net.
    """
    max_msgs = CONFIG.resilience.max_messages_in_context

    if len(messages) <= max_msgs:
        validated = _validate_tool_message_pairs(messages)
        logger.debug(
            "_trim_context | no trimming needed | original=%d -> validated=%d",
            len(messages), len(validated),
        )
        return validated

    trimmed = messages[-max_msgs:]

    for i, msg in enumerate(trimmed):
        if isinstance(msg, HumanMessage):
            trimmed = trimmed[i:]
            break

    while trimmed and isinstance(trimmed[0], ToolMessage):
        trimmed = trimmed[1:]

    safe = _validate_tool_message_pairs(trimmed)

    logger.debug(
        "_trim_context | original=%d -> trimmed=%d -> safe=%d",
        len(messages), len(trimmed), len(safe),
    )
    return safe


def _validate_tool_message_pairs(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Ensure every AIMessage with tool_calls is followed by the matching
    ToolMessages, and every ToolMessage has a preceding AIMessage.
    Orphaned ToolMessages and unmatched tool_calls are removed and logged.
    """
    safe               = []
    pending_tool_calls: dict[str, Any] = {}

    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    pending_tool_calls[tc["id"]] = tc
            safe.append(msg)

        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id and tool_call_id in pending_tool_calls:
                safe.append(msg)
                del pending_tool_calls[tool_call_id]
            else:
                logger.warning(
                    "_validate_tool_message_pairs | dropping orphaned ToolMessage "
                    "tool_call_id=%s",
                    tool_call_id or "?",
                )
        else:
            safe.append(msg)

    # Clean up any AIMessages whose tool_calls were never answered
    if pending_tool_calls:
        logger.error(
            "_validate_tool_message_pairs | %d unmatched tool_calls: %s | cleaning up",
            len(pending_tool_calls),
            list(pending_tool_calls.keys()),
        )
        final_safe = []
        for msg in safe:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                matched = [tc for tc in msg.tool_calls if tc["id"] not in pending_tool_calls]
                if matched:
                    final_safe.append(
                        AIMessage(content=msg.content, tool_calls=matched, id=msg.id)
                    )
                else:
                    final_safe.append(AIMessage(content=msg.content, id=msg.id))
            else:
                final_safe.append(msg)
        logger.info(
            "_validate_tool_message_pairs | cleanup complete | removed %d unmatched calls",
            len(pending_tool_calls),
        )
        return final_safe

    return safe


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN TRACKING  (Ollama-aware — fix #6)
# ─────────────────────────────────────────────────────────────────────────────

class TokenTracker:
    """
    Tracks token usage from Ollama response metadata.

    Ollama exposes token counts in response_metadata, not usage_metadata.
    Keys used: "prompt_eval_count" (input) and "eval_count" (output).
    """

    @staticmethod
    def update(state: ShoppingState, response: AIMessage) -> dict:
        # Prefer usage_metadata (Anthropic / OpenAI) then fall back to
        # response_metadata (Ollama).
        usage = response.usage_metadata or {}
        if not usage:
            meta    = getattr(response, "response_metadata", {}) or {}
            in_tok  = meta.get("prompt_eval_count", 0)
            out_tok = meta.get("eval_count", 0)
        else:
            in_tok  = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)

        new_in  = state.get("total_input_tokens",  0) + in_tok
        new_out = state.get("total_output_tokens", 0) + out_tok

        if new_in > CONFIG.cost.session_token_budget:
            logger.warning("Session token budget exceeded: %d tokens", new_in)

        logger.info(
            "tokens | session=%s in=%d out=%d",
            state.get("session_id", "?"), new_in, new_out,
        )

        return {"total_input_tokens": new_in, "total_output_tokens": new_out}


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self):
        self._failures  = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at > CONFIG.resilience.circuit_breaker_timeout:
            self._failures  = 0
            self._opened_at = None
            logger.info("Circuit breaker reset (half-open)")
            return False
        return True

    def record_success(self):
        self._failures  = 0
        self._opened_at = None

    def record_failure(self):
        self._failures += 1
        if self._failures >= CONFIG.resilience.circuit_breaker_threshold:
            self._opened_at = time.time()
            logger.error("Circuit breaker OPENED after %d failures", self._failures)


sap_circuit_breaker = CircuitBreaker()


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY EXTRACTION  (compiled regex — fix #5)
# ─────────────────────────────────────────────────────────────────────────────

_CATEGORY_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\btyre|tire\b",       re.I), "tyres"),
    (re.compile(r"\bwheel\b",           re.I), "wheels"),
    (re.compile(r"\bcamera\b",          re.I), "cameras"),
    (re.compile(r"\blens\b",            re.I), "lenses"),
    (re.compile(r"\boil\b",             re.I), "engine_oil"),
    (re.compile(r"\bengine\b",          re.I), "engine"),
    (re.compile(r"\balignment\b",       re.I), "alignment"),
    (re.compile(r"\bservice\b",         re.I), "service"),
    (re.compile(r"\bmud\b",             re.I), "accessories"),
    (re.compile(r"\boffroad|off.road\b",re.I), "off_road"),
    (re.compile(r"\bbattery|batteries\b",re.I),"battery"),
    (re.compile(r"\bbrake|brakes\b",    re.I), "brakes"),
    (re.compile(r"\bfilter\b",          re.I), "filters"),
]


def _extract_category(text: str) -> str:
    for pattern, cat in _CATEGORY_MAP:
        if pattern.search(text):
            return cat
    return "general"


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH NODES
# ─────────────────────────────────────────────────────────────────────────────

def agent_node(state: ShoppingState, config: RunnableConfig) -> dict:
    """
    Main reasoning node.
    - Builds context-aware system prompt (with CF recommendations).
    - Trims context window and validates tool-call pairs.
    - Calls LLM with tool binding and tracks token usage.
    """
    session_id = state.get("session_id", "?")

    if sap_circuit_breaker.is_open:
        logger.warning(
            "agent_node | session=%s | circuit breaker OPEN — skipping LLM call",
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

    logger.debug(
        "agent_node | session=%s | turn=%d | messages_in_context=%d",
        session_id,
        state.get("turn_count", 0),
        len(all_msgs),
    )

    try:
        response      = _llm_invoke_with_retry(_llm_with_tools, all_msgs, config)
        token_updates = TokenTracker.update(state, response)
        sap_circuit_breaker.record_success()

        logger.debug(
            "agent_node | session=%s | tool_calls=%s",
            session_id,
            [tc["name"] for tc in (response.tool_calls or [])],
        )

        return {
            "messages":          [response],
            "turn_count":        state.get("turn_count", 0) + 1,
            "last_error":        None,
            "consecutive_errors": 0,
            **token_updates,
        }

    except Exception as exc:
        if _is_overload_error(exc):
            logger.error(
                "agent_node | LLM overloaded after %d retries | session=%s",
                _OVERLOAD_MAX_RETRIES, session_id,
            )
            audit("API_ERROR", session_id, {
                "error":            "overloaded_error",
                "context":          "llm_call",
                "retries_exhausted": _OVERLOAD_MAX_RETRIES,
            })
            return {
                "messages": [AIMessage(content=(
                    "The AI service is currently under heavy load. "
                    "Please wait a moment and try again."
                ))],
                "last_error":        "overloaded_error",
                "consecutive_errors": state.get("consecutive_errors", 0),
            }

        if _is_ssl_error(exc):
            _log_ssl_error(exc, context="llm_call")
            audit("API_ERROR", session_id, {"error": str(exc.__cause__ or exc), "ssl": True})
        else:
            logger.exception("agent_node | LLM call failed | session=%s", session_id)

        sap_circuit_breaker.record_failure()

        consecutive = state.get("consecutive_errors", 0) + 1
        msg = (
            "I'm experiencing repeated issues. Please contact support."
            if consecutive >= 3
            else f"I hit a snag ({type(exc).__name__}). Could you rephrase?"
        )

        return {
            "messages":          [AIMessage(content=msg)],
            "last_error":        str(exc),
            "consecutive_errors": consecutive,
        }


def human_approval_node(state: ShoppingState) -> dict:
    """
    Human-in-the-loop (LangGraph interrupt).
    Pauses graph execution before place_order and waits for explicit approval.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    rejected_tool_calls = []

    for tc in last.tool_calls:
        if tc["name"] == "place_order":
            cart_id  = tc["args"].get("cart_id", "?")
            approval = interrupt({
                "type":         "order_confirmation",
                "message":      f"Ready to place order for cart {cart_id}. Confirm?",
                "tool_call_id": tc["id"],
                "args":         tc["args"],
            })
            if not approval.get("approved"):
                audit("ORDER_REJECTED", state.get("session_id", "?"), tc["args"])
                rejected_tool_calls.append(tc["id"])
            else:
                audit("ORDER_APPROVED", state.get("session_id", "?"), tc["args"])

    return {"rejected_tool_calls": rejected_tool_calls} if rejected_tool_calls else {}


def state_sync_node(state: ShoppingState) -> dict:
    """Scan the most recent tool results and persist important state fields."""
    updates    = {}
    session_id = state.get("session_id", "?")

    for msg in reversed(state["messages"]):
        if not isinstance(msg, ToolMessage):
            break
        try:
            result = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "state_sync_node | session=%s | non-JSON ToolMessage: %r",
                session_id, msg.content[:200],
            )
            continue

        if not result.get("success"):
            error_str = str(result.get("error", ""))
            if "CERTIFICATE_VERIFY_FAILED" in error_str or "SSL" in error_str.upper():
                logger.error(
                    "SSL_ERROR | context=tool_result | session=%s | error=%s",
                    session_id, error_str,
                )
                audit("API_ERROR", session_id, {
                    "error":        error_str,
                    "context":      "sap_tool_call",
                    "tool_call_id": msg.tool_call_id,
                    "ssl":          True,
                })
            else:
                logger.warning(
                    "state_sync_node | session=%s | tool_call_id=%s | error=%s",
                    session_id, msg.tool_call_id, error_str or result,
                )
            sap_circuit_breaker.record_failure()
            continue

        sap_circuit_breaker.record_success()

        if "access_token" in result:
            updates["access_token"] = result["access_token"]
        if "username" in result:
            updates["username"] = result["username"]
            updates["user_id"]  = "current"
        if result.get("cart_id"):
            updates["cart_id"] = result["cart_id"]
        if result.get("user_id") and "username" not in result:
            updates["user_id"] = result["user_id"]
        if "order_code" in result:
            updates["order_code"] = result["order_code"]
        if result.get("entry_number") is not None and result.get("success"):
            updates["last_added_product"] = result.get("product_code", "")

    if updates:
        logger.debug(
            "state_sync_node | session=%s | updated fields=%s",
            session_id, list(updates.keys()),
        )

    return updates


def memory_node(state: ShoppingState) -> dict:
    """
    Memory + Collaborative Filtering node — runs after every completed turn.

    1. Find the last human message.
    2. Skip if it's the same as the last saved query (de-dup — fix #7).
    3. Save add_to_cart interactions from tool results.
    4. Save search interactions based on keyword heuristics.
    5. Fetch updated CF recommendations for the next turn.

    All Qdrant calls are individually wrapped in try/except so a storage
    failure never crashes a turn (fix #2).
    """
    username   = state.get("username") or "anonymous"
    messages   = state.get("messages", [])
    session_id = state.get("session_id", "?")

    # Find the last human message
    last_human = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    if not last_human:
        logger.debug("memory_node | session=%s | no human message, skipping", session_id)
        return {}

    # De-duplicate: skip if this query was already saved this session (#7)
    if last_human == state.get("last_saved_query"):
        logger.debug(
            "memory_node | session=%s | duplicate query skipped for user=%s",
            session_id, username,
        )
    else:
        category = _extract_category(last_human)

        # Check most recent ToolMessage for an add_to_cart result
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage):
                try:
                    result = json.loads(msg.content)
                    if result.get("success") and result.get("entry_number") is not None:
                        try:
                            save_user_interaction(username, {
                                "action":       "add_to_cart",
                                "query":        last_human,
                                "product_code": state.get("last_added_product") or "",
                                "category":     category,
                            })
                            logger.debug(
                                "memory_node | session=%s | saved add_to_cart for user=%s",
                                session_id, username,
                            )
                        except Exception as save_err:
                            logger.warning(
                                "memory_node | session=%s | save_user_interaction failed: %s",
                                session_id, save_err,
                            )
                except Exception as parse_err:
                    logger.warning(
                        "memory_node | session=%s | tool result parse error: %s",
                        session_id, parse_err,
                    )
                break  # only inspect the most recent ToolMessage

        # Save search interaction
        _SEARCH_KEYWORDS = frozenset(
            {"search", "show", "find", "looking", "want", "need", "get",
             "buy", "price", "cost", "available", "stock"}
        )
        if any(w in last_human.lower() for w in _SEARCH_KEYWORDS):
            try:
                save_user_interaction(username, {
                    "action":   "search",
                    "query":    last_human,
                    "category": category,
                })
                logger.debug(
                    "memory_node | session=%s | saved search for user=%s",
                    session_id, username,
                )
            except Exception as save_err:
                logger.warning(
                    "memory_node | session=%s | search save failed: %s",
                    session_id, save_err,
                )

    # Generate CF recommendations for the next turn
    # cf_updates: dict = {"last_saved_query": last_human}
    # try:
    #     cf_recs = get_cf_recommendations(username, last_human, top_k=5)
    #     if cf_recs:
    #         logger.debug(
    #             "memory_node | session=%s | %d CF recs for user=%s",
    #             session_id, len(cf_recs), username,
    #         )
    #         cf_updates["cf_recommendations"] = cf_recs
    # except Exception as cf_err:
    #     logger.warning(
    #         "memory_node | session=%s | get_cf_recommendations failed: %s",
    #         session_id, cf_err,
    #     )

    # return cf_updates



     # Generate hybrid CF+CBF recommendations for the next turn
    cf_updates: dict = {"last_saved_query": last_human}
    try:
        from hybrid_recommender import get_hybrid_recommendations, hybrid_recs_for_state
        hybrid_recs = get_hybrid_recommendations(
            username      = username,
            current_query = last_human,
            top_k         = 5,
        )
        if hybrid_recs:
            logger.debug(
                "memory_node | session=%s | %d hybrid recs for user=%s",
                session_id, len(hybrid_recs), username,
            )
            cf_updates["cf_recommendations"] = hybrid_recs_for_state(hybrid_recs)
    except Exception as rec_err:
        logger.warning(
            "memory_node | session=%s | hybrid recommendations failed: %s",
            session_id, rec_err,
        )
 
    return cf_updates


# ─────────────────────────────────────────────────────────────────────────────
# TOOL NODE WITH ACCESS-TOKEN INJECTION
# ─────────────────────────────────────────────────────────────────────────────

_raw_tool_node = ToolNode(ALL_TOOLS)


def tool_node_with_injection(state: ShoppingState) -> dict:
    """
    Injects access_token from session state into every non-rejected tool call
    so the LLM never needs to handle auth tokens directly.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return _raw_tool_node.invoke(state)

    access_token        = state.get("access_token") or ""
    rejected_tool_calls = state.get("rejected_tool_calls") or []

    # Produce rejection ToolMessages for cancelled calls
    tool_results = [
        ToolMessage(
            content=json.dumps({"success": False, "reason": "User cancelled order."}),
            tool_call_id=tc["id"],
        )
        for tc in last.tool_calls
        if tc["id"] in rejected_tool_calls
    ]

    # Patch access_token into surviving calls
    patched_calls = []
    for tc in last.tool_calls:
        if tc["id"] in rejected_tool_calls:
            continue
        args = dict(tc.get("args", {}))
        if access_token:
            args["access_token"] = access_token
            logger.debug(
                "tool_injection | %s | injected access_token (len=%d)",
                tc.get("name"), len(access_token),
            )
        patched_calls.append({**tc, "args": args})

    if not patched_calls:
        return {"messages": tool_results, "rejected_tool_calls": None}

    patched_msg   = AIMessage(content=last.content, tool_calls=patched_calls, id=last.id)
    patched_state = {**state, "messages": state["messages"][:-1] + [patched_msg]}
    result        = _raw_tool_node.invoke(patched_state)

    if tool_results:
        result["messages"] = tool_results + result.get("messages", [])

    result["rejected_tool_calls"] = None
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING
# ─────────────────────────────────────────────────────────────────────────────

def route_after_agent(state: ShoppingState) -> str:
    msgs = state.get("messages", [])
    if not msgs:
        return "sync"
    last = msgs[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        for tc in last.tool_calls:
            if tc["name"] == "place_order":
                return "human_approval"
        return "tools"
    return "sync"


def route_after_sync(state: ShoppingState) -> str:
    """
    Return "agent" if there are pending tool results to process;
    otherwise end the turn (routing to memory node via conditional edge).
    Fix #8: guards against empty messages list.
    """
    msgs = state.get("messages", [])
    if msgs and isinstance(msgs[-1], ToolMessage):
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
    g.add_node("memory",         memory_node)

    g.add_edge(START, "agent")

    g.add_conditional_edges("agent", route_after_agent, {
        "human_approval": "human_approval",
        "tools":          "tools",
        "sync":           "sync",
    })

    g.add_edge("human_approval", "tools")
    g.add_edge("tools",          "sync")

    # After sync: if tool results still pending → back to agent.
    # Otherwise → memory node → END.
    g.add_conditional_edges("sync", route_after_sync, {
        "agent": "agent",
        END:     "memory",
    })

    g.add_edge("memory", END)

    return g.compile(checkpointer=MemorySaver(), interrupt_before=[])


production_graph = build_production_graph()


# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGEMENT  (fix #3 — always define token variables)
# ─────────────────────────────────────────────────────────────────────────────

def new_session(user_id: str = "anonymous") -> tuple[ShoppingState, str]:
    """
    Create a fresh session. Returns (initial_state, thread_id).

    In dev mode a SAP_STATIC_TOKEN can be set in .env to bypass OAuth.
    In production the token is acquired by the UI login flow.
    """
    thread_id = str(uuid.uuid4())

    # Safe defaults — always defined regardless of branch taken (#3)
    access_token      = os.getenv("SAP_STATIC_TOKEN", "")
    resolved_user_id  = user_id
    resolved_username = os.getenv("SAP_STATIC_USERNAME", "guest")

    if access_token:
        resolved_user_id  = "current"
        resolved_username = os.getenv("SAP_STATIC_USERNAME", "lang-graph-user")
        logger.warning(
            "new_session | SAP_STATIC_TOKEN in use — dev mode only | session=%s",
            thread_id,
        )
    else:
        logger.info(
            "new_session | No static token — guest session | session=%s", thread_id
        )

    init_state = ShoppingState(
        messages=[],
        access_token=access_token or None,
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
        rejected_tool_calls=None,
        cf_recommendations=None,
        last_added_product=None,
        last_saved_query=None,  # NEW
    )

    audit("SESSION_START", thread_id, {
        "user_id":  resolved_user_id,
        "token_ok": bool(access_token),
    })

    logger.info(
        "new_session | session=%s | user_id=%s | authenticated=%s",
        thread_id, resolved_user_id, bool(access_token),
    )

    return init_state, thread_id


# ─────────────────────────────────────────────────────────────────────────────
# TURN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_turn(
    user_message: str,
    thread_id: str,
    state: ShoppingState,
    approval_response: Optional[dict] = None,
) -> ShoppingState:
    """
    Run one conversation turn with full security + observability pipeline.

    approval_response: pass {"approved": True/False} when resuming after
    a human-approval interrupt.
    """
    # Security gate
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

    clean     = sanitise_input(user_message)
    lg_config = RunnableConfig(configurable={"thread_id": thread_id})

    logger.debug(
        "run_turn | thread=%s | resuming=%s | message_len=%d",
        thread_id, bool(approval_response), len(clean),
    )

    try:
        if approval_response:
            new_state = production_graph.invoke(
                Command(resume=approval_response), config=lg_config
            )
        else:
            state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
            new_state = production_graph.invoke(state, config=lg_config)

        # LangGraph MemorySaver can overwrite access_token from stale
        # checkpoint data — restore from our in-memory session store.
        if state.get("access_token"):
            new_state["access_token"] = state["access_token"]
            new_state["user_id"]      = state.get("user_id", "current")

    except Exception as exc:
        if _is_ssl_error(exc):
            _log_ssl_error(exc, context="graph_invoke")
            audit("API_ERROR", thread_id, {"error": str(exc.__cause__ or exc), "ssl": True})
        else:
            logger.exception("run_turn | graph.invoke failed | thread=%s", thread_id)
            audit("API_ERROR", thread_id, {"error": str(exc)})
        raise

    audit("TURN_COMPLETE", thread_id, {
        "turn":         new_state.get("turn_count"),
        "input_tokens": new_state.get("total_input_tokens"),
        "output_tokens":new_state.get("total_output_tokens"),
    })

    return new_state


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def get_last_ai_message(state: ShoppingState) -> str:
    """
    Return the last visible AI response as a plain string.
    Handles both str content and list-of-content-blocks (Anthropic streaming
    format).
    """
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
            logger.warning(
                "get_last_ai_message | unexpected content type=%s",
                type(content).__name__,
            )
            return str(content)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# STREAMING (ASYNC) — fix #10
# ─────────────────────────────────────────────────────────────────────────────

async def stream_turn(
    user_message: str,
    thread_id: str,
    state: ShoppingState,
) -> AsyncIterator[str]:
    """
    Async streaming turn — yields text chunks as they arrive from the LLM.
    Wire into FastAPI / WebSocket for real-time UX.
    """
    clean = sanitise_input(user_message)
    state["messages"] = state.get("messages", []) + [HumanMessage(content=clean)]
    lg_config = RunnableConfig(configurable={"thread_id": thread_id})

    async for chunk in production_graph.astream(
        state, config=lg_config, stream_mode="messages"
    ):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg, _meta = chunk
            if isinstance(msg, AIMessage) and msg.content:
                yield str(msg.content)
        else:
            logger.debug(
                "stream_turn | unexpected chunk structure: type=%s value=%r",
                type(chunk).__name__, repr(chunk)[:120],
            )


# ─────────────────────────────────────────────────────────────────────────────
# GRACEFUL DEGRADATION
# ─────────────────────────────────────────────────────────────────────────────

_FALLBACK_RESPONSES = {
    "search":   "I can't search right now. Try visiting the store directly.",
    "checkout": "Checkout is temporarily unavailable. Your cart is saved.",
    "generic":  "I'm having trouble right now. Please try again in a moment.",
}


def get_fallback(intent: str = "generic") -> str:
    return _FALLBACK_RESPONSES.get(intent, _FALLBACK_RESPONSES["generic"])


# ─────────────────────────────────────────────────────────────────────────────
# CLI DEMO
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("SAP Commerce Agent (Production) — type 'quit' to exit\n")
    session_state, tid = new_session("demo-user-001")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() in ("quit", "exit"):
            break

        session_state = run_turn(user_input, tid, session_state)
        reply         = get_last_ai_message(session_state)
        print(f"\nAssistant: {reply}\n")

        if session_state.get("turn_count", 0) % 5 == 0:
            print(
                f"  [Tokens: in={session_state['total_input_tokens']} "
                f"out={session_state['total_output_tokens']}]\n"
            )