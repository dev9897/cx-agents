"""
Redis client — session persistence, cart storage, order history.

Falls back gracefully to in-memory storage if Redis is unavailable.
"""

import json
import logging
from typing import Any, Optional

from app.config import CONFIG

logger = logging.getLogger("sap_agent.redis")

_redis = None
_fallback_store: dict[str, str] = {}
_using_fallback = False


def _get_redis():
    global _redis, _using_fallback
    if _redis is not None:
        return _redis
    if _using_fallback:
        return None
    try:
        import redis
        _redis = redis.Redis(
            host=CONFIG.redis.host,
            port=CONFIG.redis.port,
            password=CONFIG.redis.password or None,
            db=CONFIG.redis.db,
            decode_responses=True,
            socket_connect_timeout=3,
        )
        _redis.ping()
        logger.info("Redis connected: %s:%d", CONFIG.redis.host, CONFIG.redis.port)
        return _redis
    except Exception as e:
        logger.warning("Redis unavailable (%s) — using in-memory fallback", e)
        _using_fallback = True
        return None


def set_json(key: str, value: Any, ttl: int = 3600) -> bool:
    """Store a JSON-serializable value."""
    data = json.dumps(value, default=str)
    r = _get_redis()
    if r:
        try:
            r.set(key, data, ex=ttl)
            return True
        except Exception as e:
            logger.error("Redis SET failed for key=%s: %s", key, e)
    # Fallback
    _fallback_store[key] = data
    return True


def get_json(key: str) -> Optional[Any]:
    """Retrieve a JSON value."""
    r = _get_redis()
    if r:
        try:
            data = r.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error("Redis GET failed for key=%s: %s", key, e)
    # Fallback
    data = _fallback_store.get(key)
    return json.loads(data) if data else None


def delete(key: str) -> bool:
    """Delete a key."""
    r = _get_redis()
    if r:
        try:
            r.delete(key)
            return True
        except Exception as e:
            logger.error("Redis DELETE failed for key=%s: %s", key, e)
    _fallback_store.pop(key, None)
    return True


def keys_by_pattern(pattern: str) -> list[str]:
    """Find keys matching a pattern."""
    r = _get_redis()
    if r:
        try:
            return [k for k in r.scan_iter(match=pattern, count=100)]
        except Exception as e:
            logger.error("Redis SCAN failed for pattern=%s: %s", pattern, e)
    return [k for k in _fallback_store if k.startswith(pattern.replace("*", ""))]


def is_available() -> bool:
    """Check if Redis is connected."""
    r = _get_redis()
    if r:
        try:
            r.ping()
            return True
        except Exception:
            return False
    return False
