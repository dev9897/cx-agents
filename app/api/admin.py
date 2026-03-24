"""
Admin API — protected endpoints for agent management, debugging, and monitoring.

All routes require the X-Admin-Key header matching ADMIN_API_KEY env var.
"""

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request

from app.config import CONFIG

logger = logging.getLogger("sap_agent.admin")

router = APIRouter(prefix="/admin", tags=["Admin"])

# ── Auth dependency ──────────────────────────────────────────────────────────

_ADMIN_KEY = os.getenv("ADMIN_API_KEY", "admin")


def _require_admin(x_admin_key: str = Header(default="")):
    if not x_admin_key or x_admin_key != _ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


# ── Session store reference (injected from main.py) ─────────────────────────

_sessions: Optional[dict] = None


def set_session_store(sessions: dict):
    global _sessions
    _sessions = sessions


# ── Startup time ─────────────────────────────────────────────────────────────

_BOOT_TIME = time.time()


# ── GET /admin/overview — dashboard summary ──────────────────────────────────

@router.get("/overview", dependencies=[Depends(_require_admin)])
def admin_overview():
    """High-level dashboard stats."""
    from app.integrations import redis_client
    from app.integrations import tool_cache
    from app.middleware.audit import get_audit_log
    from app.middleware.error_handler import sap_circuit_breaker

    sessions = _sessions or {}
    audit_log = get_audit_log()

    total_input = sum(s.get("total_input_tokens", 0) for s in sessions.values())
    total_output = sum(s.get("total_output_tokens", 0) for s in sessions.values())
    total_turns = sum(s.get("turn_count", 0) for s in sessions.values())
    error_count = sum(1 for e in audit_log if e.get("event") == "API_ERROR")

    return {
        "sessions": {
            "active": len(sessions),
            "total_turns": total_turns,
        },
        "tokens": {
            "total_input": total_input,
            "total_output": total_output,
            "estimated_cost_usd": round((total_input * 0.25 + total_output * 1.25) / 1_000_000, 4),
        },
        "errors": {
            "total": error_count,
            "circuit_breaker": "open" if sap_circuit_breaker.is_open else "closed",
        },
        "infrastructure": {
            "redis": "connected" if redis_client.is_available() else "fallback",
            "uptime_seconds": round(time.time() - _BOOT_TIME),
        },
    }


# ── GET /admin/sessions — list sessions ──────────────────────────────────────

@router.get("/sessions", dependencies=[Depends(_require_admin)])
def list_sessions():
    """List all active sessions with summary stats."""
    sessions = _sessions or {}
    result = []
    for sid, state in sessions.items():
        result.append({
            "session_id": sid,
            "user_id": state.get("user_id", "?"),
            "username": state.get("username"),
            "turn_count": state.get("turn_count", 0),
            "total_input_tokens": state.get("total_input_tokens", 0),
            "total_output_tokens": state.get("total_output_tokens", 0),
            "cart_id": state.get("cart_id"),
            "order_code": state.get("order_code"),
            "last_error": state.get("last_error"),
            "message_count": len(state.get("messages", [])),
        })
    return {"sessions": result}


# ── GET /admin/sessions/{id} — full session state ────────────────────────────

@router.get("/sessions/{session_id}", dependencies=[Depends(_require_admin)])
def get_session(session_id: str):
    """Full session state dump (messages serialized, token redacted)."""
    sessions = _sessions or {}
    state = sessions.get(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    dump = {}
    for k, v in state.items():
        if k == "access_token":
            dump[k] = f"***({len(v)}chars)" if v else None
        elif k == "messages":
            msgs = []
            for msg in (v or []):
                msgs.append({
                    "type": type(msg).__name__,
                    "content": str(msg.content)[:2000] if hasattr(msg, "content") else "",
                    "tool_calls": [
                        {"name": tc.get("name"), "id": tc.get("id")}
                        for tc in getattr(msg, "tool_calls", [])
                    ] if getattr(msg, "tool_calls", None) else None,
                })
            dump[k] = msgs
        else:
            dump[k] = v
    return dump


# ── GET /admin/graph — graph structure ────────────────────────────────────────

@router.get("/graph", dependencies=[Depends(_require_admin)])
def get_graph():
    """Return the LangGraph node/edge structure for visualization."""
    from app.agent.graph import production_graph

    graph = production_graph.get_graph()
    nodes = []
    for node in graph.nodes:
        nodes.append({
            "id": node,
            "type": "start" if node == "__start__" else "end" if node == "__end__" else "node",
        })

    edges = []
    for edge in graph.edges:
        edges.append({
            "source": edge.source,
            "target": edge.target,
            "conditional": edge.conditional,
        })

    return {"nodes": nodes, "edges": edges}


# ── GET /admin/tools — registered tools ──────────────────────────────────────

@router.get("/tools", dependencies=[Depends(_require_admin)])
def list_tools():
    """List all agent tools with their schemas."""
    from app.agent.graph import ALL_TOOLS

    tools = []
    for t in ALL_TOOLS:
        schema = {}
        try:
            schema = t.args_schema.schema() if hasattr(t, "args_schema") and t.args_schema else {}
        except Exception:
            pass
        tools.append({
            "name": t.name,
            "description": getattr(t, "description", "")[:200],
            "schema": schema,
        })
    return {"tools": tools, "count": len(tools)}


# ── GET /admin/features — feature registry ───────────────────────────────────

@router.get("/features", dependencies=[Depends(_require_admin)])
def list_features():
    """Feature registry status."""
    from app.features.registry import FeatureRegistry
    registry = FeatureRegistry.instance()
    return {
        "active": registry.active_features,
        "config": registry.get_ui_config(),
    }


# ── GET /admin/audit — audit log ─────────────────────────────────────────────

@router.get("/audit", dependencies=[Depends(_require_admin)])
def get_audit(
    event: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Paginated audit log."""
    from app.middleware.audit import get_audit_log

    log = get_audit_log()
    if event:
        log = [e for e in log if e.get("event") == event]

    total = len(log)
    page = list(reversed(log))[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "entries": page}


# ── GET /admin/config — current config ───────────────────────────────────────

@router.get("/config", dependencies=[Depends(_require_admin)])
def get_config():
    """Current config dump with secrets redacted."""
    def _safe_dict(obj):
        d = asdict(obj) if hasattr(obj, "__dataclass_fields__") else vars(obj)
        safe = {}
        for k, v in d.items():
            if any(secret in k.lower() for secret in ("key", "secret", "password", "token")):
                safe[k] = "***" if v else ""
            elif hasattr(v, "__dataclass_fields__"):
                safe[k] = _safe_dict(v)
            else:
                safe[k] = v
        return safe

    return _safe_dict(CONFIG)


# ── POST /admin/config — update config ───────────────────────────────────────

@router.post("/config", dependencies=[Depends(_require_admin)])
async def update_config(request: Request):
    """Hot-reload config values (log_level, feature flags, etc.)."""
    body = await request.json()
    updated = []

    if "log_level" in body:
        level = body["log_level"].upper()
        if hasattr(logging, level):
            logging.getLogger().setLevel(getattr(logging, level))
            CONFIG.observability.log_level = level
            updated.append(f"log_level={level}")

    if "max_tool_loops_per_turn" in body:
        val = int(body["max_tool_loops_per_turn"])
        CONFIG.resilience.max_tool_loops_per_turn = val
        updated.append(f"max_tool_loops_per_turn={val}")

    if "max_messages_in_context" in body:
        val = int(body["max_messages_in_context"])
        CONFIG.resilience.max_messages_in_context = val
        updated.append(f"max_messages_in_context={val}")

    if "temperature" in body:
        val = float(body["temperature"])
        CONFIG.claude.temperature = val
        updated.append(f"temperature={val}")

    return {"updated": updated}


# ── POST /admin/cache/clear — clear tool cache ──────────────────────────────

@router.post("/cache/clear", dependencies=[Depends(_require_admin)])
def clear_cache():
    """Clear all Redis tool cache entries."""
    from app.integrations import redis_client

    keys = redis_client.keys_by_pattern("toolcache:*")
    for key in keys:
        redis_client.delete(key)
    return {"cleared": len(keys)}


# ── GET /admin/metrics — aggregated metrics ──────────────────────────────────

@router.get("/metrics", dependencies=[Depends(_require_admin)])
def get_metrics():
    """Aggregated agent metrics."""
    from app.middleware.audit import get_audit_log
    from app.middleware.error_handler import sap_circuit_breaker

    sessions = _sessions or {}
    audit_log = get_audit_log()

    # Per-event type counts
    event_counts: dict[str, int] = {}
    for entry in audit_log:
        ev = entry.get("event", "unknown")
        event_counts[ev] = event_counts.get(ev, 0) + 1

    # Session stats
    turns = [s.get("turn_count", 0) for s in sessions.values()]
    input_tokens = [s.get("total_input_tokens", 0) for s in sessions.values()]
    output_tokens = [s.get("total_output_tokens", 0) for s in sessions.values()]

    return {
        "sessions": {
            "active": len(sessions),
            "avg_turns": round(sum(turns) / max(len(turns), 1), 1),
            "max_turns": max(turns) if turns else 0,
        },
        "tokens": {
            "total_input": sum(input_tokens),
            "total_output": sum(output_tokens),
            "avg_input_per_session": round(sum(input_tokens) / max(len(input_tokens), 1)),
            "avg_output_per_session": round(sum(output_tokens) / max(len(output_tokens), 1)),
        },
        "audit_events": event_counts,
        "circuit_breaker": {
            "state": "open" if sap_circuit_breaker.is_open else "closed",
            "failures": sap_circuit_breaker._failure_count,
        },
        "uptime_seconds": round(time.time() - _BOOT_TIME),
    }


# ── GET /admin/logs/stream — SSE log stream ─────────────────────────────────

@router.get("/logs/stream")
async def stream_logs(
    request: Request,
    level: str = Query("INFO", description="Minimum log level"),
    key: str = Query("", description="Admin key (query param for SSE)"),
):
    """Server-Sent Events stream of live log entries."""
    import asyncio
    from fastapi.responses import StreamingResponse

    # SSE doesn't support custom headers — accept key from query param or header
    admin_key = key or request.headers.get("x-admin-key", "")
    if not admin_key or admin_key != _ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")

    min_level = getattr(logging, level.upper(), logging.INFO)

    class _QueueHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.queue: list[str] = []

        def emit(self, record):
            if record.levelno >= min_level:
                self.queue.append(self.format(record))

    handler = _QueueHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
    ))
    logging.getLogger().addHandler(handler)

    async def _generate():
        try:
            while True:
                while handler.queue:
                    line = handler.queue.pop(0)
                    yield f"data: {json.dumps({'log': line})}\n\n"
                await asyncio.sleep(0.5)
        finally:
            logging.getLogger().removeHandler(handler)

    return StreamingResponse(_generate(), media_type="text/event-stream")
