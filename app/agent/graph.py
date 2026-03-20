"""
LangGraph agent — graph definition, nodes, and routing.

This is the core agent orchestration. Nodes handle reasoning,
tool execution, human approval, and state synchronization.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from typing import AsyncIterator, Optional

from dotenv import load_dotenv

load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from app.agent.prompts import build_system_message
from app.agent.state import ShoppingState
from app.config import CONFIG
from app.middleware.audit import audit
from app.middleware.error_handler import (
    CircuitBreaker, is_overload_error, is_ssl_error, log_ssl_error, sap_circuit_breaker,
)
from app.middleware.security import detect_prompt_injection, rate_limiter, sanitise_input

logger = logging.getLogger("sap_agent")

# ── Tool loading ─────────────────────────────────────────────────────────────

def _load_tools():
    from app.integrations.mcp_client import get_tools_sync, get_mcp_session_id
    from app.integrations.qdrant_client import is_qdrant_configured, semantic_search_products
    from app.agent.tools import list_saved_cards, acp_checkout

    tools = list(get_tools_sync())

    # Always add ACP tools (Stripe-based, not in MCP server)
    mcp_tool_names = {t.name for t in tools}
    for local_tool in [list_saved_cards, acp_checkout]:
        if local_tool.name not in mcp_tool_names:
            tools.append(local_tool)
            print(f"Added local tool: {local_tool.name}")

    if is_qdrant_configured():
        tools.append(semantic_search_products)
        print("Qdrant semantic search enabled")
    else:
        print("Qdrant not configured — semantic search disabled")

    mcp_session_id = get_mcp_session_id()
    return tools, mcp_session_id


ALL_TOOLS, _MCP_SESSION_ID = _load_tools()


# ── LLM selection ────────────────────────────────────────────────────────────

def _create_llm():
    provider = CONFIG.llm_provider.lower()
    if provider == "gemini":
        logger.info("LLM provider: Google Gemini (%s)", CONFIG.gemini.model)
        return ChatGoogleGenerativeAI(
            model=CONFIG.gemini.model,
            google_api_key=CONFIG.gemini.api_key,
            max_output_tokens=CONFIG.gemini.max_tokens,
            temperature=CONFIG.gemini.temperature,
        )
    else:
        logger.info("LLM provider: Anthropic Claude (%s)", CONFIG.claude.model)
        rate_lim = InMemoryRateLimiter(
            requests_per_second=0.5, check_every_n_seconds=0.1, max_bucket_size=10,
        )
        return ChatAnthropic(
            base_url="https://api.anthropic.com",
            model=CONFIG.claude.model,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=CONFIG.claude.max_tokens,
            temperature=CONFIG.claude.temperature,
            timeout=CONFIG.claude.timeout_seconds,
            max_retries=CONFIG.claude.max_retries,
            streaming=CONFIG.claude.streaming,
            rate_limiter=rate_lim,
            default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )


_llm = _create_llm()
_llm_with_tools = _llm.bind_tools(ALL_TOOLS)

# Overload retry config
_OVERLOAD_MAX_RETRIES = int(os.getenv("ANTHROPIC_OVERLOAD_RETRIES", "4"))
_OVERLOAD_BASE_DELAY = float(os.getenv("ANTHROPIC_OVERLOAD_BASE_DELAY", "2.0"))


def _llm_invoke_with_retry(llm, messages, config):
    last_exc = None
    for attempt in range(_OVERLOAD_MAX_RETRIES + 1):
        try:
            return llm.invoke(messages, config=config)
        except Exception as exc:
            if not is_overload_error(exc):
                raise
            last_exc = exc
            if attempt == _OVERLOAD_MAX_RETRIES:
                break
            delay = _OVERLOAD_BASE_DELAY * (2 ** attempt)
            jitter = delay * 0.25 * (2 * random.random() - 1)
            wait = round(delay + jitter, 2)
            logger.warning("Anthropic overloaded | attempt=%d/%d | retrying in %.1fs",
                           attempt + 1, _OVERLOAD_MAX_RETRIES, wait)
            time.sleep(wait)
    logger.error("Anthropic still overloaded after %d retries", _OVERLOAD_MAX_RETRIES)
    raise last_exc


# ── Token Tracking ───────────────────────────────────────────────────────────

class TokenTracker:
    INPUT_COST_PER_1K = 0.003
    OUTPUT_COST_PER_1K = 0.015
    CACHE_READ_PER_1K = 0.0003

    @staticmethod
    def update(state: ShoppingState, response: AIMessage) -> dict:
        usage = response.usage_metadata or {}
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        cached = usage.get("cache_read_input_tokens", 0)

        new_in = state.get("total_input_tokens", 0) + in_tokens
        new_out = state.get("total_output_tokens", 0) + out_tokens

        cost = (
            (in_tokens / 1000) * TokenTracker.INPUT_COST_PER_1K +
            (out_tokens / 1000) * TokenTracker.OUTPUT_COST_PER_1K +
            (cached / 1000) * TokenTracker.CACHE_READ_PER_1K
        )

        if new_in > CONFIG.cost.session_token_budget:
            logger.warning("Session token budget exceeded: %d tokens", new_in)

        logger.info("tokens | session=%s in=%d out=%d cached=%d cost_usd=%.4f",
                     state.get("session_id", "?"), new_in, new_out, cached, cost)
        return {"total_input_tokens": new_in, "total_output_tokens": new_out}


# ── Context Trimming ─────────────────────────────────────────────────────────

def _trim_context(messages: list[BaseMessage]) -> list[BaseMessage]:
    max_msgs = CONFIG.resilience.max_messages_in_context
    if len(messages) <= max_msgs:
        return _validate_tool_message_pairs(messages)

    trimmed = messages[-max_msgs:]
    for i, msg in enumerate(trimmed):
        if isinstance(msg, HumanMessage):
            trimmed = trimmed[i:]
            break
    while trimmed and isinstance(trimmed[0], ToolMessage):
        trimmed = trimmed[1:]

    return _validate_tool_message_pairs(trimmed)


def _validate_tool_message_pairs(messages: list[BaseMessage]) -> list[BaseMessage]:
    safe = []
    pending_tool_calls = {}

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
            safe.append(msg)

    if not pending_tool_calls:
        return safe

    # Remove unmatched tool calls AND their tool_use content blocks
    final = []
    for msg in safe:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            matched = [tc for tc in msg.tool_calls if tc["id"] not in pending_tool_calls]
            if matched:
                matched_ids = {tc["id"] for tc in matched}
                clean_content = _strip_tool_use_from_content(msg.content, keep_ids=matched_ids)
                final.append(AIMessage(content=clean_content, tool_calls=matched, id=msg.id))
            else:
                clean_content = _strip_tool_use_from_content(msg.content)
                if clean_content:
                    final.append(AIMessage(content=clean_content, id=msg.id))
        else:
            final.append(msg)
    return final


# ── Message Sanitization ─────────────────────────────────────────────────────

def _strip_tool_use_from_content(content, keep_ids: set | None = None):
    """Remove tool_use blocks from AIMessage content.

    LangChain stores tool calls in BOTH msg.content (as tool_use blocks)
    AND msg.tool_calls. If we strip tool_calls, we must also strip the
    matching tool_use blocks from content, otherwise langchain_anthropic
    will still send them to the API and create orphaned tool_uses.
    """
    if not isinstance(content, list):
        return content
    filtered = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            # Keep this tool_use only if its ID is in keep_ids
            if keep_ids is not None and block.get("id") in keep_ids:
                filtered.append(block)
            # Otherwise drop it
        else:
            filtered.append(block)
    # If only text blocks remain, extract plain string if single
    if len(filtered) == 1 and isinstance(filtered[0], dict) and filtered[0].get("type") == "text":
        return filtered[0].get("text", "")
    return filtered or ""


def _sanitize_tool_pairs(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Ensure every tool_use has matching tool_result(s) immediately after.

    The Anthropic API requires that each AIMessage with tool_calls is followed
    immediately by ToolMessages for ALL of those calls. This function:
    1. Strips orphaned tool_calls (no matching result immediately after)
    2. Strips orphaned ToolMessages (no matching call)
    3. Also strips tool_use blocks from AIMessage.content (langchain_anthropic
       sends both content tool_use blocks AND tool_calls to the API)
    """
    result = []
    consumed = set()  # ToolMessage indices already consumed
    i = 0

    while i < len(messages):
        msg = messages[i]

        if isinstance(msg, AIMessage) and msg.tool_calls:
            # Collect contiguous ToolMessages immediately after this AIMessage
            contiguous_results = {}
            j = i + 1
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                tcid = getattr(messages[j], "tool_call_id", None)
                if tcid:
                    contiguous_results[tcid] = j
                j += 1

            # Keep only tool_calls whose results are in the contiguous block
            valid_calls = [tc for tc in msg.tool_calls if tc["id"] in contiguous_results]
            valid_ids = {tc["id"] for tc in valid_calls}

            if valid_calls:
                clean_content = _strip_tool_use_from_content(msg.content, keep_ids=valid_ids)
                result.append(AIMessage(content=clean_content, tool_calls=valid_calls, id=msg.id))
                for tc in valid_calls:
                    idx = contiguous_results[tc["id"]]
                    result.append(messages[idx])
                    consumed.add(idx)
            else:
                # All tool_calls orphaned — strip everything
                clean_content = _strip_tool_use_from_content(msg.content)
                if clean_content:
                    result.append(AIMessage(content=clean_content, id=msg.id))
            # Skip to after the contiguous ToolMessage block
            i = j
            continue

        elif isinstance(msg, ToolMessage):
            if i not in consumed:
                # Orphaned ToolMessage — skip it
                i += 1
                continue

        else:
            result.append(msg)

        i += 1

    return result


# ── Graph Nodes ──────────────────────────────────────────────────────────────

def agent_node(state: ShoppingState, config: RunnableConfig) -> dict:
    session_id = state.get("session_id", "?")

    if sap_circuit_breaker.is_open:
        return {
            "messages": [AIMessage(content="I'm having trouble connecting to the store right now. Please try again in a moment.")],
            "last_error": "circuit_breaker_open",
        }

    system_msg = build_system_message(state, _MCP_SESSION_ID or "")
    trimmed = _trim_context(state["messages"])
    sanitized = _sanitize_tool_pairs(trimmed)
    all_msgs = [system_msg] + sanitized

    # Debug: verify no orphaned tool_use remains
    for idx, m in enumerate(sanitized):
        if isinstance(m, AIMessage):
            if m.tool_calls:
                tc_ids = [tc["id"] for tc in m.tool_calls]
                logger.debug("msg[%d] AIMessage tool_calls=%s", idx, tc_ids)
            if isinstance(m.content, list):
                tu_ids = [b.get("id") for b in m.content
                          if isinstance(b, dict) and b.get("type") == "tool_use"]
                if tu_ids:
                    logger.warning("msg[%d] AIMessage has tool_use in content: %s", idx, tu_ids)

    try:
        response = _llm_invoke_with_retry(_llm_with_tools, all_msgs, config)
        token_updates = TokenTracker.update(state, response)
        sap_circuit_breaker.record_success()

        return {
            "messages": [response],
            "turn_count": state.get("turn_count", 0) + 1,
            "last_error": None,
            "consecutive_errors": 0,
            **token_updates,
        }

    except Exception as exc:
        if is_overload_error(exc):
            audit("API_ERROR", session_id, {"error": "overloaded_error"})
            return {
                "messages": [AIMessage(content="The AI service is under heavy load. Please try again.")],
                "last_error": "overloaded_error",
                "consecutive_errors": state.get("consecutive_errors", 0),
            }

        if is_ssl_error(exc):
            log_ssl_error(exc, "anthropic_llm_call")
        else:
            logger.exception("agent_node | LLM call failed | session=%s", session_id)

        sap_circuit_breaker.record_failure()
        consecutive = state.get("consecutive_errors", 0) + 1
        msg = ("I'm experiencing repeated issues. Please contact support."
               if consecutive >= 3
               else f"I hit a snag ({type(exc).__name__}). Let me try again — could you rephrase?")

        return {
            "messages": [AIMessage(content=msg)],
            "last_error": str(exc),
            "consecutive_errors": consecutive,
        }


def human_approval_node(state: ShoppingState) -> dict:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {}

    rejected_tool_calls = []
    for tc in last.tool_calls:
        if tc["name"] in ("place_order", "acp_checkout"):
            cart_id = tc["args"].get("cart_id", "?")
            label = "one-click purchase" if tc["name"] == "acp_checkout" else "order"
            approval = interrupt({
                "type": "order_confirmation",
                "message": f"Ready to complete {label} for cart {cart_id}. Confirm?",
                "tool_call_id": tc["id"],
                "args": tc["args"],
            })
            if not approval.get("approved"):
                audit("ORDER_REJECTED", state.get("session_id", "?"), tc["args"])
                rejected_tool_calls.append(tc["id"])
            else:
                audit("ORDER_APPROVED", state.get("session_id", "?"), tc["args"])

    return {"rejected_tool_calls": rejected_tool_calls} if rejected_tool_calls else {}


def state_sync_node(state: ShoppingState) -> dict:
    updates: dict = {}
    session_id = state.get("session_id", "?")

    for msg in reversed(state["messages"]):
        if not isinstance(msg, ToolMessage):
            break
        try:
            result = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            continue

        if not result.get("success"):
            sap_circuit_breaker.record_failure()
            continue

        sap_circuit_breaker.record_success()
        if "access_token" in result:
            updates["access_token"] = result["access_token"]
        if "username" in result:
            updates["username"] = result["username"]
            updates["user_id"] = "current"
        if result.get("cart_id"):
            updates["cart_id"] = result["cart_id"]
        if result.get("user_id") and "username" not in result:
            updates["user_id"] = result["user_id"]
        if "order_code" in result:
            updates["order_code"] = result["order_code"]
        # Track Stripe checkout state
        if result.get("stripe_session_id"):
            updates["stripe_checkout_session_id"] = result["stripe_session_id"]
        if result.get("payment_url"):
            updates["stripe_payment_url"] = result["payment_url"]
        if result.get("checkout_status"):
            updates["checkout_status"] = result["checkout_status"]
        # Sync saved cards from list_saved_cards tool
        if "cards" in result and isinstance(result["cards"], list):
            updates["saved_payment_methods"] = result["cards"]
        # Capture product search results for structured API response
        if "products" in result and isinstance(result["products"], list):
            updates["last_search_results"] = result["products"]

    return updates


# ── Tool Node with Token Injection ───────────────────────────────────────────

_raw_tool_node = ToolNode(ALL_TOOLS)


def tool_node_with_injection(state: ShoppingState) -> dict:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return _raw_tool_node.invoke(state)

    access_token = state.get("access_token") or ""
    rejected_tool_calls = state.get("rejected_tool_calls") or []

    # Rejected tool results
    tool_results = []
    for tc in last.tool_calls:
        if tc["id"] in rejected_tool_calls:
            tool_results.append(ToolMessage(
                content=json.dumps({"success": False, "reason": "User cancelled order."}),
                tool_call_id=tc["id"],
            ))

    # Non-rejected: inject access_token
    patched_calls = []
    for tc in last.tool_calls:
        if tc["id"] not in rejected_tool_calls:
            args = dict(tc.get("args", {}))
            if access_token:
                args["access_token"] = access_token
            patched_calls.append({**tc, "args": args})

    if not patched_calls:
        return {"messages": tool_results, "rejected_tool_calls": None}

    patched_msg = AIMessage(content=last.content, tool_calls=patched_calls, id=last.id)
    patched_state = {**state, "messages": state["messages"][:-1] + [patched_msg]}

    try:
        result = _raw_tool_node.invoke(patched_state)
    except Exception as e:
        logger.exception("tool_node_with_injection | ToolNode.invoke failed")
        # Produce error ToolMessages so tool_use/tool_result pairing is preserved
        error_msgs = []
        for tc in patched_calls:
            error_msgs.append(ToolMessage(
                content=json.dumps({"success": False, "error": f"Tool execution failed: {e}"}),
                tool_call_id=tc["id"],
            ))
        result = {"messages": error_msgs}

    if tool_results:
        result["messages"] = tool_results + result.get("messages", [])
    result["rejected_tool_calls"] = None
    return result


# ── Routing ──────────────────────────────────────────────────────────────────

def route_after_agent(state: ShoppingState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        for tc in last.tool_calls:
            if tc["name"] in ("place_order", "acp_checkout"):
                return "human_approval"
        return "tools"
    return "sync"


def route_after_sync(state: ShoppingState) -> str:
    last = state["messages"][-1]
    return "agent" if isinstance(last, ToolMessage) else END


# ── Graph Assembly ───────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(ShoppingState)

    g.add_node("agent", agent_node)
    g.add_node("human_approval", human_approval_node)
    g.add_node("tools", tool_node_with_injection)
    g.add_node("sync", state_sync_node)

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {
        "human_approval": "human_approval",
        "tools": "tools",
        "sync": "sync",
    })
    g.add_edge("human_approval", "tools")
    g.add_edge("tools", "sync")
    g.add_conditional_edges("sync", route_after_sync, {"agent": "agent", END: END})

    checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer, interrupt_before=[])


production_graph = build_graph()
