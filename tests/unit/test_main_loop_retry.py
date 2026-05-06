"""
Tests for src/app.py — per-category retry policies in the main loop.

Covers:
- _RetryPolicy dataclass construction
- _get_retry_policy() returns correct policy per category
- _run_with_retry() retries on transient errors and exhausts on max retries
- _run_with_retry() fails fast on non-retryable categories
- _run_with_retry() returns cleanly on normal shutdown (no error)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.app import (
    _NO_RETRY,
    _get_retry_policy,
    _MainLoopErrorCategory,
    _RetryPolicy,
)
from src.constants.app import (
    MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES,
    MAIN_LOOP_CHANNEL_DISCONNECT_RETRY_DELAY,
    MAIN_LOOP_LLM_TRANSIENT_INITIAL_DELAY,
    MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES,
)
from src.exceptions import BridgeError, ConfigurationError, ErrorCode, LLMError

# ─────────────────────────────────────────────────────────────────────────────
# _RetryPolicy dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryPolicy:
    """_RetryPolicy frozen dataclass."""

    def test_default_values(self) -> None:
        policy = _RetryPolicy(max_retries=3)
        assert policy.max_retries == 3
        assert policy.initial_delay == 0.0
        assert policy.use_exponential_backoff is True

    def test_custom_values(self) -> None:
        policy = _RetryPolicy(
            max_retries=5,
            initial_delay=3.0,
            use_exponential_backoff=False,
        )
        assert policy.max_retries == 5
        assert policy.initial_delay == 3.0
        assert policy.use_exponential_backoff is False

    def test_frozen(self) -> None:
        policy = _RetryPolicy(max_retries=1)
        with pytest.raises(AttributeError):
            policy.max_retries = 99  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# _get_retry_policy()
# ─────────────────────────────────────────────────────────────────────────────


class TestGetRetryPolicy:
    """_get_retry_policy returns the correct policy per category."""

    def test_llm_transient_policy(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.LLM_TRANSIENT)
        assert policy.max_retries == MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES
        assert policy.initial_delay == MAIN_LOOP_LLM_TRANSIENT_INITIAL_DELAY
        assert policy.use_exponential_backoff is True

    def test_channel_disconnect_policy(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.CHANNEL_DISCONNECT)
        assert policy.max_retries == MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES
        assert policy.initial_delay == MAIN_LOOP_CHANNEL_DISCONNECT_RETRY_DELAY
        assert policy.use_exponential_backoff is False

    def test_llm_permanent_fails_fast(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.LLM_PERMANENT)
        assert policy.max_retries == 0

    def test_filesystem_fails_fast(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.FILESYSTEM)
        assert policy.max_retries == 0

    def test_configuration_fails_fast(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.CONFIGURATION)
        assert policy.max_retries == 0

    def test_unknown_fails_fast(self) -> None:
        policy = _get_retry_policy(_MainLoopErrorCategory.UNKNOWN)
        assert policy.max_retries == 0

    def test_no_retry_sentinel_is_zero(self) -> None:
        assert _NO_RETRY.max_retries == 0


# ─────────────────────────────────────────────────────────────────────────────
# _run_with_retry() integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_app() -> AsyncMock:
    """Create a mock Application with the minimal attributes _run_with_retry needs."""
    app = AsyncMock()
    app._verbose = False
    app._session_metrics = Mock()
    app._session_metrics.increment_errors = Mock()
    app.shutdown_mgr = AsyncMock()
    return app


class TestRunWithRetryNormalShutdown:
    """Normal shutdown (no exception) returns cleanly."""

    @pytest.mark.asyncio
    async def test_normal_shutdown(self, mock_app: AsyncMock) -> None:
        from src.app import Application

        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(return_value=None)

        await Application._run_with_retry(mock_app)

        mock_app.shutdown_mgr.wait_for_shutdown.assert_awaited_once()
        mock_app._session_metrics.increment_errors.assert_not_called()


class TestRunWithRetryFailFast:
    """Non-retryable errors cause immediate return (shutdown)."""

    @pytest.mark.asyncio
    async def test_configuration_error_fails_fast(self, mock_app: AsyncMock) -> None:
        from src.app import Application

        exc = ConfigurationError("bad config")
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=exc)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
        ):
            mock_metrics.return_value.track_error = Mock()
            await Application._run_with_retry(mock_app)

        # Should have called wait_for_shutdown only once (no retries).
        assert mock_app.shutdown_mgr.wait_for_shutdown.await_count == 1
        mock_app._session_metrics.increment_errors.assert_called_once()

    @pytest.mark.asyncio
    async def test_os_error_fails_fast(self, mock_app: AsyncMock) -> None:
        from src.app import Application

        exc = OSError("disk full")
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=exc)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
        ):
            mock_metrics.return_value.track_error = Mock()
            await Application._run_with_retry(mock_app)

        assert mock_app.shutdown_mgr.wait_for_shutdown.await_count == 1


class TestRunWithRetryRetriesThenExhausts:
    """Retryable errors retry the configured number of times, then fail."""

    @pytest.mark.asyncio
    async def test_llm_transient_retries_exhausted(self, mock_app: AsyncMock) -> None:
        from src.app import Application

        exc = LLMError(error_code=ErrorCode.LLM_RATE_LIMITED)
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=exc)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
            patch("src.app.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_metrics.return_value.track_error = Mock()
            await Application._run_with_retry(mock_app)

        # 1 initial + 3 retries = 4 calls total.
        assert (
            mock_app.shutdown_mgr.wait_for_shutdown.await_count
            == MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES + 1
        )

    @pytest.mark.asyncio
    async def test_channel_disconnect_retries_exhausted(
        self, mock_app: AsyncMock
    ) -> None:
        from src.app import Application

        exc = BridgeError("connection lost")
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=exc)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
            patch("src.app.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_metrics.return_value.track_error = Mock()
            await Application._run_with_retry(mock_app)

        assert (
            mock_app.shutdown_mgr.wait_for_shutdown.await_count
            == MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES + 1
        )


class TestRunWithRetryRecoversAfterTransient:
    """A transient error followed by normal shutdown recovers successfully."""

    @pytest.mark.asyncio
    async def test_recovers_after_one_llm_transient(self, mock_app: AsyncMock) -> None:
        from src.app import Application

        exc = LLMError(error_code=ErrorCode.LLM_TIMEOUT)
        # Fail once, then succeed.
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(
            side_effect=[exc, None]
        )

        with patch("src.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await Application._run_with_retry(mock_app)

        assert mock_app.shutdown_mgr.wait_for_shutdown.await_count == 2
        mock_sleep.assert_awaited_once()
        # Only one error tracked for the transient failure.
        mock_app._session_metrics.increment_errors.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovers_after_channel_disconnect(
        self, mock_app: AsyncMock
    ) -> None:
        from src.app import Application

        exc = BridgeError("disconnected")
        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(
            side_effect=[exc, exc, None]
        )

        with patch("src.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await Application._run_with_retry(mock_app)

        assert mock_app.shutdown_mgr.wait_for_shutdown.await_count == 3
        assert mock_sleep.await_count == 2
