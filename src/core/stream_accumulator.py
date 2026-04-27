"""
stream_accumulator.py — Reconstructs a ChatCompletion from SSE streaming deltas.

Accumulates text content, tool call fragments, role, finish_reason, and usage
data from a stream of OpenAI SSE events into a fully reconstructed
:class:`~openai.types.chat.ChatCompletion` object.

Extracted from :meth:`LLMClient.chat_stream` to make the streaming logic
independently testable and reusable for future providers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from src.constants import STREAM_MIN_CHUNK_CHARS
from src.core.errors import NonCriticalCategory, log_noncritical

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion

log = logging.getLogger(__name__)

__all__ = ["StreamAccumulator"]


@dataclass
class StreamAccumulator:
    """Accumulates SSE streaming deltas into a ChatCompletion.

    Processes events from ``async for event in stream``, handling text
    content accumulation with buffered chunk flushing, tool call fragment
    assembly, role/finish_reason/usage capture, and ChatCompletion
    reconstruction.

    Usage::

        acc = StreamAccumulator(model="gpt-4o", on_chunk=callback)
        async for event in stream:
            await acc.process_event(event)
        await acc.flush_remaining()
        completion = acc.build_completion()
    """

    model: str
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None

    # Accumulation state
    _accumulated_content: str = field(default="", init=False, repr=False)
    _buffered_chunk: str = field(default="", init=False, repr=False)
    _finish_reason: Optional[str] = field(default=None, init=False, repr=False)
    _tool_calls_data: list[dict[str, str]] = field(
        default_factory=list, init=False, repr=False
    )
    _usage_data: Any = field(default=None, init=False, repr=False)
    _role: str = field(default="assistant", init=False, repr=False)

    async def process_event(self, event: Any) -> None:
        """Process a single SSE event, accumulating its delta data."""
        if not event.choices:
            if hasattr(event, "usage") and event.usage:
                self._usage_data = event.usage
            return

        delta = event.choices[0].delta

        if delta.content:
            self._accumulated_content += delta.content
            self._buffered_chunk += delta.content

            if self.on_chunk and len(self._buffered_chunk) >= STREAM_MIN_CHUNK_CHARS:
                await self.on_chunk(self._buffered_chunk)
                self._buffered_chunk = ""

        if delta.tool_calls:
            self._accumulate_tool_calls(delta.tool_calls)

        if delta.role:
            self._role = delta.role

        if event.choices[0].finish_reason:
            self._finish_reason = event.choices[0].finish_reason

        if hasattr(event, "usage") and event.usage:
            self._usage_data = event.usage

    def _accumulate_tool_calls(self, tool_call_deltas: Any) -> None:
        """Assemble tool call fragments from streaming deltas."""
        for tc_delta in tool_call_deltas:
            idx = tc_delta.index
            while len(self._tool_calls_data) <= idx:
                self._tool_calls_data.append(
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
            entry = self._tool_calls_data[idx]
            if tc_delta.id:
                entry["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    entry["function"]["name"] += tc_delta.function.name
                if tc_delta.function.arguments:
                    entry["function"]["arguments"] += tc_delta.function.arguments

    async def flush_remaining(self) -> None:
        """Flush any remaining buffered text to the on_chunk callback."""
        if self.on_chunk and self._buffered_chunk:
            await self.on_chunk(self._buffered_chunk)
            self._buffered_chunk = ""

    async def best_effort_flush(self) -> None:
        """Best-effort flush for use in finally blocks (non-throwing)."""
        if self.on_chunk and self._buffered_chunk:
            try:
                await self.on_chunk(self._buffered_chunk)
                self._buffered_chunk = ""
            except Exception:
                log_noncritical(
                    NonCriticalCategory.STREAMING,
                    "Best-effort chunk flush failed in stream finally block",
                    logger=log,
                )

    def build_completion(self) -> ChatCompletion:
        """Reconstruct a ChatCompletion from accumulated stream data."""
        from openai.types.chat import ChatCompletion as _CC
        from openai.types.chat import ChatCompletionMessage as _Msg
        from openai.types.chat.chat_completion import Choice as _Choice
        from openai.types.chat.chat_completion_message_tool_call import (
            ChatCompletionMessageToolCall as _TCToolCall,
            Function as _TCFunction,
        )

        tc_objects = [
            _TCToolCall(
                id=tc["id"],
                type=tc["type"],
                function=_TCFunction(
                    name=tc["function"]["name"],
                    arguments=tc["function"]["arguments"],
                ),
            )
            for tc in self._tool_calls_data
        ]

        message = _Msg(
            content=self._accumulated_content or None,
            role=self._role,  # type: ignore[arg-type]
            tool_calls=tc_objects or None,
            function_call=None,
        )
        choice = _Choice(
            index=0,
            message=message,
            finish_reason=self._finish_reason or "stop",
        )

        return _CC(
            id="stream",
            choices=[choice],
            created=0,
            model=self.model,
            object="chat.completion",
            usage=self._usage_data,
        )

    @property
    def usage_data(self) -> Any:
        """Return the accumulated usage data (if any)."""
        return self._usage_data

    @property
    def finish_reason(self) -> Optional[str]:
        """Return the captured finish reason."""
        return self._finish_reason
