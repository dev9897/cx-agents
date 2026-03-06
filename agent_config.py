"""
agent_config.py
===============
Centralised configuration for the SAP Commerce Shopping Agent.
All tunable knobs in one place — 12-factor-app style.
"""
from dotenv import load_dotenv
import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ClaudeConfig:
    # Model selection
    model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
    # Use Opus 4.6 for complex reasoning, Sonnet 4.6 for speed/cost balance

    # Token budgets
    max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))
    max_input_tokens: int = int(os.getenv("CLAUDE_MAX_INPUT_TOKENS", "4000"))

    # Prompt caching (reduces cost ~90% on repeated system prompts)
    enable_prompt_caching: bool = True

    # Streaming
    streaming: bool = True

    # Retry / resilience
    max_retries: int = 3
    timeout_seconds: float = 60.0

    # Temperature (0 = deterministic, best for tool use)
    temperature: float = 0.0


@dataclass
class ObservabilityConfig:
    # Tracing backend: "langsmith" | "none"
    tracing_backend: Literal["langsmith", "none"] = os.getenv("TRACING_BACKEND", "langsmith")
    langsmith_project: str = os.getenv("LANGSMITH_PROJECT", "sap-commerce-agent")

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: Literal["json", "text"] = "json"     # JSON for log aggregators

    # Metrics
    enable_token_tracking: bool = True
    enable_latency_tracking: bool = True


@dataclass
class ResilienceConfig:
    # Circuit breaker — stop calling SAP API if it's down
    circuit_breaker_threshold: int = 5       # failures before opening
    circuit_breaker_timeout: float = 30.0    # seconds before retry

    # Retry policy for SAP OCC API calls
    api_max_retries: int = 3
    api_backoff_factor: float = 0.5          # exponential backoff

    # Fallback: if Claude is unavailable, return canned error
    enable_llm_fallback: bool = True

    # Context window management — trim old messages automatically
    max_messages_in_context: int = 50
    context_trim_strategy: Literal["oldest_first", "keep_system"] = "oldest_first"


@dataclass
class CostConfig:
    # Alert if a single session exceeds this many input tokens
    session_token_budget: int = 50_000
    # Alert if a single message costs more than this (USD)
    per_message_cost_alert_usd: float = 0.10


@dataclass
class AgentConfig:
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)
    cost: CostConfig = field(default_factory=CostConfig)


# Singleton
CONFIG = AgentConfig()
