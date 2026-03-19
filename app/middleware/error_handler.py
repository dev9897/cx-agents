"""Error handling — circuit breaker, retry decorator, SSL diagnostics."""

import logging
import random
import ssl
import time
from functools import wraps
from typing import Optional

from app.config import CONFIG

logger = logging.getLogger("sap_agent.errors")


# ── Circuit Breaker ──────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self):
        self._failures = 0
        self._opened_at: Optional[float] = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at > CONFIG.resilience.circuit_breaker_timeout:
            self._failures = 0
            self._opened_at = None
            logger.info("Circuit breaker reset (half-open)")
            return False
        return True

    def record_success(self):
        self._failures = 0
        self._opened_at = None

    def record_failure(self):
        self._failures += 1
        if self._failures >= CONFIG.resilience.circuit_breaker_threshold:
            self._opened_at = time.time()
            logger.error("Circuit breaker OPENED after %d failures", self._failures)


sap_circuit_breaker = CircuitBreaker()


# ── Retry with Backoff ───────────────────────────────────────────────────────

def with_retry(max_attempts: int = 3, backoff_base: float = 1.0,
               retry_on: tuple = (ConnectionError, TimeoutError)):
    """Decorator for retrying functions with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)
                    if attempt > 0:
                        logger.info("Retry succeeded for %s on attempt %d", func.__name__, attempt + 1)
                    return result
                except retry_on as exc:
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        break
                    delay = backoff_base * (2 ** attempt)
                    jitter = delay * 0.25 * (2 * random.random() - 1)
                    wait = round(delay + jitter, 2)
                    logger.warning(
                        "Retry %d/%d for %s — waiting %.1fs | error: %s",
                        attempt + 1, max_attempts, func.__name__, wait, exc,
                    )
                    time.sleep(wait)
            raise last_exc
        return wrapper
    return decorator


# ── SSL Diagnostics ──────────────────────────────────────────────────────────

def is_ssl_error(exc: BaseException) -> bool:
    cause = exc.__cause__ or exc
    return (
        isinstance(cause, ssl.SSLError)
        or "CERTIFICATE_VERIFY_FAILED" in str(cause)
        or "SSL" in type(cause).__name__.upper()
    )


def log_ssl_error(exc: BaseException, context: str, url: str = "") -> None:
    cause = exc.__cause__ or exc
    logger.error(
        "SSL_ERROR | context=%s | url=%s | error=%s | OpenSSL=%s | cafile=%s",
        context, url or "(unknown)", cause,
        ssl.OPENSSL_VERSION,
        ssl.get_default_verify_paths().cafile,
    )


# ── Overload Detection ───────────────────────────────────────────────────────

def is_overload_error(exc: BaseException) -> bool:
    err_str = str(exc)
    return (
        "overloaded_error" in err_str
        or "Overloaded" in err_str
        or getattr(exc, "status_code", None) == 529
    )
