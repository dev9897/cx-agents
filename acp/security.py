"""
ACP security layer — API key auth, HMAC signature verification, idempotency.
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("sap_agent.acp.security")

# ── Configuration ────────────────────────────────────────────────────────────

ACP_API_KEY = os.getenv("ACP_API_KEY", "")
ACP_HMAC_SECRET = os.getenv("ACP_HMAC_SECRET", "")
ACP_API_VERSION = "2026-01-30"

# In-memory idempotency store (use Redis in production)
_idempotency_store: dict[str, dict] = {}
_IDEMPOTENCY_TTL = 3600  # 1 hour


# ── API Key Verification ────────────────────────────────────────────────────

def verify_api_key(request: Request) -> None:
    """Validate the Authorization: Bearer <key> header."""
    if not ACP_API_KEY:
        # No key configured — skip auth (development mode)
        logger.debug("ACP auth skipped — no ACP_API_KEY configured")
        return

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[len("Bearer "):]
    if not hmac.compare_digest(token, ACP_API_KEY):
        logger.warning("ACP auth failed — invalid API key")
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── HMAC Signature Verification ──────────────────────────────────────────────

def verify_signature(request: Request, body: bytes) -> None:
    """
    Verify the HMAC-SHA256 Signature header.
    Signature = Base64(HMAC-SHA256(secret, timestamp + "." + body))
    """
    if not ACP_HMAC_SECRET:
        return  # Skip in development

    signature = request.headers.get("Signature")
    timestamp = request.headers.get("Timestamp")

    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail="Missing Signature or Timestamp header")

    # Prevent replay attacks — reject if older than 5 minutes
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if abs(age) > 300:
            raise HTTPException(status_code=401, detail="Request timestamp too old")
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid Timestamp format")

    import base64
    payload = f"{timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(ACP_HMAC_SECRET.encode(), payload, hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(signature, expected):
        logger.warning("ACP HMAC verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")


# ── Idempotency ──────────────────────────────────────────────────────────────

def check_idempotency(idempotency_key: Optional[str]) -> Optional[dict]:
    """
    If the Idempotency-Key was seen before, return the cached response.
    Returns None if this is a new request.
    """
    if not idempotency_key:
        return None

    _cleanup_expired()

    entry = _idempotency_store.get(idempotency_key)
    if entry:
        logger.info("ACP idempotency hit | key=%s", idempotency_key)
        return entry.get("response")
    return None


def store_idempotency(idempotency_key: Optional[str], response: dict) -> None:
    """Cache a response for an idempotency key."""
    if not idempotency_key:
        return
    _idempotency_store[idempotency_key] = {
        "response": response,
        "created_at": time.time(),
    }


def _cleanup_expired() -> None:
    """Remove expired idempotency entries."""
    now = time.time()
    expired = [k for k, v in _idempotency_store.items()
               if now - v["created_at"] > _IDEMPOTENCY_TTL]
    for k in expired:
        del _idempotency_store[k]
