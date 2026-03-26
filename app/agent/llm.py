"""
LLM factory — provider-agnostic LLM creation, retry logic, and token tracking.

Encapsulates all provider-specific setup (Anthropic, Azure OpenAI, Gemini)
behind a single interface that graph.py consumes.
"""

from __future__ import annotations

import logging
import os
import random
import time

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_core.runnables import RunnableConfig
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import AzureChatOpenAI

from app.agent.state import ShoppingState
from app.config import CONFIG
from app.middleware.error_handler import is_overload_error

logger = logging.getLogger("sap_agent")


# ── Token Tracking ───────────────────────────────────────────────────────────

class TokenTracker:
    COST_MAP = {
        "anthropic":    {"input": 0.003, "output": 0.015, "cache_read": 0.0003},
        "azure_openai": {"input": 0.005, "output": 0.015, "cache_read": 0.0},
        "gemini":       {"input": 0.0,   "output": 0.0,   "cache_read": 0.0},
    }

    @staticmethod
    def update(state: ShoppingState, response: AIMessage, provider: str = "") -> dict:
        usage = response.usage_metadata or {}
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        cached = usage.get("cache_read_input_tokens", 0)

        # Azure OpenAI reports tokens in response_metadata instead of usage_metadata
        if not in_tokens and not out_tokens:
            resp_meta = getattr(response, "response_metadata", {}) or {}
            token_usage = resp_meta.get("token_usage", {})
            in_tokens = token_usage.get("prompt_tokens", 0)
            out_tokens = token_usage.get("completion_tokens", 0)

        new_in = state.get("total_input_tokens", 0) + in_tokens
        new_out = state.get("total_output_tokens", 0) + out_tokens

        costs = TokenTracker.COST_MAP.get(provider, TokenTracker.COST_MAP["anthropic"])
        cost = (
            (in_tokens / 1000) * costs["input"]
            + (out_tokens / 1000) * costs["output"]
            + (cached / 1000) * costs["cache_read"]
        )

        if new_in > CONFIG.cost.session_token_budget:
            logger.warning("Session token budget exceeded: %d tokens", new_in)

        logger.info(
            "tokens | session=%s provider=%s in=%d out=%d cached=%d cost_usd=%.4f",
            state.get("session_id", "?"), provider, new_in, new_out, cached, cost,
        )
        return {"total_input_tokens": new_in, "total_output_tokens": new_out}


# ── LLM Factory ─────────────────────────────────────────────────────────────

class LLMFactory:
    """Creates and manages the LLM instance based on the configured provider."""

    def __init__(self) -> None:
        self._provider = CONFIG.llm_provider.lower()
        self._llm: BaseChatModel = self._create()
        self._overload_max_retries = int(os.getenv("ANTHROPIC_OVERLOAD_RETRIES", "4"))
        self._overload_base_delay = float(os.getenv("ANTHROPIC_OVERLOAD_BASE_DELAY", "2.0"))

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def llm(self) -> BaseChatModel:
        return self._llm

    def bind_tools(self, tools: list) -> BaseChatModel:
        return self._llm.bind_tools(tools)

    def invoke_with_retry(self, llm, messages, config: RunnableConfig):
        last_exc = None
        for attempt in range(self._overload_max_retries + 1):
            try:
                return llm.invoke(messages, config=config)
            except Exception as exc:
                if not is_overload_error(exc):
                    raise
                last_exc = exc
                if attempt == self._overload_max_retries:
                    break
                delay = self._overload_base_delay * (2 ** attempt)
                jitter = delay * 0.25 * (2 * random.random() - 1)
                wait = round(delay + jitter, 2)
                logger.warning(
                    "LLM overloaded (%s) | attempt=%d/%d | retrying in %.1fs",
                    self._provider, attempt + 1, self._overload_max_retries, wait,
                )
                time.sleep(wait)
        logger.error("LLM still overloaded after %d retries (%s)",
                     self._overload_max_retries, self._provider)
        raise last_exc

    def track_tokens(self, state: ShoppingState, response: AIMessage) -> dict:
        return TokenTracker.update(state, response, self._provider)

    # ── Private ──────────────────────────────────────────────────────────────

    def _create(self) -> BaseChatModel:
        if self._provider == "azure_openai":
            return self._create_azure_openai()
        elif self._provider == "gemini":
            return self._create_gemini()
        else:
            return self._create_anthropic()

    def _create_azure_openai(self) -> AzureChatOpenAI:
        cfg = CONFIG.azure_openai
        logger.info("LLM provider: Azure OpenAI (%s)", cfg.deployment_name)
        return AzureChatOpenAI(
            azure_deployment=cfg.deployment_name,
            api_key=cfg.api_key,
            azure_endpoint=cfg.endpoint,
            api_version=cfg.api_version,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            streaming=cfg.streaming,
        )

    def _create_gemini(self) -> ChatGoogleGenerativeAI:
        cfg = CONFIG.gemini
        logger.info("LLM provider: Google Gemini (%s)", cfg.model)
        return ChatGoogleGenerativeAI(
            model=cfg.model,
            google_api_key=cfg.api_key,
            max_output_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
        )

    def _create_anthropic(self) -> ChatAnthropic:
        cfg = CONFIG.claude
        logger.info("LLM provider: Anthropic Claude (%s)", cfg.model)
        rate_lim = InMemoryRateLimiter(
            requests_per_second=0.5, check_every_n_seconds=0.1, max_bucket_size=10,
        )
        return ChatAnthropic(
            base_url="https://api.anthropic.com",
            model=cfg.model,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            streaming=cfg.streaming,
            rate_limiter=rate_lim,
            default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
