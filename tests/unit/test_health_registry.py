"""
Tests for src/health/registry.py — HealthCheckRegistry per-check timeout.

Verifies:
- Async checks that exceed the per-check timeout return DEGRADED.
- Sync checks completing within the timeout return their real result.
- Async checks completing within the timeout return their real result.
- A timed-out check does not prevent subsequent checks from running.
- The default timeout is used when ``check_timeout`` is not provided.
"""

from __future__ import annotations

import asyncio

import pytest

from src.health.models import ComponentHealth, HealthStatus
from src.health.registry import HealthCheckRegistry


# ── Helpers ──────────────────────────────────────────────────────────────


def _healthy_check() -> ComponentHealth:
    """Sync check that returns HEALTHY immediately."""
    return ComponentHealth(name="sync_ok", status=HealthStatus.HEALTHY)


async def _healthy_async_check() -> ComponentHealth:
    """Async check that returns HEALTHY immediately."""
    return ComponentHealth(name="async_ok", status=HealthStatus.HEALTHY)


async def _slow_check() -> ComponentHealth:
    """Async check that sleeps beyond any reasonable timeout."""
    await asyncio.sleep(60)
    return ComponentHealth(name="slow", status=HealthStatus.HEALTHY)


async def _slightly_slow_check() -> ComponentHealth:
    """Async check that sleeps 0.1s — slow but within a generous timeout."""
    await asyncio.sleep(0.1)
    return ComponentHealth(name="slightly_slow", status=HealthStatus.HEALTHY)


def _failing_check() -> ComponentHealth:
    """Sync check that raises an exception."""
    raise RuntimeError("boom")


# ── Tests ────────────────────────────────────────────────────────────────


class TestPerCheckTimeout:
    """Per-check timeout enforcement in run_all()."""

    async def test_async_check_that_times_out_returns_degraded(self) -> None:
        """A slow async check is cancelled and reported as DEGRADED."""
        registry = HealthCheckRegistry()
        registry.register(_slow_check, name="slow")

        report = await registry.run_all(check_timeout=0.05)

        assert len(report.components) == 1
        comp = report.components[0]
        assert comp.status == HealthStatus.DEGRADED
        assert "timed out" in comp.message

    async def test_sync_check_completes_within_timeout(self) -> None:
        """Sync checks are not affected by the timeout."""
        registry = HealthCheckRegistry()
        registry.register(_healthy_check, name="sync_ok")

        report = await registry.run_all(check_timeout=0.05)

        assert len(report.components) == 1
        assert report.components[0].status == HealthStatus.HEALTHY

    async def test_async_check_completes_within_timeout(self) -> None:
        """An async check finishing before the timeout returns its real result."""
        registry = HealthCheckRegistry()
        registry.register(_healthy_async_check, name="async_ok")

        report = await registry.run_all(check_timeout=5.0)

        assert len(report.components) == 1
        assert report.components[0].status == HealthStatus.HEALTHY

    async def test_timed_out_check_does_not_block_others(self) -> None:
        """A timed-out check does not prevent subsequent checks from running."""
        registry = HealthCheckRegistry()
        registry.register(_slow_check, name="slow")
        registry.register(_healthy_check, name="sync_ok")

        report = await registry.run_all(check_timeout=0.05)

        assert len(report.components) == 2
        assert report.components[0].status == HealthStatus.DEGRADED
        assert report.components[1].status == HealthStatus.HEALTHY

    async def test_failing_check_still_returns_degraded(self) -> None:
        """Exception-based failures still return DEGRADED (unchanged behavior)."""
        registry = HealthCheckRegistry()
        registry.register(_failing_check, name="boom")

        report = await registry.run_all(check_timeout=5.0)

        assert len(report.components) == 1
        assert report.components[0].status == HealthStatus.DEGRADED
        assert "RuntimeError" in report.components[0].message

    async def test_default_timeout_used_when_none_provided(self) -> None:
        """When check_timeout is None, DEFAULT_HEALTH_CHECK_TIMEOUT applies."""
        from src.constants.health import DEFAULT_HEALTH_CHECK_TIMEOUT

        # A check sleeping slightly longer than the default should time out.
        # We use a value just above the default to avoid flakiness.
        assert DEFAULT_HEALTH_CHECK_TIMEOUT > 0

        registry = HealthCheckRegistry()
        registry.register(_slow_check, name="slow")

        report = await registry.run_all()

        assert report.components[0].status == HealthStatus.DEGRADED
        assert "timed out" in report.components[0].message

    async def test_custom_timeout_overrides_default(self) -> None:
        """A generous custom timeout lets a slightly-slow check succeed."""
        registry = HealthCheckRegistry()
        registry.register(_slightly_slow_check, name="slightly_slow")

        report = await registry.run_all(check_timeout=1.0)

        assert report.components[0].status == HealthStatus.HEALTHY

    async def test_report_aggregates_degraded_overall(self) -> None:
        """A single timed-out check makes the overall report status DEGRADED."""
        registry = HealthCheckRegistry()
        registry.register(_slow_check, name="slow")

        report = await registry.run_all(check_timeout=0.05)

        report_dict = report.to_dict()
        assert report_dict["status"] == "degraded"
        assert report_dict["healthy"] is True  # degraded != unhealthy
