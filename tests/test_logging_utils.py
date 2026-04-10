"""
Unit tests for @log_execution decorator.
"""

import asyncio
import logging

import pytest

from src.utils.logging_utils import log_execution


class TestLogExecution:
    """Unit tests for @log_execution decorator."""

    # ─────────────────────────────────────────────────────────────────────
    # Async Function Tests
    # ─────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_async_logs_successful_execution(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Should log completion with duration for async functions."""
        caplog.set_level(logging.DEBUG)

        @log_execution()
        async def sample_func() -> str:
            return "result"

        result = await sample_func()

        assert result == "result"
        assert "sample_func" in caplog.text
        assert "completed in" in caplog.text
        assert "ms" in caplog.text

    @pytest.mark.asyncio
    async def test_async_logs_entry_with_args(self, caplog: pytest.LogCaptureFixture):
        """Should log function entry with arguments."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_args=True)
        async def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        await greet("World", greeting="Hi")

        assert "greet" in caplog.text
        assert "World" in caplog.text
        assert "Hi" in caplog.text
        assert "starting" in caplog.text

    @pytest.mark.asyncio
    async def test_async_logs_result_when_enabled(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Should log function result when log_result=True."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_result=True, log_args=False)
        async def get_value() -> str:
            return "secret_value"

        await get_value()

        assert "secret_value" in caplog.text

    @pytest.mark.asyncio
    async def test_async_hides_result_when_disabled(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Should not log function result when log_result=False."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_result=False, log_args=False)
        async def get_secret() -> str:
            return "super_secret"

        await get_secret()

        assert "super_secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_async_logs_failure_with_error(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Should log failure with duration and error message."""
        caplog.set_level(logging.DEBUG)

        @log_execution()
        async def failing_func() -> None:
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            await failing_func()

        assert "failed after" in caplog.text
        assert "test error" in caplog.text
        assert "ValueError" in caplog.text

    @pytest.mark.asyncio
    async def test_async_uses_info_level(self, caplog: pytest.LogCaptureFixture):
        """Should use info log level when specified."""
        caplog.set_level(logging.INFO)

        @log_execution(level="info", log_args=False)
        async def info_func() -> None:
            pass

        await info_func()

        assert "info_func" in caplog.text
        assert "completed in" in caplog.text

    # ─────────────────────────────────────────────────────────────────────
    # Sync Function Tests
    # ─────────────────────────────────────────────────────────────────────

    def test_sync_logs_successful_execution(self, caplog: pytest.LogCaptureFixture):
        """Should log completion with duration for sync functions."""
        caplog.set_level(logging.DEBUG)

        @log_execution()
        def sync_func() -> int:
            return 42

        result = sync_func()

        assert result == 42
        assert "sync_func" in caplog.text
        assert "completed in" in caplog.text

    def test_sync_logs_entry_with_args(self, caplog: pytest.LogCaptureFixture):
        """Should log function entry with arguments for sync."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_args=True)
        def calculate(a: int, b: int) -> int:
            return a + b

        calculate(1, 2)

        assert "calculate" in caplog.text
        assert "1, 2" in caplog.text
        assert "starting" in caplog.text

    def test_sync_logs_result_when_enabled(self, caplog: pytest.LogCaptureFixture):
        """Should log function result when log_result=True for sync."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_result=True, log_args=False)
        def compute() -> str:
            return "computed"

        compute()

        assert "computed" in caplog.text

    def test_sync_logs_failure_with_error(self, caplog: pytest.LogCaptureFixture):
        """Should log failure with duration and error for sync."""
        caplog.set_level(logging.DEBUG)

        @log_execution()
        def failing_sync() -> None:
            raise RuntimeError("sync error")

        with pytest.raises(RuntimeError, match="sync error"):
            failing_sync()

        assert "failed after" in caplog.text
        assert "sync error" in caplog.text

    # ─────────────────────────────────────────────────────────────────────
    # Custom Logger Tests
    # ─────────────────────────────────────────────────────────────────────

    def test_uses_custom_logger(self, caplog: pytest.LogCaptureFixture):
        """Should use provided custom logger."""
        custom_logger = logging.getLogger("custom.module")
        caplog.set_level(logging.DEBUG)

        @log_execution(logger=custom_logger, log_args=False)
        def custom_logged() -> str:
            return "done"

        custom_logged()

        # Check that the custom module name appears in logs
        assert any("custom.module" in r.name for r in caplog.records)

    # ─────────────────────────────────────────────────────────────────────
    # Edge Cases
    # ─────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_truncates_long_args(self, caplog: pytest.LogCaptureFixture):
        """Should truncate very long argument strings."""
        caplog.set_level(logging.DEBUG)

        long_arg = "x" * 500

        @log_execution(log_args=True)
        async def long_args(data: str) -> str:
            return data

        await long_args(long_arg)

        # Should be truncated
        assert "xxx..." in caplog.text
        assert len([r for r in caplog.records if long_arg in r.message]) == 0

    def test_preserves_function_metadata(self):
        """Should preserve original function name and docstring."""

        @log_execution()
        def documented_func() -> None:
            """This is documented."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "This is documented."

    @pytest.mark.asyncio
    async def test_works_with_no_args(self, caplog: pytest.LogCaptureFixture):
        """Should work with functions that take no arguments."""
        caplog.set_level(logging.DEBUG)

        @log_execution(log_args=True)
        async def no_args() -> str:
            return "ok"

        await no_args()

        assert "no_args() -> starting" in caplog.text

    def test_integer_log_level(self, caplog: pytest.LogCaptureFixture):
        """Should accept integer log level."""
        caplog.set_level(logging.INFO)

        @log_execution(level=logging.INFO, log_args=False)
        def int_level_func() -> None:
            pass

        int_level_func()

        assert "int_level_func" in caplog.text
