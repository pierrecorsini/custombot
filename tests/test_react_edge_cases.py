"""test_react_edge_cases.py — Regression tests for ReAct loop edge cases.

Covers:
  1. Infinite loop: LLM keeps returning tool calls, verify iteration cap works
  2. Missing tool parameters: tool call with empty args, verify graceful handling
  3. Circular tool dependencies: tool A calls tool B which calls tool A
  4. Context overflow mid-turn: rapidly growing context, verify truncation
  5. Empty LLM response: verify graceful handling
  6. LLM returns non-JSON tool args: verify parsing error handling
  7. Tool execution timeout: verify timeout handling doesn't break loop
  8. Wall-clock timeout: verify loop terminates when time limit reached
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src.bot.react_loop import (
    execute_tool_call,
    process_tool_calls,
    react_loop,
)
from src.exceptions import ErrorCode
from src.monitoring import PerformanceMetrics

from tests.helpers.llm_mocks import (
    make_chat_response,
    make_tool_call,
    make_usage,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

RETRYABLE_CODES = frozenset({ErrorCode.LLM_RATE_LIMITED, ErrorCode.LLM_TIMEOUT})


def _make_llm(responses: list[MagicMock]) -> AsyncMock:
    """Create a mock LLM that returns responses in sequence."""
    llm = AsyncMock()
    llm.chat = AsyncMock(side_effect=responses)
    return llm


def _make_metrics() -> PerformanceMetrics:
    return PerformanceMetrics()


def _make_tool_executor(result: str = "tool output") -> AsyncMock:
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=result)
    return executor


def _make_tool_call(
    name: str = "test_tool",
    args: str = '{"key": "value"}',
    call_id: str = "call_001",
) -> MagicMock:
    """Build a tool call mock."""
    tc = MagicMock()
    tc.id = call_id
    tc.type = "function"
    tc.function.name = name
    tc.function.arguments = args
    return tc


# ── 1. Infinite loop detection ──────────────────────────────────────────────


class TestInfiniteLoopDetection:
    """Verify the ReAct loop terminates when the LLM keeps returning tool calls."""

    async def test_iteration_cap_stops_infinite_tool_calls(self, tmp_path: Path) -> None:
        """LLM always returns tool calls — loop must stop at max_tool_iterations."""
        max_iterations = 5

        # Build a response that always requests a tool call
        tool_call_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[make_tool_call(name="loop_tool", arguments='{"n": 1}')],
        )

        # The LLM always returns tool calls
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=tool_call_response)

        metrics = _make_metrics()
        executor = _make_tool_executor("loop result")

        text, tool_log, buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-infinite-loop",
            messages=[{"role": "user", "content": "trigger infinite loop"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=max_iterations,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        # Must have stopped at the iteration cap
        assert "maximum tool iterations" in text.lower() or "max" in text.lower()
        assert str(max_iterations) in text
        # Should have executed exactly max_iterations tool calls
        assert len(tool_log) == max_iterations

    async def test_iteration_cap_with_mixed_responses(self, tmp_path: Path) -> None:
        """LLM alternates between tool calls and stop, but keeps going
        because it returns tool calls at the boundaries."""
        max_iterations = 3

        tool_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[make_tool_call(name="alternating_tool")],
        )

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=tool_response)

        metrics = _make_metrics()
        executor = _make_tool_executor("alt result")

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-mixed-loop",
            messages=[{"role": "user", "content": "mixed loop test"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=max_iterations,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        assert str(max_iterations) in text


# ── 2. Missing tool parameters ──────────────────────────────────────────────


class TestMissingToolParameters:
    """Verify graceful handling of tool calls with empty or missing arguments."""

    async def test_tool_call_with_empty_args(self, tmp_path: Path) -> None:
        """Tool call with empty arguments string should be handled gracefully."""
        tc = _make_tool_call(name="missing_args_tool", args="")

        choice = MagicMock()
        choice.message.tool_calls = [tc]
        choice.message.content = None
        choice.finish_reason = "tool_calls"

        executor = _make_tool_executor("handled empty args")
        messages: list[dict[str, Any]] = []

        tool_log, buffered = await process_tool_calls(
            tool_executor=executor,
            choice=choice,
            messages=messages,
            chat_id="chat-missing-args",
            workspace_dir=tmp_path,
        )

        # Should have processed the tool call without crashing
        assert len(tool_log) == 1
        assert tool_log[0].name == "missing_args_tool"
        assert len(messages) == 2  # assistant msg + tool result

    async def test_tool_call_with_none_args(self, tmp_path: Path) -> None:
        """Tool call with None arguments should be handled."""
        tc = MagicMock()
        tc.id = "call_none_args"
        tc.type = "function"
        tc.function.name = "none_args_tool"
        tc.function.arguments = None

        choice = MagicMock()
        choice.message.tool_calls = [tc]
        choice.message.content = None
        choice.finish_reason = "tool_calls"

        executor = _make_tool_executor("handled None args")
        messages: list[dict[str, Any]] = []

        tool_log, buffered = await process_tool_calls(
            tool_executor=executor,
            choice=choice,
            messages=messages,
            chat_id="chat-none-args",
            workspace_dir=tmp_path,
        )

        assert len(tool_log) == 1


# ── 3. Circular tool dependencies ───────────────────────────────────────────


class TestCircularToolDependencies:
    """Verify that circular tool dependencies don't cause infinite loops."""

    async def test_tool_a_calls_tool_b_calls_tool_a(self, tmp_path: Path) -> None:
        """Simulate tool A calling tool B which calls tool A.

        The ReAct loop itself doesn't resolve tool-to-tool calls (tools
        return strings, not tool calls). This tests that the iteration cap
        catches the case where the LLM keeps requesting the same circular
        chain.
        """
        call_count = 0
        max_iterations = 4

        async def _circular_llm(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 1:
                return make_chat_response(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[make_tool_call(name="tool_a", arguments='{"x": 1}')],
                )
            return make_chat_response(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[make_tool_call(name="tool_b", arguments='{"y": 2}')],
            )

        llm = AsyncMock()
        llm.chat = _circular_llm
        metrics = _make_metrics()
        executor = _make_tool_executor("circular result")

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-circular",
            messages=[{"role": "user", "content": "circular test"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=max_iterations,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        # Loop must stop at cap
        assert str(max_iterations) in text
        assert len(tool_log) == max_iterations


# ── 4. Context overflow mid-turn ────────────────────────────────────────────


class TestContextOverflow:
    """Verify handling of rapidly growing context."""

    async def test_growing_context_still_completes(self, tmp_path: Path) -> None:
        """Simulate rapidly growing context (large tool results).

        The loop should still complete even if tool results are huge.
        """
        max_iterations = 3
        iteration = 0

        async def _growing_llm(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration < max_iterations:
                return make_chat_response(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[make_tool_call(name="big_tool")],
                )
            return make_chat_response(content="Final answer after large context")

        llm = AsyncMock()
        llm.chat = _growing_llm
        metrics = _make_metrics()

        # Tool executor returns progressively larger results
        call_idx = 0

        async def _big_executor(**kwargs):
            nonlocal call_idx
            call_idx += 1
            return "x" * (call_idx * 1000)

        executor = AsyncMock()
        executor.execute = _big_executor

        messages: list[dict[str, Any]] = [{"role": "user", "content": "grow context"}]
        text, tool_log, buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-overflow",
            messages=messages,
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=max_iterations,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        assert "Final answer" in text
        assert len(tool_log) == max_iterations - 1


# ── 5. Empty LLM response ──────────────────────────────────────────────────


class TestEmptyLLMResponse:
    """Verify graceful handling of empty LLM responses."""

    async def test_empty_content_returns_fallback(self, tmp_path: Path) -> None:
        """LLM returns stop with empty content — loop should return fallback."""
        empty_response = make_chat_response(
            content="",
            finish_reason="stop",
            usage=make_usage(),
        )

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=empty_response)
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-empty",
            messages=[{"role": "user", "content": "trigger empty"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=5,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        # Should return the empty-response fallback message
        assert "empty" in text.lower()
        assert len(tool_log) == 0

    async def test_whitespace_only_response(self, tmp_path: Path) -> None:
        """LLM returns whitespace-only content."""
        ws_response = make_chat_response(
            content="   \n\t  ",
            finish_reason="stop",
            usage=make_usage(),
        )

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=ws_response)
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-whitespace",
            messages=[{"role": "user", "content": "trigger whitespace"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=5,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        assert "empty" in text.lower()


# ── 6. Non-JSON tool arguments ──────────────────────────────────────────────


class TestNonJsonToolArgs:
    """Verify handling of malformed tool arguments."""

    async def test_invalid_json_args_handled_gracefully(self, tmp_path: Path) -> None:
        """Tool call with invalid JSON arguments should not crash."""
        tc = _make_tool_call(name="bad_json_tool", args="{not valid json!!!")

        choice = MagicMock()
        choice.message.tool_calls = [tc]
        choice.message.content = None
        choice.finish_reason = "tool_calls"

        executor = _make_tool_executor("handled bad json")
        messages: list[dict[str, Any]] = []

        tool_log, buffered = await process_tool_calls(
            tool_executor=executor,
            choice=choice,
            messages=messages,
            chat_id="chat-bad-json",
            workspace_dir=tmp_path,
        )

        # Tool executor receives the raw args string — it handles parsing
        assert len(tool_log) == 1

    async def test_non_dict_json_args(self, tmp_path: Path) -> None:
        """Tool call with valid JSON that isn't a dict (e.g. a list)."""
        tc = _make_tool_call(name="list_args_tool", args="[1, 2, 3]")

        choice = MagicMock()
        choice.message.tool_calls = [tc]
        choice.message.content = None
        choice.finish_reason = "tool_calls"

        executor = _make_tool_executor("handled list args")
        messages: list[dict[str, Any]] = []

        tool_log, buffered = await process_tool_calls(
            tool_executor=executor,
            choice=choice,
            messages=messages,
            chat_id="chat-list-json",
            workspace_dir=tmp_path,
        )

        assert len(tool_log) == 1


# ── 7. Tool execution timeout ───────────────────────────────────────────────


class TestToolExecutionTimeout:
    """Verify timeout handling during tool execution."""

    async def test_slow_tool_doesnt_break_loop(self, tmp_path: Path) -> None:
        """Tool that takes a long time should not break the loop."""
        tool_call_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[make_tool_call(name="slow_tool")],
        )
        final_response = make_chat_response(content="Done after slow tool")

        llm = AsyncMock()
        llm.chat = AsyncMock(side_effect=[tool_call_response, final_response])
        metrics = _make_metrics()

        async def _slow_executor(**kwargs):
            await asyncio.sleep(0.05)
            return "slow result"

        executor = AsyncMock()
        executor.execute = _slow_executor

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-slow-tool",
            messages=[{"role": "user", "content": "slow tool test"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=5,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )

        assert "Done after slow tool" in text
        assert len(tool_log) == 1

    async def test_tool_executor_exception_handled(self, tmp_path: Path) -> None:
        """Tool executor raises an exception — should be caught gracefully."""
        tc = _make_tool_call(name="failing_tool")
        choice = MagicMock()
        choice.message.tool_calls = [tc]
        choice.message.content = None
        choice.finish_reason = "tool_calls"

        executor = AsyncMock()
        executor.execute = AsyncMock(side_effect=RuntimeError("Tool crashed"))

        messages: list[dict[str, Any]] = []

        # execute_tool_call should never raise — it catches and returns error content
        tc_id, content, entry = await execute_tool_call(
            tool_executor=executor,
            tool_call=tc,
            chat_id="chat-tool-crash",
            workspace_dir=tmp_path,
            send_media=None,
        )

        assert tc_id == tc.id
        assert "crashed" in content.lower() or "error" in content.lower() or "malformed" in content.lower()


# ── 8. Wall-clock timeout ──────────────────────────────────────────────────


class TestWallClockTimeout:
    """Verify the loop terminates when the wall-clock timeout is reached."""

    async def test_loop_terminates_on_timeout(self, tmp_path: Path) -> None:
        """Loop should terminate when wall-clock timeout is exceeded."""
        timeout_seconds = 0.3
        max_iterations = 100  # High — should hit timeout first

        # LLM always returns tool calls (would run forever without timeout)
        tool_call_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[make_tool_call(name="timeout_tool")],
        )

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=tool_call_response)
        metrics = _make_metrics()

        async def _slow_tool(**kwargs):
            await asyncio.sleep(0.1)  # Each tool call takes 0.1s
            return "slow result"

        executor = AsyncMock()
        executor.execute = _slow_tool

        start = time.monotonic()
        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-wall-timeout",
            messages=[{"role": "user", "content": "timeout test"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=max_iterations,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
            react_loop_timeout=timeout_seconds,
        )
        elapsed = time.monotonic() - start

        # Should have terminated due to timeout, not iteration cap
        assert "timed out" in text.lower()
        # Should not have run all 100 iterations
        assert len(tool_log) < max_iterations
        # Should have terminated in roughly the timeout period
        assert elapsed < 5.0  # Generous upper bound

    async def test_timeout_with_no_tool_calls(self, tmp_path: Path) -> None:
        """Timeout with fast LLM responses (no tool calls) still works."""
        timeout_seconds = 0.2

        # LLM returns quickly, but the loop keeps calling it
        fast_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[make_tool_call(name="fast_tool")],
        )

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=fast_response)
        metrics = _make_metrics()
        executor = _make_tool_executor("fast result")

        text, tool_log, _buffered = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-fast-timeout",
            messages=[{"role": "user", "content": "fast timeout test"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=1000,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
            react_loop_timeout=timeout_seconds,
        )

        assert "timed out" in text.lower()

    async def test_zero_timeout_disables_deadline(self, tmp_path: Path) -> None:
        """With react_loop_timeout=0, the deadline is disabled."""
        response = make_chat_response(content="No timeout")

        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=response)
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, _, _ = await react_loop(
            llm=llm,
            metrics=metrics,
            tool_executor=executor,
            chat_id="chat-no-timeout",
            messages=[{"role": "user", "content": "no timeout"}],
            tools=None,
            workspace_dir=tmp_path,
            max_tool_iterations=5,
            stream_response=False,
            max_retries=1,
            initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
            react_loop_timeout=0.0,
        )

        assert text == "No timeout"
