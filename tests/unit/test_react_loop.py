"""
test_react_loop.py — Tests for src/bot/react_loop.py error paths.

Covers uncovered branches in:
- call_llm_with_retry: retry logic, circuit breaker, exhausted retries
- react_loop: empty response, max iterations with tool log summary
- process_tool_calls: tool-call limit rejection, TaskGroup interruption
- execute_tool_call: path traversal, malformed tool calls
- format_max_iterations_message: with/without tool log
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.react_loop import (
    call_llm_with_retry,
    execute_tool_call,
    format_max_iterations_message,
    process_tool_calls,
    react_loop,
)
from src.core.tool_formatter import ToolLogEntry
from src.exceptions import ErrorCode, LLMError
from src.monitoring import PerformanceMetrics

from tests.helpers.llm_mocks import make_chat_response, make_tool_call


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_llm(error_code: ErrorCode | None = None) -> AsyncMock:
    """Create a mock LLM that either succeeds or raises LLMError."""
    llm = AsyncMock()
    if error_code:
        llm.chat = AsyncMock(side_effect=LLMError("test", error_code=error_code))
    else:
        llm.chat = AsyncMock(
            return_value=make_chat_response(content="ok", finish_reason="stop")
        )
    return llm


def _make_metrics() -> PerformanceMetrics:
    return PerformanceMetrics()


def _make_tool_executor(result: str = "tool output") -> AsyncMock:
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=result)
    return executor


RETRYABLE_CODES = frozenset({ErrorCode.LLM_RATE_LIMITED, ErrorCode.LLM_TIMEOUT})


# ── call_llm_with_retry tests ───────────────────────────────────────────────


class TestCallLLMWithRetry:
    """Tests for call_llm_with_retry error handling branches."""

    async def test_circuit_breaker_open_returns_none(self):
        """When circuit breaker is open, returns None (unavailable message)."""
        llm = _make_llm(ErrorCode.LLM_CIRCUIT_BREAKER_OPEN)
        metrics = _make_metrics()

        result = await call_llm_with_retry(
            llm, metrics, "chat_1", [], None, None, False,
            iteration=0, max_retries=2, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert result is None

    async def test_non_retryable_error_reraises(self):
        """Non-transient errors are re-raised immediately without retry."""
        llm = _make_llm(ErrorCode.LLM_INVALID_REQUEST)
        metrics = _make_metrics()

        with pytest.raises(LLMError) as exc_info:
            await call_llm_with_retry(
                llm, metrics, "chat_1", [], None, None, False,
                iteration=0, max_retries=3, initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )
        assert exc_info.value.error_code == ErrorCode.LLM_INVALID_REQUEST

    async def test_exhausted_retries_reraises(self):
        """After max retries exhausted for a transient error, re-raises."""
        llm = _make_llm(ErrorCode.LLM_RATE_LIMITED)
        metrics = _make_metrics()

        with pytest.raises(LLMError) as exc_info:
            await call_llm_with_retry(
                llm, metrics, "chat_1", [], None, None, False,
                iteration=0, max_retries=1, initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )
        assert exc_info.value.error_code == ErrorCode.LLM_RATE_LIMITED

    async def test_retry_succeeds_after_transient(self):
        """First call fails with transient error, second succeeds."""
        response = make_chat_response(content="recovered", finish_reason="stop")
        llm = AsyncMock()
        llm.chat = AsyncMock(side_effect=[
            LLMError("rate limited", error_code=ErrorCode.LLM_RATE_LIMITED),
            response,
        ])
        metrics = _make_metrics()

        result = await call_llm_with_retry(
            llm, metrics, "chat_1", [], None, None, False,
            iteration=0, max_retries=2, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert result is not None
        assert result.choices[0].message.content == "recovered"

    async def test_streaming_mode_used_when_requested(self):
        """When use_streaming=True, llm.chat_stream is called instead of chat."""
        response = make_chat_response(content="streamed", finish_reason="stop")
        llm = AsyncMock()
        llm.chat_stream = AsyncMock(return_value=response)
        metrics = _make_metrics()
        callback = AsyncMock()

        result = await call_llm_with_retry(
            llm, metrics, "chat_1", [], None, callback, True,
            iteration=0, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert result is not None
        llm.chat_stream.assert_awaited_once()
        llm.chat.assert_not_awaited()


# ── react_loop tests ─────────────────────────────────────────────────────────


class TestReactLoopEdgeCases:
    """Tests for react_loop edge cases and error paths."""

    async def test_empty_response_returns_fallback_message(self):
        """LLM returns empty/whitespace content → fallback message."""
        llm = _make_llm()
        llm.chat = AsyncMock(
            return_value=make_chat_response(content="   ", finish_reason="stop")
        )
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert "empty response" in text.lower()
        assert tool_log == []
        assert buffered == []

    async def test_none_content_returns_fallback_message(self):
        """LLM returns None content → fallback message."""
        llm = _make_llm()
        llm.chat = AsyncMock(
            return_value=make_chat_response(content=None, finish_reason="stop")
        )
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert "empty response" in text.lower()

    async def test_length_finish_reason_with_content_returns_actual_text(self):
        """LLM returns finish_reason='length' with non-empty content → actual text + truncation warning, not empty fallback."""
        original_text = "Quantum entanglement is a phenomenon where particles become interconnected."
        llm = _make_llm()
        llm.chat = AsyncMock(
            return_value=make_chat_response(content=original_text, finish_reason="length")
        )
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        # Must contain the actual LLM response text
        assert original_text in text
        # Must contain the truncation warning
        assert "truncated" in text.lower()
        assert "length limit" in text.lower()
        # Must NOT contain the empty-response fallback
        assert "empty response" not in text.lower()
        assert tool_log == []
        assert buffered == []

    async def test_length_finish_reason_without_content_returns_warning(self):
        """LLM returns finish_reason='length' with empty content → truncation warning, not empty fallback."""
        llm = _make_llm()
        llm.chat = AsyncMock(
            return_value=make_chat_response(content="", finish_reason="length")
        )
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        # Must contain the truncation warning
        assert "truncated" in text.lower()
        assert "length limit" in text.lower()
        # Must NOT contain the empty-response fallback
        assert "empty response" not in text.lower()
        assert tool_log == []
        assert buffered == []

    async def test_max_iterations_returns_summary_with_tools(self):
        """Max iterations reached → message includes tool summary."""
        tool_call = make_tool_call(name="search", arguments='{"q":"test"}')
        tool_response = make_chat_response(
            content=None, finish_reason="tool_calls", tool_calls=[tool_call]
        )
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=tool_response)

        from src.core.serialization import serialize_tool_call_message
        from tests.helpers.llm_mocks import make_chat_response as _mcr

        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], [],
            Path("/tmp/ws"), max_tool_iterations=2,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert "maximum tool iterations" in text.lower()
        assert "search" in text  # tool name appears in summary

    async def test_circuit_breaker_mid_loop_returns_unavailable(self):
        """Circuit breaker opens mid-loop → returns unavailable message."""
        llm = AsyncMock()
        llm.chat = AsyncMock(
            side_effect=LLMError("breaker open", error_code=ErrorCode.LLM_CIRCUIT_BREAKER_OPEN)
        )
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert "temporarily unavailable" in text.lower()


# ── format_max_iterations_message tests ──────────────────────────────────────


class TestFormatMaxIterationsMessage:
    """Tests for format_max_iterations_message."""

    def test_no_tool_log(self):
        msg = format_max_iterations_message(5, [])
        assert "5" in msg
        assert "Tools used" not in msg

    def test_with_tool_log(self):
        entries = [
            ToolLogEntry(name="search", args={}, result="found"),
            ToolLogEntry(name="search", args={}, result="more"),
            ToolLogEntry(name="read", args={}, result="data"),
        ]
        msg = format_max_iterations_message(10, entries)
        assert "3 calls" in msg
        assert "search" in msg
        assert "read" in msg


# ── process_tool_calls tests ─────────────────────────────────────────────────


class TestProcessToolCalls:
    """Tests for process_tool_calls edge cases."""

    async def test_empty_tool_calls_list_returns_empty(self):
        """When tool_calls list is empty, returns empty tool_log."""
        choice = MagicMock()
        choice.message.tool_calls = []
        choice.message.content = None
        # Need serialize to work on the message
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        tool_log, buffered = await process_tool_calls(
            AsyncMock(), choice, [], "chat_1", Path("/tmp/ws"),
        )
        assert tool_log == []
        assert len(buffered) == 1  # just the assistant message

    async def test_tool_call_limit_rejection(self):
        """When tool_calls exceed MAX_TOOL_CALLS_PER_TURN, excess are rejected."""
        from src.constants import MAX_TOOL_CALLS_PER_TURN

        # Create more tool calls than the limit
        calls = [make_tool_call(call_id=f"tc_{i}", name=f"tool_{i}") for i in range(MAX_TOOL_CALLS_PER_TURN + 3)]
        choice = MagicMock()
        choice.message.tool_calls = calls
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()

        tool_log, buffered = await process_tool_calls(
            executor, choice, [], "chat_1", Path("/tmp/ws"),
        )
        # Should only execute MAX_TOOL_CALLS_PER_TURN calls
        assert len(tool_log) == MAX_TOOL_CALLS_PER_TURN
        # Rejected calls should produce tool messages in buffered
        rejected_count = 3
        # buffered = 1 assistant + MAX_TOOL_CALLS_PER_TURN results + 3 rejections
        assert len(buffered) == 1 + MAX_TOOL_CALLS_PER_TURN + rejected_count

    async def test_stream_callback_called_for_each_tool(self):
        """stream_callback is called for each tool result."""
        tool_call = make_tool_call(name="search")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        callback = AsyncMock()

        await process_tool_calls(
            executor, choice, [], "chat_1", Path("/tmp/ws"),
            stream_callback=callback,
        )
        callback.assert_awaited_once()

    async def test_channel_send_media_callback(self):
        """When channel is provided, send_media callback is created."""
        tool_call = make_tool_call(name="audio_gen")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        channel = AsyncMock()

        await process_tool_calls(
            executor, choice, [], "chat_1", Path("/tmp/ws"),
            channel=channel,
        )
        # Channel was provided — the send_media closure is built but not
        # necessarily called (it's invoked by tool_executor internally)


# ── execute_tool_call tests ─────────────────────────────────────────────────


class TestExecuteToolCall:
    """Tests for execute_tool_call error paths."""

    async def test_missing_function_name_returns_error(self):
        """Tool call with missing function name → returns malformed error message."""
        tc = MagicMock()
        tc.id = "call_bad"
        tc.function.name = ""
        tc.function.arguments = "{}"

        executor = _make_tool_executor()

        tc_id, content, entry = await execute_tool_call(
            executor, tc, "chat_1", Path("/tmp/ws"), None,
        )
        assert tc_id == "call_bad"
        assert "malformed" in content.lower()
        assert entry.name == ""

    async def test_path_traversal_blocked(self):
        """Workspace dir outside root → path traversal blocked."""
        tc = MagicMock()
        tc.id = "call_traversal"
        tc.function.name = "read_file"
        tc.function.arguments = '{"path": "/etc/passwd"}'

        executor = _make_tool_executor()

        # workspace_dir is /tmp/evil_ws, WORKSPACE_DIR is /tmp/safe_workspace
        # /tmp/evil_ws is NOT relative to /tmp/safe_workspace → blocked
        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/safe_workspace"):
            tc_id, content, entry = await execute_tool_call(
                executor, tc, "chat_1", Path("/tmp/evil_ws"), None,
            )
        assert tc_id == "call_traversal"
        assert "path validation failed" in content.lower()
        assert entry.result == "Path traversal blocked."

    async def test_invalid_json_args_handled(self):
        """Invalid JSON in arguments → args fall back to empty dict."""
        tc = MagicMock()
        tc.id = "call_json"
        tc.function.name = "search"
        tc.function.arguments = "not valid json {{{"

        executor = _make_tool_executor(result="search results")

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            tc_id, content, entry = await execute_tool_call(
                executor, tc, "chat_1", Path("/tmp/ws"), None,
            )
        assert tc_id == "call_json"
        assert content == "search results"
        assert entry.args == {}
        assert entry.name == "search"

    async def test_successful_execution(self):
        """Normal tool execution → returns result with parsed args."""
        tc = MagicMock()
        tc.id = "call_ok"
        tc.function.name = "search"
        tc.function.arguments = '{"query": "test"}'

        executor = _make_tool_executor(result="found it")

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            tc_id, content, entry = await execute_tool_call(
                executor, tc, "chat_1", Path("/tmp/ws"), None,
            )
        assert tc_id == "call_ok"
        assert content == "found it"
        assert entry.args == {"query": "test"}
        assert entry.name == "search"


# ── Coverage gap: normal react_loop return path ────────────────────────────


class TestReactLoopHappyPath:
    """Tests for the normal (non-error) return path in react_loop."""

    async def test_normal_text_response_returned(self):
        """LLM returns a normal text response → returns content, empty tool_log."""
        response = make_chat_response(content="Hello, world!", finish_reason="stop")
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=response)
        metrics = _make_metrics()
        executor = _make_tool_executor()

        text, tool_log, buffered = await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        assert text == "Hello, world!"
        assert tool_log == []
        assert buffered == []

    async def test_normal_return_tracks_metrics(self):
        """Normal return path tracks react iterations and conversation depth."""
        response = make_chat_response(content="hi", finish_reason="stop")
        llm = AsyncMock()
        llm.chat = AsyncMock(return_value=response)
        metrics = _make_metrics()
        executor = _make_tool_executor()

        await react_loop(
            llm, metrics, executor, "chat_1", [], None,
            Path("/tmp/ws"), max_tool_iterations=5,
            stream_response=False, max_retries=1, initial_delay=0.01,
            retryable_codes=RETRYABLE_CODES,
        )
        # Iteration tracking should have been recorded
        assert metrics._react_iterations_total > 0


# ── Coverage gap: send_media callback body ──────────────────────────────────


class TestSendMediaCallback:
    """Tests for the _send_media closure created in process_tool_calls."""

    async def test_send_media_audio_calls_channel_send_audio(self):
        """send_media callback routes audio files to channel.send_audio."""
        tool_call = make_tool_call(name="audio_gen")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        channel = AsyncMock()
        channel.send_audio = AsyncMock()
        channel.send_document = AsyncMock()

        # Make tool_executor call the send_media callback
        async def _execute_with_media(**kwargs):
            send_media = kwargs.get("send_media")
            if send_media:
                await send_media("audio", Path("/tmp/audio.wav"))
            return "audio sent"

        executor.execute = AsyncMock(side_effect=_execute_with_media)

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            tool_log, buffered = await process_tool_calls(
                executor, choice, [], "chat_1", Path("/tmp/ws"),
                channel=channel,
            )
        channel.send_audio.assert_awaited_once()
        channel.send_document.assert_not_awaited()

    async def test_send_media_document_calls_channel_send_document(self):
        """send_media callback routes documents to channel.send_document."""
        tool_call = make_tool_call(name="file_gen")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        channel = AsyncMock()
        channel.send_audio = AsyncMock()
        channel.send_document = AsyncMock()

        async def _execute_with_media(**kwargs):
            send_media = kwargs.get("send_media")
            if send_media:
                await send_media("document", Path("/tmp/report.pdf"), caption="Report")
            return "doc sent"

        executor.execute = AsyncMock(side_effect=_execute_with_media)

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            await process_tool_calls(
                executor, choice, [], "chat_1", Path("/tmp/ws"),
                channel=channel,
            )
        channel.send_document.assert_awaited_once()
        channel.send_audio.assert_not_awaited()

    async def test_send_media_unknown_kind_logs_warning(self):
        """send_media callback logs warning for unknown media kind."""
        tool_call = make_tool_call(name="gen")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        channel = AsyncMock()

        async def _execute_with_media(**kwargs):
            send_media = kwargs.get("send_media")
            if send_media:
                await send_media("video", Path("/tmp/clip.mp4"))
            return "done"

        executor.execute = AsyncMock(side_effect=_execute_with_media)

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            with patch("src.bot.react_loop.log") as mock_log:
                await process_tool_calls(
                    executor, choice, [], "chat_1", Path("/tmp/ws"),
                    channel=channel,
                )
                mock_log.warning.assert_any_call("Unknown media kind: %s", "video")

    async def test_send_media_exception_handled_gracefully(self):
        """send_media callback catches and logs exceptions without crashing."""
        tool_call = make_tool_call(name="gen")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = _make_tool_executor()
        channel = AsyncMock()
        channel.send_audio = AsyncMock(side_effect=RuntimeError("send failed"))

        async def _execute_with_media(**kwargs):
            send_media = kwargs.get("send_media")
            if send_media:
                await send_media("audio", Path("/tmp/a.wav"))
            return "ok"

        executor.execute = AsyncMock(side_effect=_execute_with_media)

        with patch("src.bot.react_loop.WORKSPACE_DIR", "/tmp/ws"):
            # Should NOT raise — exception is caught inside _send_media
            tool_log, buffered = await process_tool_calls(
                executor, choice, [], "chat_1", Path("/tmp/ws"),
                channel=channel,
            )
        assert len(tool_log) == 1  # tool still completed


# ── Coverage gap: TaskGroup exception salvage & tool result truncation ──────


class TestProcessToolCallsAdvanced:
    """Tests for TaskGroup exception handling and tool result truncation."""

    async def test_taskgroup_exception_salvages_on_runtime_error(self):
        """When a tool execution raises RuntimeError, the TaskGroup
        except BaseException path salvages results from completed tasks.

        Exercises lines 322-335: the salvage loop that iterates done tasks,
        calls t.result(), and silently skips failed ones via the inner
        ``except BaseException: pass``.
        """
        import tempfile
        ws_root = Path(tempfile.gettempdir()) / "custombot_test_tg_salvage"
        ws_root.mkdir(parents=True, exist_ok=True)
        ws_dir = ws_root / "workspace"

        tc1 = make_tool_call(call_id="tc_1", name="good_tool")
        tc2 = make_tool_call(call_id="tc_2", name="bad_tool")
        choice = MagicMock()
        choice.message.tool_calls = [tc1, tc2]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = AsyncMock()

        # Use a gate to ensure the good tool completes before the bad
        # tool starts failing.  This makes the test deterministic
        # regardless of TaskGroup scheduling order.
        good_done = asyncio.Event()

        async def _execute_by_name(**kwargs):
            tool_call = kwargs.get("tool_call")
            name = getattr(getattr(tool_call, "function", None), "name", "")
            if name == "good_tool":
                result = "good result"
                good_done.set()
                return result
            else:
                # Wait until the good tool has completed before raising.
                await asyncio.wait_for(good_done.wait(), timeout=2.0)
                raise RuntimeError("Tool execution exploded")

        executor.execute = AsyncMock(side_effect=_execute_by_name)

        # process_tool_calls should NOT raise — the salvage path catches
        # the BaseException from the TaskGroup and salvages partial results.
        with patch("src.bot.react_loop.WORKSPACE_DIR", str(ws_root)):
            tool_log, buffered = await process_tool_calls(
                executor, choice, [], "chat_1", ws_dir,
            )

        # At least the first tool's result should be salvaged
        assert len(tool_log) >= 1
        assert tool_log[0].name == "good_tool"
        assert tool_log[0].result == "good result"

    async def test_taskgroup_exception_salvages_with_partial_failure(self):
        """When a task raises during salvage, it is silently skipped.

        Exercises the inner ``except BaseException: pass`` (line 334-335)
        inside the salvage loop by having ALL tasks fail with RuntimeError.
        """
        import tempfile
        ws_root = Path(tempfile.gettempdir()) / "custombot_test_tg_allfail"
        ws_root.mkdir(parents=True, exist_ok=True)
        ws_dir = ws_root / "workspace"

        tc1 = make_tool_call(call_id="tc_1", name="fail_a")
        tc2 = make_tool_call(call_id="tc_2", name="fail_b")
        choice = MagicMock()
        choice.message.tool_calls = [tc1, tc2]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        executor = AsyncMock()

        async def _execute_fail(**kwargs):
            raise RuntimeError("All tools fail")

        executor.execute = AsyncMock(side_effect=_execute_fail)

        # Should NOT raise — salvage path catches the ExceptionGroup
        with patch("src.bot.react_loop.WORKSPACE_DIR", str(ws_root)):
            tool_log, buffered = await process_tool_calls(
                executor, choice, [], "chat_1", ws_dir,
            )

        # Both tasks failed during execution, so no results salvaged
        # (the salvage loop catches BaseException from t.result())
        assert len(tool_log) == 0
        # Only the assistant message in buffered
        assert len(buffered) == 1

    async def test_tool_result_truncated_for_large_content(self):
        """Large tool results are truncated in buffered_persist."""
        from src.constants import MAX_TOOL_RESULT_PERSIST_LENGTH

        tool_call = make_tool_call(name="big_reader")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None
        choice.message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": []
        })

        large_result = "x" * (MAX_TOOL_RESULT_PERSIST_LENGTH + 500)
        executor = _make_tool_executor(result=large_result)

        # Use a workspace_dir that resolves inside WORKSPACE_DIR to pass
        # the path-traversal check.  Patch WORKSPACE_DIR to a temp-like root
        # and set workspace_dir as a child of that root.
        import tempfile
        ws_root = Path(tempfile.gettempdir()) / "custombot_test_truncation"
        ws_root.mkdir(parents=True, exist_ok=True)
        ws_dir = ws_root / "workspace"

        with patch("src.bot.react_loop.WORKSPACE_DIR", str(ws_root)):
            tool_log, buffered = await process_tool_calls(
                executor, choice, [], "chat_1", ws_dir,
            )

        # Find the tool persist entry (not the assistant message)
        tool_persist = [b for b in buffered if b.get("role") == "tool"]
        assert len(tool_persist) == 1
        # Should be truncated
        assert len(tool_persist[0]["content"]) < len(large_result)
        assert "truncated" in tool_persist[0]["content"]
