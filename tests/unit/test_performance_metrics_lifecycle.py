"""
Tests for PerformanceMetrics background service lifecycle.

Unit tests covering:
  - start_periodic_logging sets running state and creates background task
  - Periodic logging emits summaries at the configured interval
  - stop() cancels the background task cleanly without hanging
  - Lifecycle edge cases: idempotent start, stop when not started, restart
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from src.monitoring import PerformanceMetrics, reset_metrics_collector


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def metrics() -> PerformanceMetrics:
    """Provide a fresh PerformanceMetrics instance."""
    return PerformanceMetrics()


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure the global singleton is reset before and after each test."""
    reset_metrics_collector()
    yield
    reset_metrics_collector()


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceMetrics — start / stop lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class TestLifecycle:
    """Tests for PerformanceMetrics start/stop lifecycle."""

    async def test_start_periodic_logging_sets_running(
        self, metrics: PerformanceMetrics
    ):
        metrics.start_periodic_logging(interval_seconds=60)
        assert metrics.is_running is True
        await metrics.stop()

    async def test_start_periodic_logging_creates_background_task(
        self, metrics: PerformanceMetrics
    ):
        metrics.start_periodic_logging(interval_seconds=60)
        assert metrics._task is not None
        assert not metrics._task.done()
        await metrics.stop()

    async def test_start_periodic_logging_sets_interval(
        self, metrics: PerformanceMetrics
    ):
        metrics.start_periodic_logging(interval_seconds=30)
        assert metrics._interval == 30
        await metrics.stop()

    async def test_start_idempotent(self, metrics: PerformanceMetrics):
        metrics.start_periodic_logging(interval_seconds=60)
        first_task = metrics._task
        metrics.start_periodic_logging(interval_seconds=60)
        assert metrics._task is first_task
        await metrics.stop()

    async def test_stop_clears_running(self, metrics: PerformanceMetrics):
        metrics.start_periodic_logging(interval_seconds=60)
        await metrics.stop()
        assert metrics.is_running is False

    async def test_stop_cancels_task(self, metrics: PerformanceMetrics):
        metrics.start_periodic_logging(interval_seconds=60)
        task = metrics._task
        await metrics.stop()
        assert task is not None
        assert task.cancelled() or task.done()

    async def test_stop_clears_task_reference(self, metrics: PerformanceMetrics):
        metrics.start_periodic_logging(interval_seconds=60)
        await metrics.stop()
        assert metrics._task is None

    async def test_stop_when_not_started(self, metrics: PerformanceMetrics):
        """Stopping without starting should not raise."""
        await metrics.stop()
        assert metrics.is_running is False

    async def test_start_stop_restart(self, metrics: PerformanceMetrics):
        metrics.start_periodic_logging(interval_seconds=60)
        await metrics.stop()
        metrics.start_periodic_logging(interval_seconds=30)
        assert metrics.is_running is True
        assert metrics._task is not None
        assert metrics._interval == 30
        await metrics.stop()


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceMetrics — periodic summary emission
# ─────────────────────────────────────────────────────────────────────────────


class TestPeriodicLogging:
    """Tests that start_periodic_logging emits summaries at the configured interval."""

    async def test_emits_summary_after_interval(
        self, metrics: PerformanceMetrics
    ):
        """Verify _log_summary is called after the configured interval."""
        with patch.object(
            metrics, "_log_summary", wraps=metrics._log_summary
        ) as mock_log:
            metrics.start_periodic_logging(interval_seconds=0.05)
            await asyncio.sleep(0.15)
            await metrics.stop()

        assert mock_log.call_count >= 1

    async def test_emits_multiple_summaries_over_time(
        self, metrics: PerformanceMetrics
    ):
        """Verify multiple summaries are emitted across several intervals."""
        with patch.object(
            metrics, "_log_summary", wraps=metrics._log_summary
        ) as mock_log:
            metrics.start_periodic_logging(interval_seconds=0.05)
            await asyncio.sleep(0.25)
            await metrics.stop()

        assert mock_log.call_count >= 2

    async def test_summary_includes_logged_output(
        self, metrics: PerformanceMetrics, caplog: pytest.LogCaptureFixture
    ):
        """Verify the summary produces structured log output."""
        metrics.track_message_latency(1.5)
        metrics.track_llm_latency(2.3)

        with caplog.at_level(logging.INFO, logger="src.monitoring.performance"):
            metrics.start_periodic_logging(interval_seconds=0.05)
            await asyncio.sleep(0.15)
            await metrics.stop()

        summary_logs = [
            r for r in caplog.records if "Performance summary" in r.message
        ]
        assert len(summary_logs) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# PerformanceMetrics — stop cleanliness
# ─────────────────────────────────────────────────────────────────────────────


class TestStopCleanliness:
    """Tests that stop() cancels the background task cleanly without hanging."""

    async def test_stop_completes_within_timeout(
        self, metrics: PerformanceMetrics
    ):
        """stop() should return promptly, not hang waiting for the task."""
        metrics.start_periodic_logging(interval_seconds=0.05)
        await asyncio.sleep(0.1)

        # stop() should complete quickly (well under 1 second)
        await asyncio.wait_for(metrics.stop(), timeout=1.0)
        assert metrics.is_running is False

    async def test_stop_during_sleep_cancels_cleanly(
        self, metrics: PerformanceMetrics
    ):
        """Cancelling while _run_loop is sleeping should not raise."""
        metrics.start_periodic_logging(interval_seconds=60)
        # The loop is now sleeping for 60s — cancel mid-sleep
        await metrics.stop()
        assert metrics.is_running is False

    async def test_double_stop_does_not_raise(
        self, metrics: PerformanceMetrics
    ):
        """Calling stop() twice should not raise."""
        metrics.start_periodic_logging(interval_seconds=0.05)
        await asyncio.sleep(0.1)
        await metrics.stop()
        await metrics.stop()  # Second stop should be a no-op
        assert metrics.is_running is False

    async def test_run_loop_handles_exceptions_gracefully(
        self, metrics: PerformanceMetrics
    ):
        """An exception inside _run_loop should be caught and the loop continues."""
        call_count = 0
        original = metrics.refresh_system_metrics

        async def flaky_refresh() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated psutil failure")
            await original()

        with patch.object(
            metrics, "refresh_system_metrics", side_effect=flaky_refresh
        ):
            metrics.start_periodic_logging(interval_seconds=0.05)
            await asyncio.sleep(0.25)
            await metrics.stop()

        # Loop should have continued despite the first failure
        assert call_count >= 2
