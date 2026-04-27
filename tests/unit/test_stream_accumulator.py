"""
Tests for src/core/stream_accumulator.py — StreamAccumulator.

Unit tests covering:
  - Text content accumulation and buffered chunk flushing
  - Tool call fragment assembly across multiple deltas
  - Role, finish_reason, and usage capture
  - ChatCompletion reconstruction
  - Edge cases: empty stream, no usage, best-effort flush
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.constants import STREAM_MIN_CHUNK_CHARS
from src.core.stream_accumulator import StreamAccumulator


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_event(
    *,
    content: str | None = None,
    tool_calls: list | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
    usage: object | None = None,
    has_choices: bool = True,
) -> MagicMock:
    """Build a mock SSE stream event."""
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls
    delta.role = role

    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = finish_reason

    event = MagicMock()
    event.choices = [choice] if has_choices else []
    event.usage = usage
    return event


def _make_tc_delta(
    index: int = 0,
    tc_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> MagicMock:
    """Build a mock tool call delta."""
    func = MagicMock()
    func.name = name
    func.arguments = arguments
    tc = MagicMock()
    tc.index = index
    tc.id = tc_id
    tc.function = func
    return tc


def _make_usage(
    prompt_tokens: int = 50,
    completion_tokens: int = 100,
) -> dict:
    """Build a usage dict compatible with ChatCompletion construction."""
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Text content accumulation
# ─────────────────────────────────────────────────────────────────────────────


class TestTextAccumulation:
    """Tests for text content accumulation."""

    async def test_accumulates_single_text_event(self):
        acc = StreamAccumulator(model="gpt-4o")
        event = _make_event(content="Hello world")
        await acc.process_event(event)
        assert acc._accumulated_content == "Hello world"

    async def test_accumulates_multiple_text_events(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content="Hello "))
        await acc.process_event(_make_event(content="world!"))
        assert acc._accumulated_content == "Hello world!"

    async def test_none_content_is_ignored(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content=None))
        assert acc._accumulated_content == ""


class TestBufferedChunkFlushing:
    """Tests for on_chunk callback with buffered flushing."""

    async def test_flushes_when_exceeding_threshold(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        long_text = "x" * STREAM_MIN_CHUNK_CHARS
        await acc.process_event(_make_event(content=long_text))
        assert len(chunks) == 1
        assert chunks[0] == long_text

    async def test_does_not_flush_below_threshold(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        short_text = "x" * (STREAM_MIN_CHUNK_CHARS - 1)
        await acc.process_event(_make_event(content=short_text))
        assert len(chunks) == 0

    async def test_accumulates_across_events_until_threshold(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        half = "x" * (STREAM_MIN_CHUNK_CHARS // 2)
        await acc.process_event(_make_event(content=half))
        assert len(chunks) == 0
        await acc.process_event(_make_event(content=half))
        assert len(chunks) == 1

    async def test_no_callback_when_on_chunk_is_none(self):
        acc = StreamAccumulator(model="gpt-4o", on_chunk=None)
        long_text = "x" * (STREAM_MIN_CHUNK_CHARS + 10)
        await acc.process_event(_make_event(content=long_text))
        # Should not raise, content is accumulated silently
        assert acc._accumulated_content == long_text


# ─────────────────────────────────────────────────────────────────────────────
# Tool call accumulation
# ─────────────────────────────────────────────────────────────────────────────


class TestToolCallAccumulation:
    """Tests for tool call fragment assembly."""

    async def test_single_tool_call_in_one_delta(self):
        acc = StreamAccumulator(model="gpt-4o")
        tc = _make_tc_delta(index=0, tc_id="call_1", name="get_weather", arguments='{"city":"Paris"}')
        await acc.process_event(_make_event(tool_calls=[tc]))
        assert len(acc._tool_calls_data) == 1
        assert acc._tool_calls_data[0]["id"] == "call_1"
        assert acc._tool_calls_data[0]["function"]["name"] == "get_weather"
        assert acc._tool_calls_data[0]["function"]["arguments"] == '{"city":"Paris"}'

    async def test_tool_call_fragments_assemble_across_events(self):
        acc = StreamAccumulator(model="gpt-4o")
        tc1 = _make_tc_delta(index=0, tc_id="call_1", name="get_", arguments='{"ci')
        tc2 = _make_tc_delta(index=0, name=None, arguments='ty":"Paris"}')
        await acc.process_event(_make_event(tool_calls=[tc1]))
        await acc.process_event(_make_event(tool_calls=[tc2]))
        assert acc._tool_calls_data[0]["id"] == "call_1"
        assert acc._tool_calls_data[0]["function"]["name"] == "get_"
        assert acc._tool_calls_data[0]["function"]["arguments"] == '{"city":"Paris"}'

    async def test_multiple_tool_calls_via_index(self):
        acc = StreamAccumulator(model="gpt-4o")
        tc1 = _make_tc_delta(index=0, tc_id="call_a", name="func_a", arguments='{"x":1}')
        tc2 = _make_tc_delta(index=1, tc_id="call_b", name="func_b", arguments='{"y":2}')
        await acc.process_event(_make_event(tool_calls=[tc1, tc2]))
        assert len(acc._tool_calls_data) == 2
        assert acc._tool_calls_data[0]["function"]["name"] == "func_a"
        assert acc._tool_calls_data[1]["function"]["name"] == "func_b"

    async def test_out_of_order_index_extends_list(self):
        acc = StreamAccumulator(model="gpt-4o")
        tc = _make_tc_delta(index=2, tc_id="call_2", name="func", arguments="{}")
        await acc.process_event(_make_event(tool_calls=[tc]))
        assert len(acc._tool_calls_data) == 3
        assert acc._tool_calls_data[2]["id"] == "call_2"
        # Empty placeholders for index 0 and 1
        assert acc._tool_calls_data[0]["id"] == ""
        assert acc._tool_calls_data[1]["id"] == ""


# ─────────────────────────────────────────────────────────────────────────────
# Role, finish_reason, usage capture
# ─────────────────────────────────────────────────────────────────────────────


class TestMetadataCapture:
    """Tests for role, finish_reason, and usage capture."""

    async def test_captures_role(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(role="assistant"))
        assert acc._role == "assistant"

    async def test_default_role_is_assistant(self):
        acc = StreamAccumulator(model="gpt-4o")
        assert acc._role == "assistant"

    async def test_captures_finish_reason(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(finish_reason="stop"))
        assert acc.finish_reason == "stop"

    async def test_captures_usage_from_choices_event(self):
        acc = StreamAccumulator(model="gpt-4o")
        usage = _make_usage(prompt_tokens=10, completion_tokens=20)
        await acc.process_event(_make_event(usage=usage))
        assert acc.usage_data is usage

    async def test_captures_usage_from_no_choices_event(self):
        acc = StreamAccumulator(model="gpt-4o")
        usage = _make_usage(prompt_tokens=10, completion_tokens=20)
        await acc.process_event(_make_event(usage=usage, has_choices=False))
        assert acc.usage_data is usage

    async def test_no_choices_event_without_usage_is_noop(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(usage=None, has_choices=False))
        assert acc.usage_data is None

    async def test_later_usage_overwrites_earlier(self):
        acc = StreamAccumulator(model="gpt-4o")
        usage1 = _make_usage(prompt_tokens=10, completion_tokens=20)
        usage2 = _make_usage(prompt_tokens=50, completion_tokens=100)
        await acc.process_event(_make_event(usage=usage1))
        await acc.process_event(_make_event(usage=usage2))
        assert acc.usage_data is usage2


# ─────────────────────────────────────────────────────────────────────────────
# ChatCompletion reconstruction
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildCompletion:
    """Tests for build_completion() reconstruction."""

    async def test_text_only_completion(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content="Hello world", finish_reason="stop"))
        completion = acc.build_completion()

        assert completion.id == "stream"
        assert completion.model == "gpt-4o"
        assert completion.object == "chat.completion"
        assert len(completion.choices) == 1
        assert completion.choices[0].message.content == "Hello world"
        assert completion.choices[0].finish_reason == "stop"

    async def test_default_finish_reason_is_stop(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content="Hi"))
        completion = acc.build_completion()
        assert completion.choices[0].finish_reason == "stop"

    async def test_content_none_when_empty(self):
        acc = StreamAccumulator(model="gpt-4o")
        # No content events → accumulated_content is "" → converted to None
        completion = acc.build_completion()
        assert completion.choices[0].message.content is None

    async def test_tool_calls_in_completion(self):
        acc = StreamAccumulator(model="gpt-4o")
        tc = _make_tc_delta(index=0, tc_id="call_1", name="func", arguments='{"x":1}')
        await acc.process_event(_make_event(tool_calls=[tc]))
        completion = acc.build_completion()

        tool_calls = completion.choices[0].message.tool_calls
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "call_1"
        assert tool_calls[0].function.name == "func"
        assert tool_calls[0].function.arguments == '{"x":1}'

    async def test_no_tool_calls_when_none_accumulated(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content="Just text"))
        completion = acc.build_completion()
        assert completion.choices[0].message.tool_calls is None

    async def test_usage_included_in_completion(self):
        acc = StreamAccumulator(model="gpt-4o")
        usage = _make_usage(prompt_tokens=10, completion_tokens=20)
        await acc.process_event(_make_event(content="Hi", usage=usage))
        completion = acc.build_completion()
        assert completion.usage is not None
        assert completion.usage.prompt_tokens == 10
        assert completion.usage.completion_tokens == 20

    async def test_usage_none_when_not_provided(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(content="Hi"))
        completion = acc.build_completion()
        assert completion.usage is None


# ─────────────────────────────────────────────────────────────────────────────
# flush_remaining and best_effort_flush
# ─────────────────────────────────────────────────────────────────────────────


class TestFlushMethods:
    """Tests for flush_remaining() and best_effort_flush()."""

    async def test_flush_remaining_sends_buffered_text(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        await acc.process_event(_make_event(content="short"))
        await acc.flush_remaining()

        assert len(chunks) == 1
        assert chunks[0] == "short"

    async def test_flush_remaining_clears_buffer(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        await acc.process_event(_make_event(content="text"))
        await acc.flush_remaining()
        # Second flush should not send anything
        await acc.flush_remaining()
        assert len(chunks) == 1

    async def test_flush_remaining_noop_when_no_callback(self):
        acc = StreamAccumulator(model="gpt-4o", on_chunk=None)
        await acc.process_event(_make_event(content="text"))
        # Should not raise
        await acc.flush_remaining()

    async def test_flush_remaining_noop_when_buffer_empty(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        await acc.flush_remaining()
        assert len(chunks) == 0

    async def test_best_effort_flush_sends_buffered_text(self):
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o", on_chunk=on_chunk)
        await acc.process_event(_make_event(content="remaining"))
        await acc.best_effort_flush()
        assert len(chunks) == 1
        assert chunks[0] == "remaining"

    async def test_best_effort_flush_swallows_callback_error(self):
        async def failing_on_chunk(text: str) -> None:
            raise RuntimeError("callback exploded")

        acc = StreamAccumulator(model="gpt-4o", on_chunk=failing_on_chunk)
        await acc.process_event(_make_event(content="data"))
        # Should not raise
        with patch("src.core.stream_accumulator.log_noncritical"):
            await acc.best_effort_flush()

    async def test_best_effort_flush_noop_when_no_callback(self):
        acc = StreamAccumulator(model="gpt-4o", on_chunk=None)
        await acc.process_event(_make_event(content="text"))
        await acc.best_effort_flush()


# ─────────────────────────────────────────────────────────────────────────────
# Properties
# ─────────────────────────────────────────────────────────────────────────────


class TestProperties:
    """Tests for usage_data and finish_reason properties."""

    async def test_usage_data_initially_none(self):
        acc = StreamAccumulator(model="gpt-4o")
        assert acc.usage_data is None

    async def test_finish_reason_initially_none(self):
        acc = StreamAccumulator(model="gpt-4o")
        assert acc.finish_reason is None

    async def test_finish_reason_returns_last_value(self):
        acc = StreamAccumulator(model="gpt-4o")
        await acc.process_event(_make_event(finish_reason="tool_calls"))
        # In a real stream, the last event typically carries the final reason
        await acc.process_event(_make_event(finish_reason="stop"))
        assert acc.finish_reason == "stop"


# ─────────────────────────────────────────────────────────────────────────────
# Full stream simulation (integration-style)
# ─────────────────────────────────────────────────────────────────────────────


class TestFullStreamSimulation:
    """End-to-end tests simulating a complete stream cycle."""

    async def test_text_stream_with_usage(self):
        """Simulate a complete text stream: content → finish_reason → usage."""
        chunks: list[str] = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        acc = StreamAccumulator(model="gpt-4o-mini", on_chunk=on_chunk)

        # Multiple content events
        for _ in range(3):
            await acc.process_event(_make_event(content="x" * (STREAM_MIN_CHUNK_CHARS + 1)))

        # Final event with finish_reason
        await acc.process_event(_make_event(content="end", finish_reason="stop"))

        # Usage-only event (no choices)
        usage = _make_usage(prompt_tokens=100, completion_tokens=200)
        await acc.process_event(_make_event(usage=usage, has_choices=False))

        await acc.flush_remaining()
        completion = acc.build_completion()

        assert completion.choices[0].message.content.endswith("end")
        assert completion.choices[0].finish_reason == "stop"
        assert completion.usage is not None
        assert completion.usage.prompt_tokens == 100
        assert completion.usage.completion_tokens == 200
        assert len(chunks) >= 3  # At least 3 threshold-crossing flushes

    async def test_tool_call_stream(self):
        """Simulate a stream with tool call fragments."""
        acc = StreamAccumulator(model="gpt-4o")

        # Tool call arrives in fragments
        await acc.process_event(_make_event(
            tool_calls=[_make_tc_delta(index=0, tc_id="call_1", name="search", arguments=None)],
        ))
        await acc.process_event(_make_event(
            tool_calls=[_make_tc_delta(index=0, name=None, arguments='{"qu')],
        ))
        await acc.process_event(_make_event(
            tool_calls=[_make_tc_delta(index=0, name=None, arguments='ery": "test"}')],
            finish_reason="tool_calls",
        ))

        completion = acc.build_completion()
        tc = completion.choices[0].message.tool_calls[0]
        assert tc.id == "call_1"
        assert tc.function.name == "search"
        assert tc.function.arguments == '{"query": "test"}'
        assert completion.choices[0].finish_reason == "tool_calls"

    async def test_mixed_text_and_tool_calls(self):
        """Simulate a stream with both text content and tool calls."""
        acc = StreamAccumulator(model="gpt-4o")

        await acc.process_event(_make_event(content="Let me search for "))
        await acc.process_event(_make_event(
            content="that.",
            tool_calls=[_make_tc_delta(index=0, tc_id="call_1", name="search", arguments='{}')],
            finish_reason="tool_calls",
        ))

        completion = acc.build_completion()
        assert completion.choices[0].message.content == "Let me search for that."
        assert completion.choices[0].message.tool_calls is not None
        assert completion.choices[0].finish_reason == "tool_calls"
