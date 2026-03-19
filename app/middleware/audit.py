"""Audit logging — tamper-evident records for all critical actions."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("sap_agent.audit")

_audit_log: list[dict] = []

# ── PII Scrubbing ────────────────────────────────────────────────────────────

_CARD_RE = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")
_CVV_RE = re.compile(r"\bcvn?\s*[:\=]\s*\d{3,4}\b", re.I)
_TOKEN_RE = re.compile(r'"access_token"\s*:\s*"[^"]+"')
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def scrub_pii(text: str) -> str:
    text = _CARD_RE.sub("[CARD-REDACTED]", text)
    text = _CVV_RE.sub("cvn:[REDACTED]", text)
    text = _TOKEN_RE.sub('"access_token":"[REDACTED]"', text)
    text = _EMAIL_RE.sub("[EMAIL-REDACTED]", text)
    return text


# ── Audit ────────────────────────────────────────────────────────────────────

def audit(event: str, user_id: str, details: dict) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "user_id": hashlib.sha256(user_id.encode()).hexdigest()[:12],
        "details": scrub_pii(json.dumps(details, default=str)),
    }
    _audit_log.append(record)
    logger.info("AUDIT | %s", json.dumps(record))


def get_audit_log() -> list[dict]:
    return list(_audit_log)
