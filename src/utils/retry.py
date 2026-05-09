"""
retry.py — Retry decorator with exponential backoff for transient failures.

Provides a configurable @retry_with_backoff decorator that:
  - Retries on transient errors (rate limits, timeouts, 5xx errors)
  - Uses exponential backoff with jitter to prevent thundering herd
  - Logs retry attempts for observability

Usage:
    from src.utils.retry import retry_with_backoff

    @retry_with_backoff(max_retries=3, initial_delay=1.0)
    async def call_external_api():
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from typing import Any, Callable, Coroutine, Set, Tuple, TypeVar, Union

from src.exceptions import LLMError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Default delay multipliers for exponential backoff
BACKOFF_MULTIPLIER = 2
JITTER_FACTOR = 0.1  # 10% jitter

# HTTP status codes that indicate transient errors
TRANSIENT_STATUS_CODES: Set[int] = {
    408,  # Request Timeout
    429,  # Too Many Requests (rate limit)
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
}

# Error type substrings that indicate transient failures
TRANSIENT_ERROR_PATTERNS: Tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "overloaded",
    "capacity",
    "try again",
)

# ── OpenAI SDK typed exception classification ──────────────────────────────

try:
    from openai import (
        APIConnectionError as _APIConnectionError,
        APITimeoutError as _APITimeoutError,
        AuthenticationError as _AuthenticationError,
        BadRequestError as _BadRequestError,
        ConflictError as _ConflictError,
        InternalServerError as _InternalServerError,
        NotFoundError as _NotFoundError,
        PermissionDeniedError as _PermissionDeniedError,
        RateLimitError as _RateLimitError,
        UnprocessableEntityError as _UnprocessableEntityError,
    )

    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

# Retryable: network, timeout, rate-limit, server errors
_RETRYABLE_OPENAI_TYPES: Tuple[type, ...] = (
    (_APIConnectionError, _APITimeoutError, _RateLimitError, _InternalServerError)
    if _OPENAI_AVAILABLE
    else ()
)

# Non-retryable: auth, bad request, not found, permission, conflict, unprocessable
_NON_RETRYABLE_OPENAI_TYPES: Tuple[type, ...] = (
    (
        _AuthenticationError,
        _BadRequestError,
        _PermissionDeniedError,
        _NotFoundError,
        _ConflictError,
        _UnprocessableEntityError,
    )
    if _OPENAI_AVAILABLE
    else ()
)


def is_transient_error(error: Exception) -> bool:
    """
    Determine if an error is transient and should be retried.

    Classification strategy (ordered by reliability):

    1. **OpenAI SDK typed exceptions** — isinstance checks against the SDK's
       exception hierarchy.  Retryable: ``APIConnectionError``,
       ``APITimeoutError``, ``RateLimitError``, ``InternalServerError``.
       Non-retryable: ``AuthenticationError``, ``BadRequestError``,
       ``PermissionDeniedError``, ``NotFoundError``, ``ConflictError``,
       ``UnprocessableEntityError``.

    2. **HTTP status code** — ``getattr(error, "status_code")`` compared
       against :data:`TRANSIENT_STATUS_CODES`.

    3. **String pattern matching** — last resort heuristic matching against
       :data:`TRANSIENT_ERROR_PATTERNS`.

    4. **Wrapped cause chain** — recurses into ``error.__cause__``.

    Args:
        error: The exception to check.

    Returns:
        True if the error is transient and should be retried.
    """
    # 1. Type-based OpenAI classification (most reliable)
    if _OPENAI_AVAILABLE:
        if isinstance(error, _NON_RETRYABLE_OPENAI_TYPES):
            log.debug("Non-retryable OpenAI error: %s", type(error).__name__)
            return False
        if isinstance(error, _RETRYABLE_OPENAI_TYPES):
            log.debug("Retryable OpenAI error: %s", type(error).__name__)
            return True

    # 2. HTTP status-code check
    status_code = getattr(error, "status_code", None)
    if status_code is not None and status_code in TRANSIENT_STATUS_CODES:
        return True

    # 3. Response object status-code check (OpenAI-specific structure)
    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None)
        if resp_status is not None and resp_status in TRANSIENT_STATUS_CODES:
            return True

    # 4. String pattern matching (least reliable, fallback)
    error_str = str(error).lower()
    for pattern in TRANSIENT_ERROR_PATTERNS:
        if pattern in error_str:
            return True

    # 5. Wrapped cause chain
    if error.__cause__ is not None:
        return is_transient_error(error.__cause__)

    return False


def calculate_delay_with_jitter(base_delay: float) -> float:
    """
    Calculate delay with random jitter to prevent thundering herd.

    Applies ±10% jitter to the base delay.

    Args:
        base_delay: The base delay in seconds

    Returns:
        Delay with jitter applied
    """
    jitter = base_delay * JITTER_FACTOR * random.uniform(-1, 1)
    return max(0.0, base_delay + jitter)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    retryable_exceptions: Tuple[type, ...] = (Exception,),
    max_total_seconds: float | None = None,
) -> Callable[
    [Callable[..., Coroutine[Any, Any, T]]],
    Callable[..., Coroutine[Any, Any, T]],
]:
    """
    Decorator that retries async functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        retryable_exceptions: Tuple of exception types to catch (default: all)
        max_total_seconds: Maximum total elapsed seconds across all attempts.
            When set, retries stop once the cumulative wall-clock time (including
            call durations and backoff waits) exceeds this budget.  Defaults to
            ``None`` (no time budget, only attempt-count limiting).

    Returns:
        Decorated function with retry logic

    Example:
        @retry_with_backoff(max_retries=3, initial_delay=1.0)
        async def call_llm():
            return await client.chat(messages)

        @retry_with_backoff(max_retries=5, initial_delay=0.5, max_total_seconds=180)
        async def fetch_data():
            return await api.get("/data")
    """

    def decorator(
        func: Callable[..., Coroutine[Any, Any, T]],
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: Exception | None = None
            delay = initial_delay
            start_time = time.monotonic() if max_total_seconds is not None else 0.0

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as error:
                    last_error = error

                    # Check if this is a transient error worth retrying
                    if not is_transient_error(error):
                        log.debug(
                            "Non-transient error, not retrying: %s",
                            error,
                            exc_info=True,
                        )
                        raise

                    # Check if we've exhausted retries
                    if attempt >= max_retries:
                        log.warning(
                            "Retry exhausted after %d attempts: %s",
                            max_retries + 1,
                            error,
                        )
                        raise

                    # Check if the time budget is exhausted
                    if max_total_seconds is not None:
                        elapsed = time.monotonic() - start_time
                        if elapsed >= max_total_seconds:
                            log.warning(
                                "Retry budget exhausted: %.1fs/%.1fs after %d attempts: %s",
                                elapsed,
                                max_total_seconds,
                                attempt + 1,
                                error,
                            )
                            raise

                    # Calculate delay with jitter
                    actual_delay = calculate_delay_with_jitter(delay)

                    log.info(
                        "Retry attempt %d/%d after %.2fs (error: %s)",
                        attempt + 1,
                        max_retries,
                        actual_delay,
                        type(error).__name__,
                    )

                    await asyncio.sleep(actual_delay)
                    delay *= BACKOFF_MULTIPLIER

            # Should never reach here, but satisfy type checker
            if last_error:
                raise last_error
            raise RuntimeError("Unexpected state in retry logic")

        return wrapper

    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# Convenience Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "retry_with_backoff",
    "is_transient_error",
    "calculate_delay_with_jitter",
    "TRANSIENT_STATUS_CODES",
    "TRANSIENT_ERROR_PATTERNS",
]
