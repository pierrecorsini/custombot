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

from src.core.tool_executor import MAX_ARGS_BYTES, MAX_ARGS_DEPTH, ToolExecutor, _measured_depth, format_skill_error
from src.exceptions import SkillError
from src.rate_limiter import RateLimitResult
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
        assert "error: PermissionError" in result
        assert "ref: corr-abc" in result

    @patch("src.core.tool_executor.get_correlation_id", return_value="")
    def test_no_ref_when_empty_correlation_id(self, _mock_corr: MagicMock) -> None:
        result = format_skill_error(
            skill_name="s",
            error_type="E",
            user_message="m",
        )
        assert "ref:" not in result


# ─────────────────────────────────────────────────────────────────────────────
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
        assert "UnknownSkill" in result


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
        assert "ArgumentError" in result


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
        assert "ArgumentError" in result

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
        assert "ArgumentError" in result

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

        metrics.track_skill_args_oversized.assert_called_once_with(
            "my_skill", len(oversized_args)
        )

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
        assert "TimeoutError" in result

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
        assert "TimeoutError" in result
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
        assert "TimeoutError" in result


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

        # Should contain the error from SkillError details
        assert "DiskSpaceError" in result

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
        assert "RuntimeError" in result

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

        assert "MalformedToolCall" in result
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

        assert "MalformedToolCall" in result

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
        "exception,expected_error_type",
        [
            pytest.param(ValueError("bad value"), "ValueError", id="value_error"),
            pytest.param(KeyError("missing_key"), "KeyError", id="key_error"),
            pytest.param(OSError("io failure"), "OSError", id="os_error"),
            pytest.param(TypeError("wrong type"), "TypeError", id="type_error"),
            pytest.param(PermissionError("denied"), "PermissionError", id="permission_error"),
            pytest.param(FileNotFoundError("gone"), "FileNotFoundError", id="file_not_found"),
        ],
    )
    async def test_unhandled_exception_returns_error(
        self,
        exception: Exception,
        expected_error_type: str,
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
        assert expected_error_type in result


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
        # Trigger lazy creation of the audit logger via _audit()
        executor._audit("chat_1", "skill_a", "{}", True, "success")
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
        # Trigger lazy creation
        executor._audit("chat_1", "skill_a", "{}", True, "success")
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

        # This should not recreate the logger because _audit_log_dir
        # was set to None during first _audit() call
        executor._audit("chat_2", "skill_b", "{}", True, "success")
        assert executor._audit_logger is None

        # Verify only the pre-close entry exists
        audit_file = audit_dir / "audit.jsonl"
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert "skill_a" in lines[0]
