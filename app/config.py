"""
Centralised configuration — 12-factor-app style.
All tunable knobs in one place.
"""

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

load_dotenv()


@dataclass
class ClaudeConfig:
    model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
    max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
    max_input_tokens: int = int(os.getenv("CLAUDE_MAX_INPUT_TOKENS", "4000"))
    enable_prompt_caching: bool = True
    streaming: bool = True
    max_retries: int = 3
    timeout_seconds: float = 60.0
    temperature: float = 0.0


@dataclass
class GeminiConfig:
    model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    api_key: str = os.getenv("GOOGLE_API_KEY", "")
    max_tokens: int = int(os.getenv("GEMINI_MAX_TOKENS", "1024"))
    temperature: float = 0.0


@dataclass
class ObservabilityConfig:
    tracing_backend: Literal["langsmith", "none"] = os.getenv("TRACING_BACKEND", "langsmith")
    langsmith_project: str = os.getenv("LANGSMITH_PROJECT", "sap-commerce-agent")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: Literal["json", "text"] = "json"
    enable_token_tracking: bool = True
    enable_latency_tracking: bool = True


@dataclass
class ResilienceConfig:
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: float = 30.0
    api_max_retries: int = 3
    api_backoff_factor: float = 0.5
    enable_llm_fallback: bool = True
    max_messages_in_context: int = 50
    context_trim_strategy: Literal["oldest_first", "keep_system"] = "oldest_first"


@dataclass
class CostConfig:
    session_token_budget: int = 50_000
    per_message_cost_alert_usd: float = 0.10


@dataclass
class StripeConfig:
    secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    success_url: str = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:8004/checkout/success")
    cancel_url: str = os.getenv("STRIPE_CANCEL_URL", "http://localhost:8004/checkout/cancel")


@dataclass
class RedisConfig:
    host: str = os.getenv("REDIS_HOST", "localhost")
    port: int = int(os.getenv("REDIS_PORT", "6379"))
    password: str = os.getenv("REDIS_PASSWORD", "")
    db: int = int(os.getenv("REDIS_DB", "0"))
    session_ttl: int = 3600
    cart_ttl: int = 86400       # 24 hours
    order_history_ttl: int = 2592000  # 30 days


@dataclass
class AgentConfig:
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    stripe: StripeConfig = field(default_factory=StripeConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)


# Singleton
CONFIG = AgentConfig()
