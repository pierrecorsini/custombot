"""
llm.py — OpenAI-compatible async LLM client.

Supports any provider that speaks the OpenAI Chat Completions API:
  OpenAI, Anthropic (via proxy), Ollama, LM Studio, OpenRouter, Groq, etc.

Just set base_url + api_key in config.json.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import httpx
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)

from src.config import LLMConfig
from src.constants import (
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    WORKSPACE_DIR,
)
from src.exceptions import ErrorCode, LLMError
from src.logging import get_correlation_id
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.retry import retry_with_backoff
from src.utils.type_guards import is_llm_config

if TYPE_CHECKING:
    from src.logging.llm_logging import LLMLogger

log = logging.getLogger(__name__)


def _classify_llm_error(error: Exception) -> LLMError:
    """Map an OpenAI API exception to a structured :class:`LLMError`.

    Uses ``isinstance`` checks against the OpenAI SDK exception hierarchy
    so that each error category gets the right :class:`ErrorCode`,
    user-facing message, and actionable suggestion.

    Args:
        error: A raw exception raised by the OpenAI SDK.

    Returns:
        An :class:`LLMError` with classified ``error_code`` and ``suggestion``.
    """
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )

    if isinstance(error, AuthenticationError):
        return LLMError(
            message="LLM API authentication failed",
            suggestion="Check your API key in config.json",
            error_code=ErrorCode.LLM_API_KEY_INVALID,
            provider="openai",
        )
    if isinstance(error, PermissionDeniedError):
        return LLMError(
            message="LLM API permission denied",
            suggestion="Verify your API key has access to the requested model",
            error_code=ErrorCode.LLM_API_KEY_INVALID,
            provider="openai",
        )
    if isinstance(error, RateLimitError):
        return LLMError(
            message="LLM API rate limit exceeded",
            suggestion="Wait a moment and try again",
            error_code=ErrorCode.LLM_RATE_LIMITED,
        )
    if isinstance(error, APITimeoutError):
        return LLMError(
            message="LLM API request timed out",
            suggestion="Try again or increase the timeout in config",
            error_code=ErrorCode.LLM_TIMEOUT,
        )
    if isinstance(error, NotFoundError):
        return LLMError(
            message=f"LLM model not found: {error}",
            suggestion="Check the model name in config.json",
            error_code=ErrorCode.LLM_MODEL_UNAVAILABLE,
        )
    if isinstance(error, APIConnectionError):
        return LLMError(
            message="Could not connect to LLM API",
            suggestion="Check your network connection and base_url in config.json",
            error_code=ErrorCode.LLM_CONNECTION_FAILED,
        )
    if isinstance(error, BadRequestError):
        error_msg = str(error).lower()
        if any(
            token in error_msg
            for token in ("context_length", "context length", "max_tokens", "too many tokens")
        ):
            return LLMError(
                message="Conversation exceeds model's context length",
                suggestion="Start a new conversation or reduce message history",
                error_code=ErrorCode.LLM_CONTEXT_LENGTH_EXCEEDED,
            )
        return LLMError(
            message=f"LLM API bad request: {error}",
            suggestion="Check your request parameters",
            error_code=ErrorCode.LLM_INVALID_REQUEST,
        )

    # Generic fallback for any other API error
    return LLMError(
        message=f"LLM API error: {error}",
        suggestion="Check the error details and try again",
    )


@dataclass
class TokenUsage:
    """Token usage statistics for LLM API calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> Dict[str, int]:
        """Convert to dictionary for serialization."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
        }

    def add(self, prompt: int, completion: int) -> None:
        """Add token usage from a single request (thread-safe)."""
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += prompt + completion
            self.request_count += 1


class LLMClient:
    def __init__(
        self, cfg: LLMConfig, *, log_llm: bool = False, token_usage: TokenUsage | None = None
    ) -> None:
        # Runtime validation for LLM config
        if not is_llm_config(cfg):
            raise ValueError(f"Invalid LLMConfig provided: {cfg!r}")
        self._cfg = cfg
        self._token_usage = token_usage if token_usage is not None else TokenUsage()
        self._http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
            timeout=httpx.Timeout(
                timeout=cfg.timeout or 120.0,
                connect=10.0,
            ),
        )
        self._client = AsyncOpenAI(
            api_key=cfg.api_key or "sk-no-key",  # some local servers ignore it
            base_url=cfg.base_url,
            http_client=self._http_client,
        )
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            cooldown_seconds=CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        self._llm_logger: LLMLogger | None = None
        if log_llm:
            from src.logging.llm_logging import LLMLogger

            log_dir = f"{WORKSPACE_DIR}/logs/llm"
            self._llm_logger = LLMLogger(log_dir)
            log.info("LLM request/response logging enabled → %s", log_dir)

    # ── public API ─────────────────────────────────────────────────────────

    @property
    def token_usage(self) -> TokenUsage:
        """Token usage statistics for this client instance."""
        return self._token_usage

    @retry_with_backoff(max_retries=3, initial_delay=1.0, max_total_seconds=180)
    async def _raw_chat(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
    ) -> ChatCompletion:
        """Low-level LLM call with retry.  Use :meth:`chat` from callers."""
        kwargs: Dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "timeout": timeout if timeout is not None else self._cfg.timeout,
        }
        if self._cfg.max_tokens is not None:
            kwargs["max_tokens"] = self._cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        log.debug(
            "LLM request: model=%s, messages=%d, timeout=%s",
            self._cfg.model,
            len(messages),
            kwargs.get("timeout"),
        )

        # ── LLM file logging (request) ────────────────────────────────────
        request_ts = None
        if self._llm_logger:
            req_id = self._llm_logger.new_request_id()
            request_ts = self._llm_logger.log_request(
                request_id=req_id,
                model=self._cfg.model,
                messages=messages,
                tools=tools,
                temperature=self._cfg.temperature,
                max_tokens=self._cfg.max_tokens,
            )

        response = await self._client.chat.completions.create(**kwargs)

        # ── LLM file logging (response) ───────────────────────────────────
        if self._llm_logger and request_ts:
            self._llm_logger.log_response(
                request_id=req_id,
                model=self._cfg.model,
                response=response,
                request_ts=request_ts,
            )

        # Log token usage from response
        if response.usage:
            usage = response.usage
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or 0
                completion_tokens = usage.get("completion_tokens") or 0
                total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
            else:
                prompt_tokens = usage.prompt_tokens or 0
                completion_tokens = usage.completion_tokens or 0
                total_tokens = usage.total_tokens or (prompt_tokens + completion_tokens)

            self._token_usage.add(prompt_tokens, completion_tokens)

            corr_id = get_correlation_id()
            log.debug(
                "LLM token usage: model=%s prompt=%d completion=%d total=%d corr_id=%s",
                self._cfg.model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                corr_id or "none",
                extra={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "correlation_id": corr_id,
                },
            )

            if self._token_usage.request_count % 10 == 0:
                log.info(
                    "LLM session totals: prompt=%d completion=%d total=%d requests=%d",
                    self._token_usage.prompt_tokens,
                    self._token_usage.completion_tokens,
                    self._token_usage.total_tokens,
                    self._token_usage.request_count,
                )

        if not response.choices:
            raise LLMError(
                "LLM API returned empty choices (content may have been filtered)"
            )

        log.debug(
            "LLM response: finish_reason=%s, tokens=%s",
            response.choices[0].finish_reason,
            response.usage,
        )
        return response

    async def chat(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
    ) -> ChatCompletion:
        """Call the LLM with retry, structured error classification, and circuit breaker.

        Wraps :meth:`_raw_chat` to convert raw OpenAI SDK exceptions into
        classified :class:`LLMError` instances with error codes and
        actionable suggestions.

        A circuit breaker protects against cascading failures: after N
        consecutive failures all calls are short-circuited for a cooldown
        period so callers do not wait for the full LLM timeout.

        The caller (bot.py) drives the ReAct loop: it checks
        ``finish_reason`` and appends tool results before calling again.

        Args:
            messages: List of message dicts.
            tools: Optional list of tool definitions.
            timeout: Optional timeout in seconds (default from config).
        """
        # Circuit breaker: reject immediately when provider is down
        if await self._circuit_breaker.is_open():
            raise LLMError(
                message="LLM provider is temporarily unavailable (circuit breaker open)",
                suggestion="Please try again in a minute",
                error_code=ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
            )

        try:
            result = await self._raw_chat(messages, tools=tools, timeout=timeout)
            await self._circuit_breaker.record_success()
            return result
        except LLMError:
            await self._circuit_breaker.record_failure()
            raise
        except Exception as exc:
            await self._circuit_breaker.record_failure()
            classified = _classify_llm_error(exc)
            log.error(
                "LLM error classified: %s → %s (code=%s)",
                type(exc).__name__,
                classified.message,
                classified.error_code.value,
                exc_info=True,
            )
            raise classified from exc

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Expose the circuit breaker for health checks and diagnostics."""
        return self._circuit_breaker

    async def close(self) -> None:
        """Close the underlying httpx connection pool for clean shutdown."""
        await self._http_client.aclose()
        log.debug("LLM client connection pool closed")

    @staticmethod
    def tool_call_to_dict(message: ChatCompletionMessage) -> ChatCompletionAssistantMessageParam:
        """Convert a tool-call assistant message to a plain dict for context.

        .. deprecated::
            Use :func:`src.core.serialization.serialize_tool_call_message` instead.
        """
        from src.core.serialization import serialize_tool_call_message

        return serialize_tool_call_message(message)
