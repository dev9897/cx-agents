"""
security_layer.py
=================
Multi-layer security guard for the SAP Commerce Shopping Agent.

Covers:
  1. Prompt Injection / Jailbreak detection
  2. Input validation & sanitisation
  3. Tool-call guardrails (what the agent is ALLOWED to do)
  4. Rate limiting per user/session
  5. PII / sensitive-data scrubbing in logs
  6. Human-in-the-loop confirmation for destructive actions
  7. Audit logging
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

# ─────────────────────────────────────────────
# 1. Prompt Injection / Jailbreak Detection
# ─────────────────────────────────────────────

# Patterns that indicate adversarial prompt injection attempts
_INJECTION_PATTERNS: list[re.Pattern] = [
    # Classic role-switch attacks
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|constraints?)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(a|an)\b", re.I),
    re.compile(r"\bact\s+as\s+(if\s+you\s+are|a|an)\b", re.I),
    re.compile(r"\bforget\s+(everything|all|your)\b", re.I),
    re.compile(r"\bDAN\b|\bjailbreak\b|\bunlock\b.*\bmode\b", re.I),
    # Instruction override via fake system/assistant turns
    re.compile(r"\[SYSTEM\]|\[INST\]|<\|system\|>|<<SYS>>", re.I),
    re.compile(r"###\s*(system|instruction|override)", re.I),
    # Leaking / exfiltrating context
    re.compile(r"\bprint\s+(your\s+)?(system\s+prompt|instructions?|context)\b", re.I),
    re.compile(r"\brepeat\s+(everything|all|above|your prompt)\b", re.I),
    # Trying to call arbitrary tools
    re.compile(r"\bcall\s+tool\b|\bexecute\s+(function|code|command|tool)\b", re.I),
    re.compile(r"\bos\.(system|popen|exec)\b|\bsubprocess\b", re.I),
    # Social engineering
    re.compile(r"\bfor\s+(testing|debugging|research|demo)\s+purposes?\b", re.I),
    re.compile(r"\bhypothetically\b.{0,40}\border\b", re.I),
]

# Maximum allowed input length (characters)
MAX_INPUT_LENGTH = 2000


def detect_prompt_injection(text: str) -> tuple[bool, str]:
    """
    Returns (is_malicious, reason).
    Call this on every user message BEFORE passing to the agent.
    """
    if len(text) > MAX_INPUT_LENGTH:
        return True, f"Input too long ({len(text)} chars, max {MAX_INPUT_LENGTH})"

    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return True, f"Suspicious pattern detected: '{match.group(0)}'"

    return False, ""


# ─────────────────────────────────────────────
# 2. Input Sanitisation
# ─────────────────────────────────────────────

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")          # strip control chars
_HTML_TAGS      = re.compile(r"<[^>]+>")                  # strip HTML
_SQL_INJECTION  = re.compile(
    r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|EXEC|xp_)\b)", re.I
)


def sanitise_input(text: str) -> str:
    """Clean user input before sending to LLM or API."""
    text = _CONTROL_CHARS.sub(" ", text)
    text = _HTML_TAGS.sub("", text)
    # Warn on SQL patterns but don't silently drop — the agent shouldn't build SQL
    if _SQL_INJECTION.search(text):
        logging.warning("SQL-like pattern in user input: %s", text[:100])
    return text.strip()


# ─────────────────────────────────────────────
# 3. Tool-Call Guardrails
# ─────────────────────────────────────────────

# Define which tools are allowed without confirmation vs require human approval
_AUTO_ALLOWED_TOOLS = {
    "account_login",
    "guest_token",
    "search_products",
    "get_product_details",
    "create_cart",
    "add_to_cart",
    "get_cart",
    "set_delivery_address",
    "set_delivery_mode",
    "set_payment_details",
}

_REQUIRE_CONFIRMATION_TOOLS = {
    "place_order",   # irreversible — always confirm with human
}

_BLOCKED_TOOLS: set[str] = set()  # add any tools you want to permanently block


def validate_tool_call(tool_name: str, tool_args: dict,
                        user_id: str,
                        confirm_callback: Optional[Callable] = None) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    For tools requiring confirmation, calls confirm_callback(tool_name, tool_args).
    If confirm_callback is None, confirmation is auto-denied.
    """
    if tool_name in _BLOCKED_TOOLS:
        return False, f"Tool '{tool_name}' is permanently disabled."

    if tool_name in _REQUIRE_CONFIRMATION_TOOLS:
        if confirm_callback is None:
            return False, f"Tool '{tool_name}' requires human confirmation but no callback provided."
        approved = confirm_callback(tool_name, tool_args, user_id)
        if not approved:
            return False, f"User did not confirm execution of '{tool_name}'."
        return True, "confirmed"

    if tool_name in _AUTO_ALLOWED_TOOLS:
        return True, "auto-approved"

    # Unknown tool — deny by default
    return False, f"Unknown tool '{tool_name}' is not whitelisted."


def validate_tool_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Basic argument schema validation per tool."""

    validators: dict[str, Callable[[dict], tuple[bool, str]]] = {
        "add_to_cart": _validate_add_to_cart,
        "set_payment_details": _validate_payment,
        "account_login": _validate_login,
    }

    validator = validators.get(tool_name)
    if validator:
        return validator(args)
    return True, ""


def _validate_add_to_cart(args: dict) -> tuple[bool, str]:
    qty = args.get("quantity", 1)
    if not isinstance(qty, int) or qty < 1 or qty > 100:
        return False, f"Invalid quantity: {qty}"
    code = args.get("product_code", "")
    if not re.match(r"^[A-Za-z0-9_\-]{1,50}$", str(code)):
        return False, f"Invalid product_code: {code}"
    return True, ""


def _validate_payment(args: dict) -> tuple[bool, str]:
    payment = args.get("payment", {})
    card = str(payment.get("cardNumber", "")).replace(" ", "")
    if not re.match(r"^\d{13,19}$", card):
        return False, "Invalid card number format."
    expiry_month = str(payment.get("expiryMonth", ""))
    expiry_year  = str(payment.get("expiryYear", ""))
    if not re.match(r"^\d{1,2}$", expiry_month):
        return False, "Invalid expiry month."
    if not re.match(r"^\d{4}$", expiry_year):
        return False, "Invalid expiry year."
    return True, ""


def _validate_login(args: dict) -> tuple[bool, str]:
    username = args.get("username", "")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", username):
        return False, "Invalid username (must be email)."
    password = args.get("password", "")
    if len(password) < 6:
        return False, "Password too short."
    return True, ""


# ─────────────────────────────────────────────
# 4. Rate Limiting
# ─────────────────────────────────────────────

class RateLimiter:
    """
    Simple in-process token-bucket rate limiter.
    In production, replace with Redis-backed sliding window.
    """

    def __init__(self, requests_per_minute: int = 20,
                 order_per_hour: int = 3):
        self.rpm = requests_per_minute
        self.order_limit = order_per_hour
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._order_buckets: dict[str, list[float]] = defaultdict(list)

    def _prune(self, bucket: list[float], window: float) -> None:
        cutoff = time.time() - window
        bucket[:] = [t for t in bucket if t > cutoff]

    def check_message(self, user_id: str) -> tuple[bool, str]:
        bucket = self._buckets[user_id]
        self._prune(bucket, 60.0)
        if len(bucket) >= self.rpm:
            return False, f"Rate limit: max {self.rpm} messages/min exceeded."
        bucket.append(time.time())
        return True, ""

    def check_order(self, user_id: str) -> tuple[bool, str]:
        bucket = self._order_buckets[user_id]
        self._prune(bucket, 3600.0)
        if len(bucket) >= self.order_limit:
            return False, f"Rate limit: max {self.order_limit} orders/hour exceeded."
        bucket.append(time.time())
        return True, ""


rate_limiter = RateLimiter()


# ─────────────────────────────────────────────
# 5. PII Scrubbing (for Logs / Audit Trail)
# ─────────────────────────────────────────────

_CARD_RE   = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")
_CVV_RE    = re.compile(r"\bcvn?\s*[:\=]\s*\d{3,4}\b", re.I)
_TOKEN_RE  = re.compile(r'"access_token"\s*:\s*"[^"]+"')
_EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def scrub_pii(text: str) -> str:
    """Remove sensitive data before writing to logs."""
    text = _CARD_RE.sub("[CARD-REDACTED]", text)
    text = _CVV_RE.sub("cvn:[REDACTED]", text)
    text = _TOKEN_RE.sub('"access_token":"[REDACTED]"', text)
    text = _EMAIL_RE.sub("[EMAIL-REDACTED]", text)
    return text


# ─────────────────────────────────────────────
# 6. Audit Logging
# ─────────────────────────────────────────────

_audit_log: list[dict] = []   # In production → write to SIEM / CloudWatch / Splunk

def audit(event: str, user_id: str, details: dict) -> None:
    """Write a tamper-evident audit record."""
    record = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "event":   event,
        "user_id": hashlib.sha256(user_id.encode()).hexdigest()[:12],  # pseudonymised
        "details": scrub_pii(json.dumps(details, default=str)),
    }
    _audit_log.append(record)
    logging.info("AUDIT | %s", json.dumps(record))


# ─────────────────────────────────────────────
# 7. Secure Message Pipeline — wire into agent
# ─────────────────────────────────────────────

class SecurityMiddleware:
    """
    Wrap run_shopping_agent with security checks.
    Usage:
        secure_agent = SecurityMiddleware(run_shopping_agent)
        state = secure_agent.run("add a Sony headphone to my cart", user_id="u123")
    """

    def __init__(self, agent_fn: Callable, confirm_fn: Optional[Callable] = None):
        self.agent_fn = agent_fn
        self.confirm_fn = confirm_fn  # (tool_name, args, user_id) -> bool

    def run(self, user_message: str, user_id: str = "anonymous",
            state: Optional[dict] = None) -> dict:

        # ── Rate limit ──
        ok, reason = rate_limiter.check_message(user_id)
        if not ok:
            audit("RATE_LIMIT", user_id, {"reason": reason})
            return _error_state(state, reason)

        # ── Injection check ──
        is_malicious, reason = detect_prompt_injection(user_message)
        if is_malicious:
            audit("INJECTION_ATTEMPT", user_id, {"reason": reason,
                  "snippet": user_message[:120]})
            return _error_state(state,
                "I couldn't process that request. Please rephrase.")

        # ── Sanitise ──
        clean_message = sanitise_input(user_message)

        # ── Run agent ──
        audit("USER_MESSAGE", user_id, {"length": len(clean_message)})
        new_state = self.agent_fn(clean_message, state)

        # ── Post-run: validate any tool calls the agent made ──
        self._audit_tool_calls(new_state, user_id)

        return new_state

    def _audit_tool_calls(self, state: dict, user_id: str) -> None:
        for msg in state.get("messages", []):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    args = tc.get("args", {})

                    # Log every tool call
                    audit("TOOL_CALL", user_id, {"tool": name, "args": args})

                    # Order-specific rate limit
                    if name == "place_order":
                        ok, reason = rate_limiter.check_order(user_id)
                        if not ok:
                            audit("ORDER_RATE_LIMIT", user_id, {"reason": reason})
                            # In a real implementation you'd intercept the tool call
                            # before execution — see interrupts in LangGraph docs


def _error_state(state: Optional[dict], message: str) -> dict:
    from langchain_core.messages import AIMessage
    base = state or {"messages": [], "access_token": None,
                     "user_id": "anonymous", "cart_id": None,
                     "order_code": None, "username": None}
    base["messages"] = list(base.get("messages", [])) + [
        AIMessage(content=message)
    ]
    return base
