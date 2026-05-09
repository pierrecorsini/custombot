"""Tests for NullMemoryMonitor — NullObject for MemoryMonitor Protocol."""

from __future__ import annotations

import asyncio

import pytest

from src.monitoring import NullMemoryMonitor
from src.utils.protocols import MemoryMonitor as MemoryMonitorProtocol


class TestNullMemoryMonitorSatisfiesProtocol:
    """NullMemoryMonitor must satisfy the MemoryMonitor Protocol."""

    def test_is_instance_of_protocol(self) -> None:
        monitor = NullMemoryMonitor()
        assert isinstance(monitor, MemoryMonitorProtocol)

    def test_register_cache_is_noop(self) -> None:
        monitor = NullMemoryMonitor()
        # Should not raise
        monitor.register_cache("test", lambda: 42)

    def test_unregister_cache_is_noop(self) -> None:
        monitor = NullMemoryMonitor()
        # Should not raise
        monitor.unregister_cache("test")

    def test_start_periodic_check_is_noop(self) -> None:
        monitor = NullMemoryMonitor()
        # Should not raise, not spawn any background tasks
        monitor.start_periodic_check(interval_seconds=999)

    @pytest.mark.asyncio
    async def test_stop_is_noop(self) -> None:
        monitor = NullMemoryMonitor()
        # Should not raise
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        monitor = NullMemoryMonitor()
        await monitor.stop()
        await monitor.stop()
        # No exception = pass

    def test_register_and_unregister_multiple_caches(self) -> None:
        monitor = NullMemoryMonitor()
        for i in range(10):
            monitor.register_cache(f"cache_{i}", lambda: 0)
        for i in range(10):
            monitor.unregister_cache(f"cache_{i}")
        # All no-ops, no exception
