"""
LangGraph agent — graph definition, nodes, and routing.

This is the core agent orchestration. Nodes handle reasoning,
tool execution, human approval, and state synchronization.

Debug logging
=============
Set LOG_LEVEL=DEBUG or LOG_LEVEL_OVERRIDES=sap_agent:DEBUG to see:
  - Full LLM request messages (all_msgs sent to the model)
  - Full LLM response (content + tool_calls)
  - State snapshot at each node entry
  - Tool call arguments and results
"""

from __future__ import annotations

import json
import logging

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from langgraph.types import interrupt  # noqa: E402

from app.agent.llm import LLMFactory  # noqa: E402
from app.agent.prompts import build_system_messages  # noqa: E402
from app.agent.state import ShoppingState  # noqa: E402
from app.config import CONFIG  # noqa: E402
from app.middleware.audit import audit  # noqa: E402
from app.middleware.error_handler import (  # noqa: E402
    is_overload_error, is_ssl_error, log_ssl_error, sap_circuit_breaker,
)

logger = logging.getLogger("sap_agent")

# ── Tool loading ─────────────────────────────────────────────────────────────

def _load_tools():
    from app.integrations.mcp_client import get_tools_sync, get_mcp_session_id
    from app.integrations.qdrant_client import is_qdrant_configured, semantic_search_products
    from app.agent.tools import list_saved_cards, acp_checkout, get_order_history, get_saved_addresses

    tools = list(get_tools_sync())

    # Always add local tools (not in MCP server)
    mcp_tool_names = {t.name for t in tools}
    for local_tool in [list_saved_cards, acp_checkout, get_order_history, get_saved_addresses]:
        if local_tool.name not in mcp_tool_names:
            tools.append(local_tool)
            print(f"Added local tool: {local_tool.name}")

    if is_qdrant_configured():
        tools.append(semantic_search_products)
        print("Qdrant semantic search enabled")
    else:
        print("Qdrant not configured — semantic search disabled")

    # Load tools from pluggable features (recommendations, etc.)
    try:
        from app.features.registry import FeatureRegistry
        feature_tools = FeatureRegistry.instance().get_all_tools()
        tool_names = {t.name for t in tools}
        for ft in feature_tools:
            if ft.name not in tool_names:
                tools.append(ft)
                print(f"Added feature tool: {ft.name}")
    except Exception as e:
        print(f"Feature tools not loaded: {e}")

    mcp_session_id = get_mcp_session_id()
    return tools, mcp_session_id


ALL_TOOLS, _MCP_SESSION_ID = _load_tools()


# ── LLM (injected from LLMFactory) ───────────────────────────────────────────

_llm_factory = LLMFactory()
_llm_with_tools = _llm_factory.bind_tools(ALL_TOOLS)


# ── Context Trimming ─────────────────────────────────────────────────────────

def _estimate_tokens(msg: BaseMessage) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    content = msg.content
    if isinstance(content, str):
        chars = len(content)
    elif isinstance(content, list):
        chars = sum(len(str(b)) for b in content)
    else:
        chars = len(str(content))
    # Add overhead for tool calls
    if isinstance(msg, AIMessage) and msg.tool_calls:
        chars += sum(len(str(tc.get("args", {}))) for tc in msg.tool_calls)
    return max(chars // 4, 1)


def _trim_context(messages: list[BaseMessage]) -> list[BaseMessage]:
    max_msgs = CONFIG.resilience.max_messages_in_context
    # Reserve ~3000 tokens for system prompt + dynamic context + output buffer
    max_context_tokens = CONFIG.claude.max_input_tokens - 3000

    # First: trim by message count
    if len(messages) > max_msgs:
        messages = messages[-max_msgs:]

    # Second: trim by estimated token count (drop oldest messages)
    total_tokens = sum(_estimate_tokens(m) for m in messages)
    while total_tokens > max_context_tokens and len(messages) > 4:
        dropped = _estimate_tokens(messages[0])
        messages = messages[1:]
        total_tokens -= dropped

    # Realign to start with HumanMessage
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            messages = messages[i:]
            break

    # Drop leading orphaned ToolMessages
    while messages and isinstance(messages[0], ToolMessage):
        messages = messages[1:]

    if total_tokens > max_context_tokens:
        logger.warning("context_trim | still %d est. tokens after trim (limit %d)",
                       total_tokens, max_context_tokens)

    return _validate_tool_message_pairs(messages)


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


# ── Debug helpers ────────────────────────────────────────────────────────────

def _dump_state(state: ShoppingState) -> str:
    """Full JSON state dump — everything except messages (logged separately)."""
    snapshot = {}
    for k, v in state.items():
        if k == "messages":
            snapshot["messages_count"] = len(v) if v else 0
            continue
        # Redact access_token value but show presence
        if k == "access_token":
            snapshot[k] = f"***({len(v)}chars)" if v else None
            continue
        snapshot[k] = v
    try:
        return json.dumps(snapshot, default=str, ensure_ascii=False)
    except Exception:
        return str(snapshot)


def _dump_msg(msg: BaseMessage, truncate: int = 0) -> str:
    """Full message dump. Set truncate>0 to cap content length."""
    kind = type(msg).__name__
    content = msg.content

    # Full content unless truncate is set
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                text = b.get("text", "")
                if truncate and len(text) > truncate:
                    text = text[:truncate] + f"...[{len(b.get('text',''))} total chars]"
                parts.append(text)
            elif isinstance(b, str):
                t = b if not truncate else b[:truncate]
                parts.append(t)
            else:
                parts.append(json.dumps(b, default=str)[:500])
        content_str = "\n".join(parts)
    elif isinstance(content, str):
        content_str = content if not truncate or len(content) <= truncate else content[:truncate] + f"...[{len(content)} total chars]"
    else:
        content_str = str(content)

    extras = ""
    if isinstance(msg, AIMessage) and msg.tool_calls:
        calls_detail = []
        for tc in msg.tool_calls:
            args_str = json.dumps(tc.get("args", {}), default=str)
            calls_detail.append(f"  {tc['name']}(id={tc['id']}) args={args_str}")
        extras = "\n  tool_calls:\n" + "\n".join(calls_detail)
    if isinstance(msg, ToolMessage):
        extras = f" [tool_call_id={getattr(msg, 'tool_call_id', '?')}]"

    return f"[{kind}] {content_str}{extras}"


# ── Graph Nodes ──────────────────────────────────────────────────────────────

def agent_node(state: ShoppingState, config: RunnableConfig) -> dict:
    session_id = state.get("session_id", "?")

    # Reset tool loop counter at the start of a new human turn
    last_msg = state["messages"][-1] if state.get("messages") else None
    extra_updates = {}
    if isinstance(last_msg, HumanMessage):
        extra_updates["tool_loops_this_turn"] = 0

    if sap_circuit_breaker.is_open:
        return {
            "messages": [AIMessage(content="I'm having trouble connecting to the store right now. Please try again in a moment.")],
            "last_error": "circuit_breaker_open",
        }

    # ── DEBUG: full state snapshot ──────────────────────────────────────
    logger.debug("agent_node STATE | %s", _dump_state(state))

    system_msgs = build_system_messages(state, _MCP_SESSION_ID or "", _llm_factory.provider)
    trimmed = _trim_context(state["messages"])
    sanitized = _sanitize_tool_pairs(trimmed)
    all_msgs = system_msgs + sanitized

    # ── DEBUG: full LLM request (every message, full content) ────────────
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("agent_node LLM_REQUEST | %d messages to %s model:",
                     len(all_msgs), _llm_factory.provider)
        for idx, m in enumerate(all_msgs):
            logger.debug("  msg[%d] %s", idx, _dump_msg(m))

    # Verify no orphaned tool_use remains
    for idx, m in enumerate(sanitized):
        if isinstance(m, AIMessage) and isinstance(m.content, list):
            tu_ids = [b.get("id") for b in m.content
                      if isinstance(b, dict) and b.get("type") == "tool_use"]
            if tu_ids:
                logger.warning("msg[%d] AIMessage has tool_use in content: %s", idx, tu_ids)

    try:
        response = _llm_factory.invoke_with_retry(_llm_with_tools, all_msgs, config)
        token_updates = _llm_factory.track_tokens(state, response)
        sap_circuit_breaker.record_success()

        # ── DEBUG: full LLM response (complete content + tool calls) ─────
        logger.debug("agent_node LLM_RESPONSE | %s", _dump_msg(response))

        return {
            "messages": [response],
            "turn_count": state.get("turn_count", 0) + 1,
            "last_error": None,
            "consecutive_errors": 0,
            **token_updates,
            **extra_updates,
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
    logger.debug("state_sync_node STATE | %s", _dump_state(state))
    updates: dict = {}
    has_any_success = False
    has_any_failure = False

    for msg in reversed(state["messages"]):
        if not isinstance(msg, ToolMessage):
            break
        try:
            result = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            continue

        if not result.get("success"):
            sap_circuit_breaker.record_failure()
            has_any_failure = True
            continue

        sap_circuit_breaker.record_success()
        has_any_success = True
        if "access_token" in result:
            updates["access_token"] = result["access_token"]
        if "username" in result:
            updates["username"] = result["username"]
            updates["user_id"] = "current"
        if result.get("cart_id"):
            # SAP Commerce requires GUID for anonymous cart URLs
            is_anonymous = state.get("user_id", "anonymous") == "anonymous"
            if is_anonymous and result.get("cart_guid"):
                updates["cart_id"] = result["cart_guid"]
            else:
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
        # Capture recommendation results as search results for product card rendering
        if "recommendations" in result and isinstance(result["recommendations"], list):
            updates["last_search_results"] = result["recommendations"]
        # Capture full cart data for structured cart card rendering
        if "entries" in result and isinstance(result["entries"], list):
            updates["last_cart_data"] = result
        # Capture product detail for structured detail card
        if "description" in result and "code" in result and "products" not in result and "entries" not in result:
            updates["last_product_detail"] = result

    # Track consecutive tool failures for loop detection.
    # Reset on any success; increment only on all-failure rounds.
    if has_any_success:
        updates["tool_loops_this_turn"] = 0
    elif has_any_failure:
        updates["tool_loops_this_turn"] = state.get("tool_loops_this_turn", 0) + 1

    # ── DEBUG: full state updates from sync ─────────────────────────────
    if logger.isEnabledFor(logging.DEBUG):
        safe = {k: v for k, v in updates.items() if k != "access_token"}
        logger.debug("state_sync UPDATES | %s", json.dumps(safe, default=str))

    return updates


# ── Tool Node with Token Injection ───────────────────────────────────────────

_raw_tool_node = ToolNode(ALL_TOOLS)


def tool_node_with_injection(state: ShoppingState) -> dict:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return _raw_tool_node.invoke(state)

    access_token = state.get("access_token") or ""
    rejected_tool_calls = state.get("rejected_tool_calls") or []

    # ── DEBUG: full tool call args ──────────────────────────────────────
    if logger.isEnabledFor(logging.DEBUG):
        for tc in last.tool_calls:
            safe_args = {k: v for k, v in tc.get("args", {}).items()
                         if k != "access_token"}
            logger.debug("tool_node CALL | tool=%s | id=%s | args=%s",
                         tc["name"], tc["id"],
                         json.dumps(safe_args, default=str))

    # Rejected tool results
    tool_results = []
    for tc in last.tool_calls:
        if tc["id"] in rejected_tool_calls:
            tool_results.append(ToolMessage(
                content=json.dumps({"success": False, "reason": "User cancelled order."}),
                tool_call_id=tc["id"],
            ))

    # Non-rejected: inject access_token + user_email
    user_email = state.get("user_email") or ""
    patched_calls = []
    for tc in last.tool_calls:
        if tc["id"] not in rejected_tool_calls:
            args = dict(tc.get("args", {}))
            if access_token:
                args["access_token"] = access_token
            if user_email and tc["name"] in ("get_personalized_recommendations",):
                args["user_email"] = user_email
            patched_calls.append({**tc, "args": args})

    if not patched_calls:
        return {"messages": tool_results, "rejected_tool_calls": None}

    patched_msg = AIMessage(content=last.content, tool_calls=patched_calls, id=last.id)
    patched_state = {**state, "messages": state["messages"][:-1] + [patched_msg]}

    try:
        result = _raw_tool_node.invoke(patched_state)
    except Exception as e:
        logger.exception("tool_node_with_injection | ToolNode.invoke failed")
        error_msgs = []
        for tc in patched_calls:
            error_msgs.append(ToolMessage(
                content=json.dumps({"success": False, "error": f"Tool execution failed: {e}"}),
                tool_call_id=tc["id"],
            ))
        result = {"messages": error_msgs}

    # ── DEBUG: full tool results ─────────────────────────────────────────
    if logger.isEnabledFor(logging.DEBUG):
        for msg in result.get("messages", []):
            if isinstance(msg, ToolMessage):
                logger.debug("tool_node RESULT | tool_call_id=%s | %s",
                             getattr(msg, "tool_call_id", "?"), msg.content)

    if tool_results:
        result["messages"] = tool_results + result.get("messages", [])
    result["rejected_tool_calls"] = None
    return result


# ── Loop Breaker Node ────────────────────────────────────────────────────

def loop_breaker_node(state: ShoppingState) -> dict:
    """Terminate agent loop when consecutive tool failures exceed the limit."""
    logger.warning("loop_breaker | session=%s | breaking after %d consecutive tool failures",
                   state.get("session_id", "?"), state.get("tool_loops_this_turn", 0))
    return {
        "messages": [AIMessage(
            content="I'm sorry, I ran into repeated issues trying to complete that action. "
                    "Please try again or contact support if this continues."
        )],
        "tool_loops_this_turn": 0,
    }


# ── Routing ──────────────────────────────────────────────────────────────────

def route_after_agent(state: ShoppingState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        tool_names = [tc["name"] for tc in last.tool_calls]
        for tc in last.tool_calls:
            if tc["name"] in ("place_order", "acp_checkout"):
                logger.debug("route_after_agent → human_approval | tools=%s", tool_names)
                return "human_approval"
        logger.debug("route_after_agent → tools | tools=%s", tool_names)
        return "tools"
    logger.debug("route_after_agent → sync (no tool calls)")
    return "sync"


def route_after_sync(state: ShoppingState) -> str:
    last = state["messages"][-1]
    if not isinstance(last, ToolMessage):
        logger.debug("route_after_sync → END (last msg is %s)", type(last).__name__)
        return END

    # Guard against infinite tool-retry loops (only counts consecutive failures)
    consecutive_failures = state.get("tool_loops_this_turn", 0)
    max_failures = CONFIG.resilience.max_tool_loops_per_turn
    if consecutive_failures >= max_failures:
        logger.warning("route_after_sync → loop_breaker | failures=%d/%d",
                       consecutive_failures, max_failures)
        return "loop_breaker"

    logger.debug("route_after_sync → agent | failures=%d/%d", consecutive_failures, max_failures)
    return "agent"


# ── Graph Assembly ───────────────────────────────────────────────────────────

def build_graph():
    g = StateGraph(ShoppingState)

    g.add_node("agent", agent_node)
    g.add_node("human_approval", human_approval_node)
    g.add_node("tools", tool_node_with_injection)
    g.add_node("sync", state_sync_node)
    g.add_node("loop_breaker", loop_breaker_node)

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {
        "human_approval": "human_approval",
        "tools": "tools",
        "sync": "sync",
    })
    g.add_edge("human_approval", "tools")
    g.add_edge("tools", "sync")
    g.add_conditional_edges("sync", route_after_sync, {
        "agent": "agent",
        "loop_breaker": "loop_breaker",
        END: END,
    })
    g.add_edge("loop_breaker", END)

    checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer, interrupt_before=[])


production_graph = build_graph()
