"""
llm.py — OpenAI-compatible async LLM client.

Supports any provider that speaks the OpenAI Chat Completions API:
  OpenAI, Anthropic (via proxy), Ollama, LM Studio, OpenRouter, Groq, etc.

Just set base_url + api_key in config.json.
"""

from __future__ import annotations

import logging
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
from typing import Callable, Awaitable

from src.config import LLMConfig
from src.constants import (
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
)

from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import ErrorCode, LLMError
from src.llm_error_classifier import classify_llm_error
from src.llm_provider import TokenUsage
from src.logging import get_correlation_id
from src.security.url_sanitizer import sanitize_url_for_logging
from src.utils.circuit_breaker import CircuitBreaker
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

        log.info(
            "LLM client initialized: model=%s, base_url=%s",
            cfg.model,
            sanitize_url_for_logging(cfg.base_url),
        )

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
        import asyncio

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
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or 0
                completion_tokens = usage.get("completion_tokens") or 0
                total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)
            else:
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
        if self._circuit_breaker.is_open():
            raise LLMError(
                message="LLM provider is temporarily unavailable (circuit breaker open)",
                suggestion="Please try again in a minute",
                error_code=ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
            )

        try:
            result = await self._raw_chat(messages, tools=tools, timeout=timeout, chat_id=chat_id)
            self._circuit_breaker.record_success()
            return result
        except LLMError:
            self._circuit_breaker.record_failure()
            raise
        except Exception as exc:
            self._circuit_breaker.record_failure()
            classified = classify_llm_error(exc)
            log.error(
                "LLM error classified: %s → %s (code=%s)",
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
        response is accumulated and returned as a normal
        :class:`ChatCompletion` so callers can inspect ``finish_reason``,
        ``tool_calls``, and ``usage`` exactly like :meth:`chat`.

        The ``on_chunk`` coroutine is called with each accumulated text
        segment once it exceeds ``STREAM_MIN_CHUNK_CHARS`` characters.
        This batches small deltas to avoid excessive channel sends.

        Falls back to non-streaming :meth:`chat` when the circuit breaker
        is open or when the provider is known not to support streaming.

        Args:
            messages: Conversation history for the LLM.
            tools: Optional tool definitions.
            timeout: Optional per-request timeout.
            on_chunk: Async callback receiving partial text chunks.

        Returns:
            Fully assembled :class:`ChatCompletion` (identical shape to
            the non-streaming :meth:`chat` return value).
        """
        # Circuit breaker: reject immediately when provider is down
        if self._circuit_breaker.is_open():
            raise LLMError(
                message="LLM provider is temporarily unavailable (circuit breaker open)",
                suggestion="Please try again in a minute",
                error_code=ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
            )

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

        # Initialise accumulator variables before the try block so the finally
        # block can safely reference them even if the stream fails immediately.
        accumulated_content = ""
        buffered_chunk = ""
        finish_reason = None
        tool_calls_data: list[dict] = []
        usage_data = None
        role = "assistant"

        try:

            stream = await self._client.chat.completions.create(**kwargs)
            async for event in stream:
                if not event.choices:
                    # Usage-only event at the end of the stream
                    if hasattr(event, "usage") and event.usage:
                        usage_data = event.usage
                    continue

                delta = event.choices[0].delta

                # Accumulate text content
                if delta.content:
                    accumulated_content += delta.content
                    buffered_chunk += delta.content

                    # Flush buffered text to callback when large enough
                    if on_chunk and len(buffered_chunk) >= STREAM_MIN_CHUNK_CHARS:
                        await on_chunk(buffered_chunk)
                        buffered_chunk = ""

                # Accumulate tool calls (arrive as fragments)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        # Extend list to fit index
                        while len(tool_calls_data) <= idx:
                            tool_calls_data.append(
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                            )
                        entry = tool_calls_data[idx]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["function"]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["function"]["arguments"] += tc_delta.function.arguments

                if delta.role:
                    role = delta.role

                # Capture finish reason from the final event
                if event.choices[0].finish_reason:
                    finish_reason = event.choices[0].finish_reason

                # Some providers send usage in the last event with choices
                if hasattr(event, "usage") and event.usage:
                    usage_data = event.usage

            # Flush any remaining buffered text
            if on_chunk and buffered_chunk:
                await on_chunk(buffered_chunk)

            # Track token usage from stream summary
            if usage_data:
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

            # Reconstruct a ChatCompletion object from accumulated data
            from openai.types.chat import ChatCompletion as _CC
            from openai.types.chat.chat_completion import Choice as _Choice
            from openai.types.chat import ChatCompletionMessage as _Msg
            from openai.types.chat.chat_completion_message_tool_call import (
                ChatCompletionMessageToolCall as _TCToolCall,
                Function as _TCFunction,
            )

            tc_objects = [
                _TCToolCall(
                    id=tc["id"],
                    type=tc["type"],
                    function=_TCFunction(name=tc["function"]["name"], arguments=tc["function"]["arguments"]),
                )
                for tc in tool_calls_data
            ]

            message = _Msg(
                content=accumulated_content or None,
                role=role,  # type: ignore[arg-type]
                tool_calls=tc_objects or None,
                function_call=None,
            )
            choice = _Choice(
                index=0,
                message=message,
                finish_reason=finish_reason or "stop",
            )

            completion = _CC(
                id="stream",
                choices=[choice],
                created=0,
                model=self._cfg.model,
                object="chat.completion",
                usage=usage_data,
            )

            await self._circuit_breaker.record_success()
            log.debug("LLM stream completed: finish_reason=%s", finish_reason)
            return completion

        except LLMError:
            await self._circuit_breaker.record_failure()
            raise
        except Exception as exc:
            await self._circuit_breaker.record_failure()
            classified = classify_llm_error(exc)
            log.error(
                "LLM streaming error classified: %s → %s (code=%s)",
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
        finally:
            # Best-effort flush of any remaining buffered text so the user
            # sees a partial response rather than nothing on stream failure.
            if on_chunk and buffered_chunk:
                try:
                    await on_chunk(buffered_chunk)
                    buffered_chunk = ""
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.STREAMING,
                        "Best-effort chunk flush failed in stream finally block",
                        logger=log,
                    )

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
