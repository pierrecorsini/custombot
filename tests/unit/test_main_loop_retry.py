"""
Tests for src/app.py — per-category retry policies in the main loop.

Covers:
- _RetryPolicy dataclass construction
- _get_retry_policy() returns correct policy per category
- _run_with_retry() retries on transient errors and exhausts on max retries
- _run_with_retry() fails fast on non-retryable categories
- _run_with_retry() returns cleanly on normal shutdown (no error)
- _run_with_retry() mixed error category transitions with recovery phases
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


class TestRunWithRetryMixedErrorCategoryTransitions:
    """Mixed error category transitions across sequential ``_run_with_retry`` calls.

    Simulates the full sequence described in PLAN.md:
        LLM transient → recovery → channel disconnect → recovery → permanent error

    Because ``_run_with_retry`` returns on normal shutdown, each recovery
    phase is a separate call with a fresh ``attempt`` counter.
    """

    @pytest.mark.asyncio
    async def test_llm_transient_then_channel_then_permanent(
        self, mock_app: AsyncMock
    ) -> None:
        """LLM transient → recover → channel disconnect → recover → permanent fails fast.

        Verifies that:
        - Each ``_run_with_retry`` call starts with a fresh attempt counter
        - The correct retry policy is applied per category (LLM transient
          retries with exponential backoff, channel disconnect retries with
          fixed delay, LLM permanent fails immediately)
        - ``increment_errors`` is recorded for every error (retryable + permanent)
        - ``track_error`` and ``emit_error_event`` fire only on fail-fast
        - ``asyncio.sleep`` is called only for retryable errors
        """
        from src.app import Application

        llm_transient = LLMError(error_code=ErrorCode.LLM_RATE_LIMITED)
        channel_disconnect = BridgeError("connection lost")
        llm_permanent = LLMError(error_code=ErrorCode.LLM_API_KEY_INVALID)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock) as mock_emit,
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
            patch("src.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_metrics.return_value.track_error = Mock()

            # ── Phase 1: LLM transient → retry → recovery ────────────
            mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(
                side_effect=[llm_transient, None]
            )
            await Application._run_with_retry(mock_app)

            # ── Phase 2: Channel disconnect → retry → recovery ────────
            mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(
                side_effect=[channel_disconnect, None]
            )
            await Application._run_with_retry(mock_app)

            # ── Phase 3: LLM permanent → fail fast (no retry) ────────
            mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(
                side_effect=[llm_permanent]
            )
            await Application._run_with_retry(mock_app)

        # ── Assertions ────────────────────────────────────────────────

        # Sleep called twice: once after LLM transient, once after channel
        # disconnect.  No sleep after the permanent (fail-fast) error.
        assert mock_sleep.await_count == 2

        # Error event emitted only for the permanent (fail-fast) error.
        assert mock_emit.await_count == 1
        emit_call_kwargs = mock_emit.call_args
        assert emit_call_kwargs[0][0] is llm_permanent
        assert emit_call_kwargs[1]["extra_data"]["category"] == "llm_permanent"

        # All three errors tracked by session metrics.
        assert mock_app._session_metrics.increment_errors.call_count == 3

        # Prometheus metrics collector tracks only the fail-fast error.
        assert mock_metrics.return_value.track_error.call_count == 1


class TestRunWithRetryCategoryTransitionAttemptReset:
    """Verify attempt counter resets when error category changes mid-sequence.

    This is the within-call counterpart to
    ``TestRunWithRetryMixedErrorCategoryTransitions`` which tests category
    changes *between* separate ``_run_with_retry`` calls (each with a fresh
    counter).  Here the category changes inside a *single* call, so the
    counter must be explicitly reset.
    """

    @pytest.mark.asyncio
    async def test_attempt_resets_on_category_change(
        self, mock_app: AsyncMock
    ) -> None:
        """LLM transient errors accumulate attempts, then channel disconnect
        gets its own fresh retry budget.

        Without the fix the monolithic ``attempt`` counter would carry over,
        stealing retries from the channel-disconnect budget.
        """
        from src.app import Application

        llm_transient = LLMError(error_code=ErrorCode.LLM_RATE_LIMITED)
        channel_disconnect = BridgeError("connection lost")

        # 3 LLM transient (attempts 1,2,3 — all ≤ 3 so each retries)
        # then 6 channel disconnect:
        #   with fix:   attempts 1–6 (1–5 retry, 6 > 5 triggers fail-fast)
        #   without:    attempts 4–6 (only 2 retries before fail-fast)
        errors = [llm_transient] * MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES
        errors += [channel_disconnect] * (MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES + 1)

        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=errors)

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
            patch("src.monitoring.performance.get_metrics_collector") as mock_metrics,
            patch("src.app.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_metrics.return_value.track_error = Mock()
            await Application._run_with_retry(mock_app)

        # With the fix: 3 LLM + 6 channel = 9 total calls.
        expected_calls = (
            MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES
            + MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES
            + 1
        )
        assert (
            mock_app.shutdown_mgr.wait_for_shutdown.await_count == expected_calls
        ), (
            f"Expected {expected_calls} calls (3 LLM + 6 channel), "
            f"got {mock_app.shutdown_mgr.wait_for_shutdown.await_count} "
            "— attempt counter was not reset on category change"
        )

    @pytest.mark.asyncio
    async def test_delay_resets_on_category_change(
        self, mock_app: AsyncMock
    ) -> None:
        """When category changes, delay is also reset so the new category
        gets its own ``initial_delay`` instead of inheriting the previous
        category's backoff state.
        """
        from src.app import Application

        llm_transient = LLMError(error_code=ErrorCode.LLM_RATE_LIMITED)
        channel_disconnect = BridgeError("connection lost")

        # 1 LLM transient (sets delay to initial_delay=2.0, then backoff)
        # 1 channel disconnect (should reset delay and use fixed 5.0 delay)
        errors = [llm_transient, channel_disconnect, None]

        mock_app.shutdown_mgr.wait_for_shutdown = AsyncMock(side_effect=errors)

        with patch("src.app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await Application._run_with_retry(mock_app)

        # First sleep: LLM transient initial delay (with jitter applied).
        # Second sleep: channel disconnect initial delay (with jitter applied).
        assert mock_sleep.await_count == 2

        # The channel disconnect sleep should be based on its own
        # initial_delay (5.0), not the LLM transient backoff state.
        channel_sleep_arg = mock_sleep.call_args_list[1].args[0]
        # With jitter (±10%), the value should be close to 5.0, not 4.0
        # (which would be 2.0 * 2 backoff multiplier from LLM).
        assert channel_sleep_arg >= 4.0, (
            f"Channel disconnect delay {channel_sleep_arg:.2f}s is too low — "
            "delay was not reset on category change (expected ~5.0s)"
        )
