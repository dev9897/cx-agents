"""
Redis-backed tool result cache for SAP API calls.

Caches read-only tool results (search, product details, delivery modes, cart)
to reduce token costs and SAP API load.  Falls back gracefully via the
existing redis_client in-memory store when Redis is unavailable.
"""

import hashlib
import json
import logging
from typing import Any, Optional

from app.integrations import redis_client

logger = logging.getLogger("sap_agent.tool_cache")

# ── TTL per tool (seconds) ──────────────────────────────────────────────────

TOOL_CACHE_TTL: dict[str, int] = {
    "search_products": 300,           # 5 min  — catalog changes rarely
    "get_product_details": 1800,      # 30 min — product data is stable
    "get_delivery_modes": 3600,       # 1 hr   — almost never changes
    "get_cart": 30,                   # 30 s   — stale cart is confusing
    "get_order_history": 60,          # 1 min  — recent orders may appear
    "get_saved_addresses": 300,       # 5 min
}

# Tools that mutate state — never cache, but invalidate related entries
CART_MUTATING_TOOLS = frozenset({
    "add_to_cart", "update_cart_entry", "delete_cart_entry",
    "set_delivery_address", "set_delivery_mode", "set_payment_details",
})

# ── Helpers ─────────────────────────────────────────────────────────────────


def _cache_key(tool_name: str, kwargs: dict) -> str:
    """Deterministic cache key, excluding ephemeral args."""
    filtered = {
        k: v for k, v in sorted(kwargs.items())
        if k not in ("access_token", "session_id")
    }
    payload = json.dumps(filtered, sort_keys=True)
    h = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"toolcache:{tool_name}:{h}"


# ── Public API ──────────────────────────────────────────────────────────────


def get(tool_name: str, kwargs: dict) -> Optional[Any]:
    """Return cached result or None on miss / non-cacheable tool."""
    if tool_name not in TOOL_CACHE_TTL:
        return None
    key = _cache_key(tool_name, kwargs)
    result = redis_client.get_json(key)
    if result is not None:
        logger.info("tool_cache HIT  | tool=%s key=%s", tool_name, key)
        return result
    logger.debug("tool_cache MISS | tool=%s key=%s", tool_name, key)
    return None


def put(tool_name: str, kwargs: dict, result: Any) -> None:
    """Cache a successful tool result."""
    if tool_name not in TOOL_CACHE_TTL:
        return
    # Only cache successful responses
    if isinstance(result, dict) and result.get("success") is False:
        return
    key = _cache_key(tool_name, kwargs)
    ttl = TOOL_CACHE_TTL[tool_name]
    redis_client.set_json(key, result, ttl=ttl)
    logger.info("tool_cache SET  | tool=%s key=%s ttl=%ds", tool_name, key, ttl)


def invalidate_cart() -> None:
    """Drop all cached get_cart entries after a cart mutation."""
    for key in redis_client.keys_by_pattern("toolcache:get_cart:*"):
        redis_client.delete(key)
    logger.debug("tool_cache | invalidated get_cart entries")


def on_tool_call(tool_name: str) -> None:
    """Call after a mutating tool succeeds to keep caches consistent."""
    if tool_name in CART_MUTATING_TOOLS:
        invalidate_cart()
