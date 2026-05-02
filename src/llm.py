"""
llm.py — OpenAI-compatible async LLM client.

Supports any provider that speaks the OpenAI Chat Completions API:
  OpenAI, Anthropic (via proxy), Ollama, LM Studio, OpenRouter, Groq, etc.

Just set base_url + api_key in config.json.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path as _Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from typing import Callable, Awaitable

from src.config import LLMConfig
from src.constants import (
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DEFAULT_HTTPX_MAX_CONNECTIONS,
    DEFAULT_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
    LLM_HEALTH_PROBE_INTERVAL_SECONDS,
    LLM_WARMUP_TIMEOUT,
    WORKSPACE_DIR,
)

from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.stream_accumulator import StreamAccumulator
from src.exceptions import ConfigurationError, ErrorCode, LLMError
from src.llm_error_classifier import classify_llm_error
from src.llm_provider import TokenUsage
from src.logging import get_correlation_id
from src.security.url_sanitizer import sanitize_url_for_logging
from src.utils.circuit_breaker import CircuitBreaker, CircuitState
from src.utils.retry import retry_with_backoff
from src.utils.type_guards import is_llm_config

if TYPE_CHECKING:
    from src.logging.llm_logging import LLMLogger

log = logging.getLogger(__name__)

# Backward-compatible alias — prefer importing from src.llm_error_classifier directly.
_classify_llm_error = classify_llm_error


# Re-export TokenUsage from its canonical location for backward compatibility.
# Consumers that imported ``from src.llm import TokenUsage`` will continue to work.
__all__ = ["LLMClient", "TokenUsage"]


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
                max_connections=DEFAULT_HTTPX_MAX_CONNECTIONS,
                max_keepalive_connections=DEFAULT_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
            ),
            timeout=httpx.Timeout(
                timeout=cfg.timeout or 120.0,
                connect=10.0,
            ),
        )
        self._client = AsyncOpenAI(
            api_key=self._resolve_api_key(cfg),
            base_url=cfg.base_url,
            http_client=self._http_client,
        )
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            cooldown_seconds=CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        self._health_probe_task: asyncio.Task[None] | None = None
        self._llm_logger: LLMLogger | None = None
        if log_llm:
            from src.logging.llm_logging import LLMLogger

            log_dir = str(_Path(WORKSPACE_DIR) / "logs" / "llm")
            self._llm_logger = LLMLogger(log_dir)
            log.info("LLM request/response logging enabled → %s", log_dir)

        log.info(
            "LLM client initialized: model=%s, base_url=%s",
            cfg.model,
            sanitize_url_for_logging(cfg.base_url),
        )

    # ── api key resolution ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_api_key(cfg: LLMConfig) -> str:
        """Return the API key or raise for remote providers missing one."""
        if cfg.api_key:
            return cfg.api_key

        parsed = urlparse(cfg.base_url)
        hostname = (parsed.hostname or "").lower()
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return "not-configured"  # local servers typically ignore the key

        # Allow empty key for RFC 1918 private network addresses (LAN LLM servers)
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private:
                return "not-configured"
        except ValueError:
            pass  # Not an IP address — proceed to raise

        raise ConfigurationError(
            "API key is required for remote LLM providers",
            config_key="llm.api_key",
        )

    # ── health probe ────────────────────────────────────────────────────────

    def _ensure_health_probe(self) -> None:
        """Spawn a background health probe if the circuit breaker is OPEN.

        Idempotent: if a probe is already running or the breaker is not OPEN,
        this is a no-op.  The probe polls ``models.list()`` at a fixed
        interval and force-closes the breaker as soon as the provider
        recovers — avoiding the full cooldown wait.
        """
        if self._health_probe_task is not None:
            return
        if self._circuit_breaker.state != CircuitState.OPEN:
            return
        self._health_probe_task = asyncio.create_task(self._health_probe_loop())
        log.info("LLM health probe started (interval=%.0fs)", LLM_HEALTH_PROBE_INTERVAL_SECONDS)

    async def _health_probe_loop(self) -> None:
        """Background task polling the LLM provider while the breaker is open."""
        try:
            while self._circuit_breaker.state == CircuitState.OPEN:
                await asyncio.sleep(LLM_HEALTH_PROBE_INTERVAL_SECONDS)
                # Re-check after sleep — breaker may have closed naturally.
                if self._circuit_breaker.state != CircuitState.OPEN:
                    break
                try:
                    await asyncio.wait_for(self._client.models.list(), timeout=5.0)
                    await self._circuit_breaker.force_close()
                    log.info("LLM health probe succeeded — circuit breaker force-closed")
                    return
                except Exception as exc:
                    log_noncritical(
                        NonCriticalCategory.HEALTH_CHECK,
                        f"LLM health probe failed: {type(exc).__name__}: {exc}",
                        logger=log,
                    )
        finally:
            self._health_probe_task = None

    # ── circuit breaker + error classification context manager ─────────────

    @asynccontextmanager
    async def _circuit_protected_call(self, label: str = "LLM") -> AsyncIterator[None]:
        """Context manager that handles circuit breaker checks and error classification.

        Eliminates the duplicated try/except pattern in :meth:`chat` and
        :meth:`chat_stream`.  On entry it checks the circuit breaker; on
        normal exit it records success; on exception it records failure,
        classifies the error, logs it, tracks it in metrics, and re-raises.

        Args:
            label: Prefix for log messages (e.g. ``"LLM"`` or ``"LLM streaming"``).
        """
        if await self._circuit_breaker.is_open():
            raise LLMError(
                message="LLM provider is temporarily unavailable (circuit breaker open)",
                suggestion="Please try again in a minute",
                error_code=ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
            )
        try:
            yield
            await self._circuit_breaker.record_success()
        except LLMError:
            await self._circuit_breaker.record_failure()
            self._ensure_health_probe()
            raise
        except Exception as exc:
            await self._circuit_breaker.record_failure()
            self._ensure_health_probe()
            classified = classify_llm_error(exc)
            log.error(
                "%s error classified: %s → %s (code=%s)",
                label,
                type(exc).__name__,
                classified.message,
                classified.error_code.value,
                exc_info=True,
            )
            from src.monitoring.performance import get_metrics_collector

            get_metrics_collector().track_llm_error(
                classified.error_code.value if classified.error_code else "ERR_9000"
            )
            raise classified from exc

    # ── public API ─────────────────────────────────────────────────────────

    @property
    def token_usage(self) -> TokenUsage:
        """Token usage statistics for this client instance."""
        return self._token_usage

    async def warmup(self) -> bool:
        """Pre-establish the TCP + TLS connection to the LLM provider.

        Sends a lightweight ``models.list()`` request during startup so the
        first real user message doesn't pay the cold-start handshake cost
        (typically 1–3 s for remote proxies).  Failures are non-fatal and
        simply logged — the first message will still work, just slower.

        Returns ``True`` if the warmup succeeded, ``False`` otherwise.
        """
        try:
            await asyncio.wait_for(
                self._client.models.list(),
                timeout=LLM_WARMUP_TIMEOUT,
            )
            log.info("LLM connection warmup succeeded")
            return True
        except Exception as exc:
            log.warning(
                "LLM connection warmup failed (non-fatal): %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    @retry_with_backoff(max_retries=3, initial_delay=1.0, max_total_seconds=180)
    async def _raw_chat(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
        chat_id: Optional[str] = None,
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
            prompt_tokens = usage.prompt_tokens or 0
            completion_tokens = usage.completion_tokens or 0
            total_tokens = usage.total_tokens or (prompt_tokens + completion_tokens)

            if chat_id:
                self._token_usage.add_for_chat(chat_id, prompt_tokens, completion_tokens)
            else:
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
        chat_id: Optional[str] = None,
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
            chat_id: Optional chat ID for per-chat token tracking.
        """
        # Circuit breaker: reject immediately when provider is down
        async with self._circuit_protected_call("LLM"):
            return await self._raw_chat(messages, tools=tools, timeout=timeout, chat_id=chat_id)

    @retry_with_backoff(max_retries=3, initial_delay=1.0, max_total_seconds=180)
    async def _create_stream(self, **kwargs: Any) -> Any:
        """Create a streaming response with retry on transient handshake failures.

        Only wraps the initial ``create()`` call — stream consumption
        (iterating events) is **not** retried.  Uses the same retry
        parameters as :meth:`_raw_chat` for consistency.
        """
        return await self._client.chat.completions.create(**kwargs)

    async def chat_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
        on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
        chat_id: Optional[str] = None,
    ) -> ChatCompletion:
        """Stream an LLM response, forwarding text deltas via *on_chunk*.

        Uses ``stream=True`` to receive token-by-token deltas.  The full
        response is accumulated via :class:`StreamAccumulator` and returned
        as a normal :class:`ChatCompletion` so callers can inspect
        ``finish_reason``, ``tool_calls``, and ``usage`` exactly like
        :meth:`chat`.

        Transient errors during stream establishment are retried via
        :meth:`_create_stream`.  Stream consumption is not retried.

        Args:
            messages: Conversation history for the LLM.
            tools: Optional tool definitions.
            timeout: Optional per-request timeout.
            on_chunk: Async callback receiving partial text chunks.
            chat_id: Optional chat ID for per-chat token tracking.

        Returns:
            Fully assembled :class:`ChatCompletion` (identical shape to
            the non-streaming :meth:`chat` return value).
        """
        kwargs: Dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "timeout": timeout if timeout is not None else self._cfg.timeout,
        }
        if self._cfg.max_tokens is not None:
            kwargs["max_tokens"] = self._cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        log.debug(
            "LLM streaming request: model=%s, messages=%d",
            self._cfg.model,
            len(messages),
        )

        acc = StreamAccumulator(model=self._cfg.model, on_chunk=on_chunk)

        try:
            async with self._circuit_protected_call("LLM streaming"):
                stream = await self._create_stream(**kwargs)
                async for event in stream:
                    await acc.process_event(event)

                await acc.flush_remaining()
                self._track_stream_usage(acc.usage_data, chat_id)

                completion = acc.build_completion()
                log.debug("LLM stream completed: finish_reason=%s", acc.finish_reason)
                return completion
        finally:
            try:
                await acc.best_effort_flush()
            except Exception:
                pass  # best-effort by definition — never mask the original error

    def _track_stream_usage(self, usage_data: Any, chat_id: Optional[str]) -> None:
        """Record token usage from stream summary data."""
        if not usage_data:
            return
        if isinstance(usage_data, dict):
            prompt_tokens = usage_data.get("prompt_tokens") or 0
            completion_tokens = usage_data.get("completion_tokens") or 0
        else:
            prompt_tokens = usage_data.prompt_tokens or 0
            completion_tokens = usage_data.completion_tokens or 0
        if chat_id:
            self._token_usage.add_for_chat(chat_id, prompt_tokens, completion_tokens)
        else:
            self._token_usage.add(prompt_tokens, completion_tokens)

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """Expose the circuit breaker for health checks and diagnostics."""
        return self._circuit_breaker

    @property
    def openai_client(self) -> AsyncOpenAI:
        """Underlying OpenAI client for embeddings / models.list().

        Used by VectorMemory to obtain an embeddings client without
        coupling to LLMClient internals via ``_client``.
        """
        return self._client

    async def close(self) -> None:
        """Close the underlying httpx connection pool for clean shutdown."""
        if self._health_probe_task is not None:
            self._health_probe_task.cancel()
            try:
                await self._health_probe_task
            except asyncio.CancelledError:
                pass
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
