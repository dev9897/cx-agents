"""
Security layer — prompt injection detection, input sanitisation,
tool-call guardrails, rate limiting.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Callable, Optional

logger = logging.getLogger("sap_agent.security")

# ── Prompt Injection Detection ───────────────────────────────────────────────

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|constraints?)\b", re.I),
    re.compile(r"\byou\s+are\s+now\s+(a|an)\b", re.I),
    re.compile(r"\bact\s+as\s+(if\s+you\s+are|a|an)\b", re.I),
    re.compile(r"\bforget\s+(everything|all|your)\b", re.I),
    re.compile(r"\bDAN\b|\bjailbreak\b|\bunlock\b.*\bmode\b", re.I),
    re.compile(r"\[SYSTEM\]|\[INST\]|<\|system\|>|<<SYS>>", re.I),
    re.compile(r"###\s*(system|instruction|override)", re.I),
    re.compile(r"\bprint\s+(your\s+)?(system\s+prompt|instructions?|context)\b", re.I),
    re.compile(r"\brepeat\s+(everything|all|above|your prompt)\b", re.I),
    re.compile(r"\bcall\s+tool\b|\bexecute\s+(function|code|command|tool)\b", re.I),
    re.compile(r"\bos\.(system|popen|exec)\b|\bsubprocess\b", re.I),
    re.compile(r"\bfor\s+(testing|debugging|research|demo)\s+purposes?\b", re.I),
    re.compile(r"\bhypothetically\b.{0,40}\border\b", re.I),
]

MAX_INPUT_LENGTH = 2000


def detect_prompt_injection(text: str) -> tuple[bool, str]:
    if len(text) > MAX_INPUT_LENGTH:
        return True, f"Input too long ({len(text)} chars, max {MAX_INPUT_LENGTH})"
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return True, f"Suspicious pattern detected: '{match.group(0)}'"
    return False, ""


# ── Input Sanitisation ───────────────────────────────────────────────────────

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HTML_TAGS = re.compile(r"<[^>]+>")
_SQL_INJECTION = re.compile(r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|EXEC|xp_)\b)", re.I)


def sanitise_input(text: str) -> str:
    text = _CONTROL_CHARS.sub(" ", text)
    text = _HTML_TAGS.sub("", text)
    if _SQL_INJECTION.search(text):
        logger.warning("SQL-like pattern in user input: %s", text[:100])
    return text.strip()


# ── Tool-Call Guardrails ─────────────────────────────────────────────────────

_AUTO_ALLOWED_TOOLS = {
    "account_login", "guest_token", "search_products", "get_product_details",
    "create_cart", "add_to_cart", "get_cart", "update_cart_entry",
    "set_delivery_address", "set_delivery_mode", "set_payment_details",
    "semantic_search_products", "initiate_checkout",
    "get_order", "get_order_history", "get_saved_addresses",
    "list_saved_cards", "get_personalized_recommendations",
}

_REQUIRE_CONFIRMATION_TOOLS = {"place_order"}
_BLOCKED_TOOLS: set[str] = set()


def validate_tool_call(tool_name: str, tool_args: dict, user_id: str,
                       confirm_callback: Optional[Callable] = None) -> tuple[bool, str]:
    if tool_name in _BLOCKED_TOOLS:
        return False, f"Tool '{tool_name}' is permanently disabled."
    if tool_name in _REQUIRE_CONFIRMATION_TOOLS:
        if confirm_callback is None:
            return False, f"Tool '{tool_name}' requires human confirmation."
        approved = confirm_callback(tool_name, tool_args, user_id)
        return (True, "confirmed") if approved else (False, f"User did not confirm '{tool_name}'.")
    if tool_name in _AUTO_ALLOWED_TOOLS:
        return True, "auto-approved"
    return False, f"Unknown tool '{tool_name}' is not whitelisted."


# ── Rate Limiting ────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, requests_per_minute: int = 20, order_per_hour: int = 3):
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
