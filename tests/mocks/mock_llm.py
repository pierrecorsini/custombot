"""MockLLMProvider — configurable mock that satisfies the LLMProvider protocol.

Supports:
  - Default response: always returns the same ChatCompletion
  - Response queue: returns responses in sequence, then raises
  - Latency simulation: adds ``asyncio.sleep`` before each response
  - Call tracking: records every ``chat()`` / ``chat_stream()`` invocation

Usage::

    from tests.mocks.mock_llm import MockLLMProvider
    from tests.mocks.llm_responses import make_text_response

    provider = MockLLMProvider(default_response=make_text_response("Hi"))
    result = await provider.chat([{"role": "user", "content": "hello"}])
"""

from __future__ import annotations

import asyncio
from typing import Any

from unittest.mock import MagicMock

from src.exceptions import ErrorCode, LLMError
from src.llm._provider import TokenUsage
from src.utils.circuit_breaker import CircuitBreaker

from tests.mocks.llm_responses import make_text_response


class MockLLMProvider:
    """In-memory mock implementing the ``LLMProvider`` protocol.

    Parameters
    ----------
    default_response:
        The ChatCompletion returned when no response queue is set.
    response_queue:
        If provided, responses are consumed from this list in order.
        When the queue is exhausted, the next call raises
        ``IndexError`` (or ``LLMError`` if ``error_on_queue_empty`` is True).
    latency:
        Simulated LLM latency in seconds (default 0).
    error_on_queue_empty:
        When True, raise ``LLMError`` instead of ``IndexError`` when
        the response queue is exhausted.
    """

    def __init__(
        self,
        default_response: MagicMock | None = None,
        response_queue: list[MagicMock] | None = None,
        latency: float = 0.0,
        error_on_queue_empty: bool = False,
    ) -> None:
        self._default_response = default_response or make_text_response("mock response")
        self._response_queue = list(response_queue) if response_queue else []
        self._latency = latency
        self._error_on_queue_empty = error_on_queue_empty

        # Call tracking
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_stream_calls: list[dict[str, Any]] = []
        self._queue_index: int = 0

        # LLMProvider protocol properties
        self._token_usage = TokenUsage()
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30,
        )

    @property
    def token_usage(self) -> TokenUsage:
        return self._token_usage

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def openai_client(self) -> Any:
        raise NotImplementedError("MockLLMProvider does not provide an OpenAI client")

    def update_config(self, new_cfg: Any) -> None:
        """No-op for mock."""

    async def warmup(self) -> bool:
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        chat_id: str | None = None,
    ) -> MagicMock:
        """Return the next response, simulating latency if configured."""
        self.chat_calls.append({
            "messages": messages,
            "tools": tools,
            "timeout": timeout,
            "chat_id": chat_id,
        })

        if self._latency > 0:
            await asyncio.sleep(self._latency)

        return self._next_response()

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        on_chunk: Any = None,
        chat_id: str | None = None,
    ) -> MagicMock:
        """Return the next response (streaming is simulated as a single call)."""
        self.chat_stream_calls.append({
            "messages": messages,
            "tools": tools,
            "timeout": timeout,
            "on_chunk": on_chunk,
            "chat_id": chat_id,
        })

        if self._latency > 0:
            await asyncio.sleep(self._latency)

        return self._next_response()

    async def close(self) -> None:
        """No-op for mock."""

    def _next_response(self) -> MagicMock:
        """Return the next response from the queue or the default."""
        if self._response_queue and self._queue_index < len(self._response_queue):
            response = self._response_queue[self._queue_index]
            self._queue_index += 1
            return response

        if self._response_queue and self._queue_index >= len(self._response_queue):
            if self._error_on_queue_empty:
                raise LLMError(
                    "MockLLMProvider: response queue exhausted",
                    error_code=ErrorCode.LLM_TIMEOUT,
                )
            raise IndexError("MockLLMProvider: response queue exhausted")

        return self._default_response

    @property
    def call_count(self) -> int:
        """Total number of chat() + chat_stream() calls."""
        return len(self.chat_calls) + len(self.chat_stream_calls)

    def enqueue(self, response: MagicMock) -> None:
        """Append a response to the queue (for incremental setup)."""
        self._response_queue.append(response)
