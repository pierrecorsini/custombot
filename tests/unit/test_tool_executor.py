"""
Tests for src/core/tool_executor.py — ToolExecutor and format_skill_error.

Covers:
- execute() with unknown skill (returns error)
- execute() with invalid JSON args (returns error)
- execute() with rate limit exceeded (returns rate limit message)
- execute() with a mock skill that succeeds
- execute() with a skill that times out
- execute() with a skill that raises an exception
- format_skill_error() helper
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.tool_executor import (
    MAX_ARGS_BYTES,
    MAX_ARGS_DEPTH,
    _MAX_ERROR_FIELD_LEN,
    _MAX_ERROR_RESPONSE_LEN,
    ToolExecutor,
    _measured_depth,
    _sanitize_error_type,
    format_skill_error,
)
from src.exceptions import SkillError
from src.rate_limiter import RateLimitResult
from src.utils.circuit_breaker import CircuitState
from tests.helpers.llm_mocks import make_tool_call


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_skill_registry(skill_map: dict | None = None) -> MagicMock:
    """Create a mock SkillRegistry.

    Args:
        skill_map: dict mapping skill_name -> mock skill object.
            Each mock must have an async ``execute`` method.
    """
    registry = MagicMock()
    _map = skill_map or {}

    def _get(name: str):
        return _map.get(name)

    registry.get = MagicMock(side_effect=_get)
    return registry


def _make_rate_limiter(allowed: bool = True) -> MagicMock:
    """Create a mock RateLimiter that returns a canned RateLimitResult."""
    limiter = MagicMock()
    limiter.check_rate_limit = MagicMock(
        return_value=RateLimitResult(
            allowed=allowed,
            remaining=5 if allowed else 0,
            reset_at=0.0,
            retry_after=0.0 if allowed else 30.0,
            limit_type="chat",
            limit_value=30,
        )
    )
    return limiter


# ─────────────────────────────────────────────────────────────────────────────
# Test format_skill_error
# ─────────────────────────────────────────────────────────────────────────────


class TestFormatSkillError:
    """Tests for the format_skill_error() helper function."""

    @patch("src.core.tool_executor.get_correlation_id", return_value="corr-123")
    def test_includes_user_message(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="test_skill",
            error_type="TimeoutError",
            user_message="It timed out.",
        )
        assert "⚠️ It timed out." in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="corr-123")
    def test_includes_suggestion_for_known_error(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="read_file",
            error_type="TimeoutError",
            user_message="Timed out.",
        )
        assert "💡" in result
        assert "simpler request" in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="corr-123")
    def test_no_suggestion_for_unknown_error(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="read_file",
            error_type="SomeWeirdError",
            user_message="Oops.",
        )
        assert "💡" not in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="corr-abc")
    def test_includes_skill_name_error_type_and_corr_id(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="my_skill",
            error_type="PermissionError",
            user_message="No access.",
        )
        assert "skill: my_skill" in result
        assert "error: permission_denied" in result
        assert "ref: corr-abc" in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="")
    def test_no_ref_when_empty_correlation_id(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="s",
            error_type="E",
            user_message="m",
        )
        assert "ref:" not in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="")
    def test_truncates_long_skill_name(self, _mock_corr: MagicMock) -> None:
        long_name = "x" * 500
        result = format_skill_error(
            skill_name=long_name,
            error_type="E",
            user_message="m",
        )
        assert len(result) <= _MAX_ERROR_RESPONSE_LEN
        # Skill name should be truncated to _MAX_ERROR_FIELD_LEN + ellipsis
        assert f"skill: {'x' * (_MAX_ERROR_FIELD_LEN - 1)}…" in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="c" * 500)
    def test_truncates_long_correlation_id(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="s",
            error_type="E",
            user_message="m",
        )
        assert len(result) <= _MAX_ERROR_RESPONSE_LEN

    @patch("src.core.tool_executor.get_correlation_id", return_value="")
    def test_truncates_long_user_message(self, _mock_corr: MagicMock) -> None:
        long_msg = "y" * 500
        result = format_skill_error(
            skill_name="s",
            error_type="E",
            user_message=long_msg,
        )
        assert len(result) <= _MAX_ERROR_RESPONSE_LEN

    @patch("src.core.tool_executor.get_correlation_id", return_value="c" * 500)
    def test_total_response_capped_at_max(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="s" * 500,
            error_type="E" * 500,
            user_message="m" * 500,
        )
        assert len(result) <= _MAX_ERROR_RESPONSE_LEN


# ─────────────────────────────────────────────────────────────────────────────
# Test error type sanitization (security — no information leakage)
# ─────────────────────────────────────────────────────────────────────────────


class TestSanitizeErrorType:
    """Verify _sanitize_error_type() never exposes Python internals to users."""

    def test_known_python_exceptions_are_sanitized(self) -> None:
        for raw in ("ValueError", "RuntimeError", "OSError", "TypeError", "KeyError"):
            display = _sanitize_error_type(raw)
            assert display == "internal_error", f"{raw} should map to internal_error"

    def test_known_domain_errors_get_safe_labels(self) -> None:
        assert _sanitize_error_type("TimeoutError") == "timeout"
        assert _sanitize_error_type("UnknownSkill") == "unknown_tool"
        assert _sanitize_error_type("CircuitBreakerOpen") == "service_unavailable"

    def test_unknown_error_type_returns_generic(self) -> None:
        assert _sanitize_error_type("SomeThirdPartyExc") == "internal_error"

    @patch("src.core.tool_executor.get_correlation_id", return_value="")
    def test_format_skill_error_hides_python_exception_names(self, _mock: MagicMock) -> None:
        """Raw Python exception class names must NEVER appear in user output."""
        for raw_type in ("ValueError", "RuntimeError", "OSError", "KeyError", "TypeError"):
            result = format_skill_error(
                skill_name="s",
                error_type=raw_type,
                user_message="Something went wrong.",
            )
            assert raw_type not in result, (
                f"Raw Python exception name {raw_type!r} leaked to user output"
            )
            assert "internal_error" in result
# Test ToolExecutor.execute
# ─────────────────────────────────────────────────────────────────────────────


class TestToolExecutorUnknownSkill:
    """execute() returns an error for unknown skills."""

    async def test_unknown_skill_returns_error(self) -> None:
        registry = _make_skill_registry(skill_map={})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="nonexistent")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "not available" in result
        assert "unknown_tool" in result


class TestToolExecutorInvalidArgs:
    """execute() returns an error when arguments are not valid JSON."""

    async def test_invalid_json_args_returns_error(self) -> None:
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="any_skill", arguments="{bad json!!")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "couldn't understand the arguments" in result
        assert "invalid_arguments" in result


class TestToolExecutorDeeplyNestedArgs:
    """execute() rejects arguments that exceed the max nesting depth."""

    async def test_deeply_nested_args_returns_error(self) -> None:
        # Build a dict nested deeper than MAX_ARGS_DEPTH
        nested: dict = {}
        current = nested
        for _ in range(MAX_ARGS_DEPTH + 1):
            current["x"] = {}
            current = current["x"]
        current["leaf"] = 1

        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="any_skill", arguments=json.dumps(nested))

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "too deeply nested" in result
        assert "invalid_arguments" in result

    async def test_args_at_exact_max_depth_passes(self) -> None:
        # Build a dict nested exactly at MAX_ARGS_DEPTH (boundary)
        nested: dict = {}
        current = nested
        for _ in range(MAX_ARGS_DEPTH - 1):
            current["x"] = {}
            current = current["x"]
        current["leaf"] = 1

        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok")
        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments=json.dumps(nested))

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "ok"

    async def test_deeply_nested_list_args_returns_error(self) -> None:
        # Build a list nested deeper than MAX_ARGS_DEPTH
        nested: list = []
        current = nested
        for _ in range(MAX_ARGS_DEPTH + 1):
            inner: list = []
            current.append(inner)
            current = inner
        current.append("leaf")

        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="any_skill", arguments=json.dumps(nested))

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "too deeply nested" in result


class TestToolExecutorOversizedArgs:
    """execute() rejects argument payloads exceeding MAX_ARGS_BYTES."""

    async def test_oversized_args_returns_error(self) -> None:
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="any_skill", arguments="x" * (MAX_ARGS_BYTES + 1))

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "too large" in result
        assert "invalid_arguments" in result

    async def test_args_at_exact_max_size_passes(self) -> None:
        """Payload exactly at the limit is still valid JSON → executes."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok")

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        # Build valid JSON that is exactly MAX_ARGS_BYTES long
        padding_needed = MAX_ARGS_BYTES - len('{"k":"') - len('"}')
        args_json = '{"k":"' + "a" * padding_needed + '"}'
        assert len(args_json) == MAX_ARGS_BYTES

        tc = make_tool_call(name="s", arguments=args_json)
        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "ok"

    async def test_oversized_args_tracks_metric(self) -> None:
        """Oversized args rejection calls metrics.track_skill_args_oversized."""
        metrics = MagicMock()
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry, metrics=metrics)
        oversized_args = "x" * (MAX_ARGS_BYTES + 1)
        tc = make_tool_call(name="my_skill", arguments=oversized_args)

        await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        metrics.track_skill_args_oversized.assert_called_once_with("my_skill", len(oversized_args))

    async def test_oversized_args_no_metrics_when_none(self) -> None:
        """No error when metrics is None (default)."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry, metrics=None)
        tc = make_tool_call(name="any_skill", arguments="x" * (MAX_ARGS_BYTES + 1))

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "too large" in result


class TestToolExecutorRateLimit:
    """execute() respects rate limit results."""

    async def test_rate_limit_exceeded_returns_message(self) -> None:
        # Must register the skill so it passes the "unknown skill" check
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="should not run")

        registry = _make_skill_registry(skill_map={"web_search": skill})
        limiter = _make_rate_limiter(allowed=False)
        # The rate limit message from RateLimitResult
        rate_msg = limiter.check_rate_limit.return_value.message
        executor = ToolExecutor(skills_registry=registry, rate_limiter=limiter)
        tc = make_tool_call(name="web_search", arguments='{"q": "test"}')

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        # Should return the rate limit message directly
        assert result == rate_msg

    async def test_no_rate_limiter_skips_check(self) -> None:
        """When rate_limiter is None, no rate check is performed."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok result")

        registry = _make_skill_registry(skill_map={"my_skill": skill})
        executor = ToolExecutor(skills_registry=registry, rate_limiter=None)
        tc = make_tool_call(name="my_skill", arguments='{"a": 1}')

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "ok result" in result


class TestToolExecutorSuccess:
    """execute() returns the skill result on success."""

    async def test_successful_execution(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="The answer is 42")

        registry = _make_skill_registry(skill_map={"calc": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="calc", arguments='{"expr": "6*7"}')

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "The answer is 42"
        skill.execute.assert_awaited_once()

    async def test_passes_workspace_and_args(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="done")

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments='{"key": "val"}')

        ws = Path("/tmp/my_workspace")
        await executor.execute(chat_id="c", tool_call=tc, workspace_dir=ws)

        skill.execute.assert_awaited_once_with(
            workspace_dir=ws,
            key="val",
        )

    async def test_injects_send_media_callback(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="sent")

        registry = _make_skill_registry(skill_map={"audio_skill": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="audio_skill", arguments="{}")

        send_media = AsyncMock()
        await executor.execute(
            chat_id="c",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
            send_media=send_media,
        )

        skill.execute.assert_awaited_once()
        call_kwargs = skill.execute.call_args
        assert call_kwargs.kwargs.get("send_media") is send_media


class TestToolExecutorTimeout:
    """execute() handles skill timeouts gracefully."""

    async def test_timeout_returns_error(self) -> None:
        skill = AsyncMock()

        async def _hang(**kwargs):
            await asyncio.sleep(999)

        skill.execute = AsyncMock(side_effect=_hang)

        registry = _make_skill_registry(skill_map={"slow": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="slow", arguments="{}")

        # Patch DEFAULT_SKILL_TIMEOUT to a very small value so test runs fast
        with patch("src.core.tool_executor.DEFAULT_SKILL_TIMEOUT", 0.1):
            result = await executor.execute(
                chat_id="chat_1",
                tool_call=tc,
                workspace_dir=Path("/tmp/ws"),
            )

        assert "too long" in result
        assert "timeout" in result

    async def test_per_skill_timeout_override_allows_longer_execution(self) -> None:
        """Skill with timeout_seconds=2.0 succeeds despite DEFAULT_SKILL_TIMEOUT=0.1."""
        skill = AsyncMock()
        skill.timeout_seconds = 2.0

        async def _slow_but_within_limit(**kwargs):
            await asyncio.sleep(0.3)
            return "slow result"

        skill.execute = AsyncMock(side_effect=_slow_but_within_limit)

        registry = _make_skill_registry(skill_map={"patient_skill": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="patient_skill", arguments="{}")

        with patch("src.core.tool_executor.DEFAULT_SKILL_TIMEOUT", 0.1):
            result = await executor.execute(
                chat_id="chat_1",
                tool_call=tc,
                workspace_dir=Path("/tmp/ws"),
            )

        assert result == "slow result"

    async def test_per_skill_timeout_exceeded_returns_error(self) -> None:
        """Skill with timeout_seconds=0.2 times out even when default is much larger."""
        skill = AsyncMock()
        skill.timeout_seconds = 0.2

        async def _hang(**kwargs):
            await asyncio.sleep(999)

        skill.execute = AsyncMock(side_effect=_hang)

        registry = _make_skill_registry(skill_map={"quick_timeout": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="quick_timeout", arguments="{}")

        with patch("src.core.tool_executor.DEFAULT_SKILL_TIMEOUT", 60.0):
            result = await executor.execute(
                chat_id="chat_1",
                tool_call=tc,
                workspace_dir=Path("/tmp/ws"),
            )

        assert "too long" in result
        assert "timeout" in result
        assert "timeout: 0.2s" in result

    async def test_non_numeric_timeout_seconds_falls_back_to_default(self) -> None:
        """Skill with non-numeric timeout_seconds falls back to DEFAULT_SKILL_TIMEOUT."""
        skill = AsyncMock()
        skill.timeout_seconds = "not_a_number"

        async def _hang(**kwargs):
            await asyncio.sleep(999)

        skill.execute = AsyncMock(side_effect=_hang)

        registry = _make_skill_registry(skill_map={"bad_timeout": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="bad_timeout", arguments="{}")

        with patch("src.core.tool_executor.DEFAULT_SKILL_TIMEOUT", 0.1):
            result = await executor.execute(
                chat_id="chat_1",
                tool_call=tc,
                workspace_dir=Path("/tmp/ws"),
            )

        assert "too long" in result
        assert "timeout" in result


class TestToolExecutorExceptions:
    """execute() catches various exceptions from skills."""

    async def test_skill_error_returns_formatted_message(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=SkillError("disk full", reason="DiskSpaceError"))

        registry = _make_skill_registry(skill_map={"write_file": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="write_file", arguments='{"path": "/x"}')

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        # Should contain the error from SkillError details (sanitized)
        assert "storage_error" in result

    async def test_generic_exception_returns_unexpected_error(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=RuntimeError("boom"))

        registry = _make_skill_registry(skill_map={"crashy": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="crashy", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "unexpected error" in result
        assert "internal_error" in result

    async def test_metrics_tracked_on_success(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok")

        metrics = MagicMock()
        metrics.track_skill_time = MagicMock()

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry, metrics=metrics)
        tc = make_tool_call(name="s", arguments="{}")

        await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        metrics.track_skill_time.assert_called_once()

    async def test_none_result_returns_empty_string(self) -> None:
        """execute() converts None result to empty string."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=None)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == ""

    async def test_metrics_not_tracked_on_error(self) -> None:
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=RuntimeError("fail"))

        metrics = MagicMock()
        metrics.track_skill_time = MagicMock()

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry, metrics=metrics)
        tc = make_tool_call(name="s", arguments="{}")

        await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        metrics.track_skill_time.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Test Malformed Tool Calls
# ═══════════════════════════════════════════════════════════════════════════════


class TestMalformedToolCalls:
    """execute() handles structurally malformed tool_call objects."""

    # ── Missing function attribute ─────────────────────────────────────────

    async def test_missing_function_attribute_returns_error(self) -> None:
        """tool_call without 'function' returns a MalformedToolCall error."""
        tc = MagicMock(spec=[])
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "invalid_request" in result
        assert "malformed" in result

    async def test_function_without_name_returns_error(self) -> None:
        """tool_call.function without 'name' returns a MalformedToolCall error."""
        tc = MagicMock()
        tc.function = MagicMock(spec=[])
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "invalid_request" in result

    # ── None arguments ─────────────────────────────────────────────────────

    async def test_none_arguments_defaults_to_empty_dict(self) -> None:
        """tool_call with arguments=None treats them as {}."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok")

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments=None)

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "ok"
        skill.execute.assert_awaited_once_with(workspace_dir=Path("/tmp/ws"))

    # ── Unhandled exception types (parameterized) ──────────────────────────

    @pytest.mark.parametrize(
        "exception,expected_display",
        [
            pytest.param(ValueError("bad value"), "internal_error", id="value_error"),
            pytest.param(KeyError("missing_key"), "internal_error", id="key_error"),
            pytest.param(OSError("io failure"), "internal_error", id="os_error"),
            pytest.param(TypeError("wrong type"), "internal_error", id="type_error"),
            pytest.param(PermissionError("denied"), "permission_denied", id="permission_error"),
            pytest.param(FileNotFoundError("gone"), "not_found", id="file_not_found"),
        ],
    )
    async def test_unhandled_exception_returns_error(
        self,
        exception: Exception,
        expected_display: str,
    ) -> None:
        """Various unhandled exception types are caught and formatted."""
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=exception)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert "unexpected error" in result
        assert expected_display in result


# ═══════════════════════════════════════════════════════════════════════════════
# Test Non-String Skill Return Values
# ═══════════════════════════════════════════════════════════════════════════════


class TestNonStringSkillResults:
    """execute() converts non-string skill results via str() without crashing."""

    async def test_dict_result_converted_to_string(self) -> None:
        """Skill returning a dict is converted via str() without crashing."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value={"key": "value", "num": 42})

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert isinstance(result, str)
        assert "key" in result
        assert "value" in result

    async def test_list_result_converted_to_string(self) -> None:
        """Skill returning a list is converted via str() without crashing."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=["alpha", "beta", "gamma"])

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert isinstance(result, str)
        assert "alpha" in result

    async def test_bytes_result_converted_to_string(self) -> None:
        """Skill returning bytes gets str() representation (verbose but safe)."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=b"\x00\x01\x02binary_data")

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert isinstance(result, str)
        # str(bytes) includes the b'' prefix representation
        assert "binary_data" in result

    async def test_large_bytes_result_does_not_crash(self) -> None:
        """Skill returning large bytes object is safely converted via str()."""
        skill = AsyncMock()
        # 100KB of binary data
        skill.execute = AsyncMock(return_value=b"\xff" * 102_400)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert isinstance(result, str)
        assert len(result) > 0

    async def test_integer_result_converted_to_string(self) -> None:
        """Skill returning an integer is converted to its string form."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=42)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "42"

    async def test_float_result_converted_to_string(self) -> None:
        """Skill returning a float is converted to its string form."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=3.14)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "3.14"

    async def test_bool_result_converted_to_string(self) -> None:
        """Skill returning a bool is converted to 'True'/'False'."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=True)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "True"

    async def test_nested_structure_result_converted(self) -> None:
        """Skill returning deeply nested dict/list is converted safely."""
        skill = AsyncMock()
        skill.execute = AsyncMock(
            return_value={"users": [{"name": "Alice"}, {"name": "Bob"}], "count": 2}
        )

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert isinstance(result, str)
        assert "Alice" in result
        assert "Bob" in result

    async def test_custom_object_result_converted_to_string(self) -> None:
        """Skill returning a custom object uses its __str__ method."""

        class CustomResult:
            def __str__(self) -> str:
                return "custom-output"

        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=CustomResult())

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "custom-output"

    async def test_empty_list_result_converted(self) -> None:
        """Skill returning an empty list becomes '[]'."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value=[])

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        result = await executor.execute(
            chat_id="chat_1",
            tool_call=tc,
            workspace_dir=Path("/tmp/ws"),
        )

        assert result == "[]"


# ═══════════════════════════════════════════════════════════════════════════════
# Test ToolExecutor.close() audit logger cleanup
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolExecutorClose:
    """Verify ToolExecutor.close() flushes and releases the audit logger."""

    async def test_close_sets_audit_logger_to_none(self, tmp_path: Path) -> None:
        """After close(), _audit_logger is None even if it was created."""
        registry = _make_skill_registry()
        executor = ToolExecutor(
            skills_registry=registry,
            audit_log_dir=tmp_path / "audit",
        )
        assert executor._audit_logger is not None

        executor.close()
        assert executor._audit_logger is None

    async def test_close_calls_skill_audit_logger_close(self, tmp_path: Path) -> None:
        """close() delegates to SkillAuditLogger.close() for flushing."""
        registry = _make_skill_registry()
        executor = ToolExecutor(
            skills_registry=registry,
            audit_log_dir=tmp_path / "audit",
        )
        logger = executor._audit_logger
        assert logger is not None

        executor.close()
        # After close(), SkillAuditLogger._path is set to None
        assert logger._path is None

    async def test_close_no_op_when_audit_logger_never_created(self) -> None:
        """close() is a safe no-op when no skills were ever executed."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        # _audit_logger is never created because _audit() is never called
        assert executor._audit_logger is None

        # Should not raise
        executor.close()
        assert executor._audit_logger is None

    async def test_close_no_op_without_audit_log_dir(self) -> None:
        """close() is a no-op when audit_log_dir was not configured."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry, audit_log_dir=None)
        assert executor._audit_logger is None

        executor.close()
        assert executor._audit_logger is None

    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        """Calling close() multiple times is safe."""
        registry = _make_skill_registry()
        executor = ToolExecutor(
            skills_registry=registry,
            audit_log_dir=tmp_path / "audit",
        )
        executor._audit("chat_1", "skill_a", "{}", True, "success")
        assert executor._audit_logger is not None

        executor.close()
        assert executor._audit_logger is None

        # Second call should not raise
        executor.close()
        assert executor._audit_logger is None

    async def test_close_flushes_buffered_entries(self, tmp_path: Path) -> None:
        """Entries written before close() are persisted to disk."""
        audit_dir = tmp_path / "audit"
        registry = _make_skill_registry()
        executor = ToolExecutor(
            skills_registry=registry,
            audit_log_dir=audit_dir,
        )
        executor._audit("chat_1", "skill_a", "{}", True, "success")

        # Verify the audit file has at least one entry before closing
        audit_file = audit_dir / "audit.jsonl"
        assert audit_file.exists()
        content_before = audit_file.read_text(encoding="utf-8")
        assert "skill_a" in content_before

        executor.close()

        # File content should still be intact after close
        content_after = audit_file.read_text(encoding="utf-8")
        assert content_after == content_before

    async def test_audit_after_close_is_no_op(self, tmp_path: Path) -> None:
        """_audit() after close() is a no-op (logger was set to None)."""
        audit_dir = tmp_path / "audit"
        registry = _make_skill_registry()
        executor = ToolExecutor(
            skills_registry=registry,
            audit_log_dir=audit_dir,
        )
        executor._audit("chat_1", "skill_a", "{}", True, "success")
        executor.close()

        # _audit_logger is already None after close(), so _audit() is a no-op
        executor._audit("chat_2", "skill_b", "{}", True, "success")
        assert executor._audit_logger is None

        # Verify only the pre-close entry exists
        audit_file = audit_dir / "audit.jsonl"
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "skill_a" in lines[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Test Per-Skill Circuit Breaker
# ═══════════════════════════════════════════════════════════════════════════════


class TestSkillCircuitBreaker:
    """Per-skill circuit breaker opens after consecutive failures."""

    async def test_breaker_not_triggered_on_success(self) -> None:
        """Successful skill execution keeps the breaker CLOSED."""
        skill = AsyncMock()
        skill.execute = AsyncMock(return_value="ok")

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        breaker = executor._get_breaker("s")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_breaker_opens_after_consecutive_failures(self) -> None:
        """Breaker transitions to OPEN after SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD failures."""
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=RuntimeError("boom"))

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        from src.constants import SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD

        for _ in range(SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        breaker = executor._get_breaker("s")
        assert breaker.state == CircuitState.OPEN

    async def test_open_breaker_fast_fails_skill(self) -> None:
        """When breaker is OPEN, skill execution is skipped with CircuitBreakerOpen error."""
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=RuntimeError("boom"))

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        from src.constants import SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD

        # Drive the breaker OPEN
        for _ in range(SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        # Replace skill with a succeeding one — breaker should still fast-fail
        skill.execute = AsyncMock(return_value="ok")
        result = await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        assert "temporarily unavailable" in result
        assert "service_unavailable" in result
        skill.execute.assert_not_awaited()

    async def test_breakers_are_isolated_per_skill(self) -> None:
        """Each skill has its own independent circuit breaker."""
        skill_a = AsyncMock()
        skill_a.execute = AsyncMock(side_effect=RuntimeError("fail"))
        skill_b = AsyncMock()
        skill_b.execute = AsyncMock(return_value="ok")

        registry = _make_skill_registry(skill_map={"a": skill_a, "b": skill_b})
        executor = ToolExecutor(skills_registry=registry)

        from src.constants import SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD

        tc_a = make_tool_call(name="a", arguments="{}")
        for _ in range(SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD):
            await executor.execute(chat_id="c", tool_call=tc_a, workspace_dir=Path("/tmp/ws"))

        # Breaker for 'a' is open
        assert executor._get_breaker("a").state == CircuitState.OPEN
        # Breaker for 'b' is still closed (never used)
        assert executor._get_breaker("b").state == CircuitState.CLOSED

        # Skill 'b' still executes normally
        tc_b = make_tool_call(name="b", arguments="{}")
        result = await executor.execute(chat_id="c", tool_call=tc_b, workspace_dir=Path("/tmp/ws"))
        assert result == "ok"

    async def test_timeout_records_failure(self) -> None:
        """Skill timeout records a failure on the breaker."""
        skill = AsyncMock()

        async def _hang(**kwargs):
            await asyncio.sleep(999)

        skill.execute = AsyncMock(side_effect=_hang)

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        with patch("src.core.tool_executor.DEFAULT_SKILL_TIMEOUT", 0.05):
            await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        breaker = executor._get_breaker("s")
        assert breaker.failure_count == 1

    async def test_skill_error_records_failure(self) -> None:
        """SkillError records a failure on the breaker."""
        skill = AsyncMock()
        skill.execute = AsyncMock(side_effect=SkillError("disk full", reason="DiskSpaceError"))

        registry = _make_skill_registry(skill_map={"s": skill})
        executor = ToolExecutor(skills_registry=registry)
        tc = make_tool_call(name="s", arguments="{}")

        await executor.execute(chat_id="c", tool_call=tc, workspace_dir=Path("/tmp/ws"))

        breaker = executor._get_breaker("s")
        assert breaker.failure_count == 1

    async def test_get_breaker_is_lazy(self) -> None:
        """_get_breaker creates breakers on demand."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)

        assert executor._skill_breakers.size == 0
        breaker = executor._get_breaker("new_skill")
        assert executor._skill_breakers.size == 1
        assert breaker is executor._skill_breakers.get_or_create("new_skill")

    async def test_get_breaker_returns_same_instance(self) -> None:
        """_get_breaker returns the same breaker for the same skill name."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)

        b1 = executor._get_breaker("s")
        b2 = executor._get_breaker("s")
        assert b1 is b2

    async def test_skill_breakers_registry_does_not_exceed_max_size(self) -> None:
        """Registry evicts oldest breakers when max_skills cap is exceeded."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        max_skills = 5

        with patch.object(
            executor._skill_breakers, "_max_skills", max_skills
        ):
            breakers_reg = executor._skill_breakers

            # Fill to capacity.
            for i in range(max_skills):
                breakers_reg.get_or_create(f"skill_{i}")
            assert breakers_reg.size == max_skills

            # The first entry should still be present before overflow.
            assert "skill_0" in breakers_reg._breakers

            # One more triggers LRU eviction of the oldest.
            breakers_reg.get_or_create("skill_overflow")
            assert breakers_reg.size == max_skills
            # Oldest was evicted.
            assert "skill_0" not in breakers_reg._breakers
            # Newest is present.
            assert "skill_overflow" in breakers_reg._breakers

    async def test_skill_breakers_lru_eviction_removes_oldest_first(self) -> None:
        """When the registry overflows, the least-recently-used breaker is evicted."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        max_skills = 5

        with patch.object(
            executor._skill_breakers, "_max_skills", max_skills
        ):
            breakers_reg = executor._skill_breakers

            # Insert two specific entries we'll track.
            breakers_reg.get_or_create("old")
            breakers_reg.get_or_create("mid")

            # Fill remaining slots.
            for i in range(max_skills - 2):
                breakers_reg.get_or_create(f"fill_{i}")

            # All present — at capacity.
            assert breakers_reg.size == max_skills
            assert "old" in breakers_reg._breakers
            assert "mid" in breakers_reg._breakers

            # Overflow evicts "old" (least recently used).
            breakers_reg.get_or_create("new_overflow")
            assert "old" not in breakers_reg._breakers
            assert "mid" in breakers_reg._breakers
            assert "new_overflow" in breakers_reg._breakers

    async def test_skill_breakers_repeated_access_refreshes_lru(self) -> None:
        """Re-accessing a breaker promotes it, preventing its eviction."""
        registry = _make_skill_registry()
        executor = ToolExecutor(skills_registry=registry)
        max_skills = 5

        with patch.object(
            executor._skill_breakers, "_max_skills", max_skills
        ):
            breakers_reg = executor._skill_breakers

            breakers_reg.get_or_create("precious")

            # Fill to capacity — "precious" becomes the oldest.
            for i in range(max_skills - 1):
                breakers_reg.get_or_create(f"fill_{i}")

            assert breakers_reg.size == max_skills

            # Re-access "precious" to refresh its LRU position.
            breakers_reg.get_or_create("precious")

            # Overflow — "precious" should survive.
            breakers_reg.get_or_create("overflow")
            assert "precious" in breakers_reg._breakers
            # fill_0 (now the oldest) should be evicted instead.
            assert "fill_0" not in breakers_reg._breakers


# ═══════════════════════════════════════════════════════════════════════════════
# Property-Based Tests for _measured_depth (Hypothesis)
# ═══════════════════════════════════════════════════════════════════════════════

from hypothesis import given, settings
from hypothesis import strategies as st

# Strategy for generating JSON-compatible nested structures
_json_value = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=50,
)


class TestMeasuredDepth:
    """Property-based tests for _measured_depth — adversarial nested structures."""

    # ── Deterministic edge cases ────────────────────────────────────────────

    def test_flat_value_has_depth_zero(self) -> None:
        """Non-container values have depth 0."""
        assert _measured_depth(42) == 0
        assert _measured_depth("hello") == 0
        assert _measured_depth(None) == 0
        assert _measured_depth(True) == 0

    def test_empty_dict_has_depth_zero(self) -> None:
        """Empty dicts are skipped, depth is 0."""
        assert _measured_depth({}) == 0

    def test_empty_list_has_depth_zero(self) -> None:
        """Empty lists are skipped, depth is 0."""
        assert _measured_depth([]) == 0

    def test_single_level_dict(self) -> None:
        assert _measured_depth({"a": 1}) == 1

    def test_single_level_list(self) -> None:
        assert _measured_depth([1, 2, 3]) == 1

    def test_deeply_nested_single_key_dicts(self) -> None:
        """Deeply nested single-key dicts track depth correctly."""
        obj: dict = {"a": 1}
        for _ in range(100):
            obj = {"key": obj}
        assert _measured_depth(obj) == 101

    def test_alternating_dict_list_chain(self) -> None:
        """Alternating dict→list→dict chains."""
        assert _measured_depth({"a": [1]}) == 2
        assert _measured_depth({"a": [{"b": 1}]}) == 3
        assert _measured_depth([{"a": [1]}]) == 3

    def test_mixed_nesting(self) -> None:
        """Mixed dict/list nesting at various depths."""
        obj = {"a": [1, {"b": [2, {"c": 3}]}]}
        assert _measured_depth(obj) == 5

    def test_empty_containers_at_depth(self) -> None:
        """Empty containers don't contribute to depth."""
        assert _measured_depth({"a": {}}) == 1  # inner {} is empty, skipped
        assert _measured_depth({"a": []}) == 1  # inner [] is empty, skipped

    def test_empty_container_only_tree(self) -> None:
        """Non-empty dicts count even when their values are empty dicts."""
        assert _measured_depth({"a": {"b": {"c": {}}}}) == 3
        assert _measured_depth([[[[[]]]]]) == 4

    # ── Hypothesis property-based tests ─────────────────────────────────────

    @given(obj=_json_value)
    @settings(max_examples=200)
    def test_depth_is_non_negative(self, obj: object) -> None:
        """Depth should always be non-negative for any JSON structure."""
        result = _measured_depth(obj)
        assert result >= 0

    @given(obj=_json_value)
    @settings(max_examples=200)
    def test_depth_matches_recursive_reference(self, obj: object) -> None:
        """Iterative _measured_depth matches a simple recursive implementation."""
        def recursive_depth(o: object, d: int = 0) -> int:
            if isinstance(o, dict) and o:
                return max(recursive_depth(v, d + 1) for v in o.values())
            if isinstance(o, list) and o:
                return max(recursive_depth(v, d + 1) for v in o)
            return d

        assert _measured_depth(obj) == recursive_depth(obj)

    @given(obj=_json_value)
    @settings(max_examples=200)
    def test_wrapping_increments_depth_by_one(self, obj: object) -> None:
        """Wrapping any value in a single-key dict increments depth by exactly 1."""
        base = _measured_depth(obj)
        wrapped = {"k": obj}
        # If obj is an empty container, _measured_depth skips it, so depth is 1.
        # Otherwise depth is base + 1.
        if isinstance(obj, (dict, list)) and not obj:
            assert _measured_depth(wrapped) == 1
        else:
            assert _measured_depth(wrapped) == base + 1
