"""
src/monitoring/memory.py — Memory usage monitoring for custombot.

Provides cross-platform memory monitoring using psutil:
- Periodic memory usage tracking
- Threshold-based warnings
- LRU cache size tracking
- Memory stats for health check integration

Usage:
    from src.monitoring.memory import MemoryMonitor, get_memory_stats

    # Create monitor with threshold
    monitor = MemoryMonitor(warning_threshold_percent=80.0)
    monitor.start_periodic_check(interval_seconds=60.0)

    # Get current stats
    stats = get_memory_stats()
    print(f"Memory usage: {stats.used_percent}%")

    # Register LRU cache for tracking
    monitor.register_cache("chat_locks", cache_instance)
"""

from __future__ import annotations

import asyncio
import logging
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.utils.background_service import BaseBackgroundService
from src.utils.singleton import get_or_create_singleton, reset_singleton

log = logging.getLogger(__name__)

# Default warning threshold for memory usage (percentage).
DEFAULT_MEMORY_WARNING_THRESHOLD: float = 80.0

# Default interval for periodic memory checks (seconds).
DEFAULT_MEMORY_CHECK_INTERVAL: float = 60.0


@dataclass(slots=True)
class MemoryStats:
    """Memory usage statistics for system and process."""

    # System memory
    total_gb: float
    available_gb: float
    used_percent: float

    # Process memory
    process_rss_mb: float
    process_vms_mb: float
    process_percent: float

    # Swap memory
    swap_total_gb: float
    swap_used_percent: float

    # Metadata
    platform: str
    timestamp: float = field(default_factory=time.time)

    # Optional platform-specific fields
    buffers_gb: float = 0.0
    cached_gb: float = 0.0

    # LRU cache sizes (name -> size)
    cache_sizes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "system": {
                "total_gb": round(self.total_gb, 2),
                "available_gb": round(self.available_gb, 2),
                "used_percent": round(self.used_percent, 1),
                "buffers_gb": round(self.buffers_gb, 2),
                "cached_gb": round(self.cached_gb, 2),
            },
            "process": {
                "rss_mb": round(self.process_rss_mb, 2),
                "vms_mb": round(self.process_vms_mb, 2),
                "percent": round(self.process_percent, 2),
            },
            "swap": {
                "total_gb": round(self.swap_total_gb, 2),
                "used_percent": round(self.swap_used_percent, 1),
            },
            "cache_sizes": self.cache_sizes,
            "platform": self.platform,
            "timestamp": self.timestamp,
        }


def get_memory_stats(
    cache_tracker: Optional[dict[str, Callable[[], int]]] = None,
) -> MemoryStats:
    """
    Get current memory usage statistics.

    Cross-platform function that works on Windows, Linux, and macOS.
    Uses psutil for memory information.

    Args:
        cache_tracker: Optional dict mapping cache names to size getter functions.

    Returns:
        MemoryStats with current memory usage.
    """
    import psutil

    # System memory
    mem = psutil.virtual_memory()
    current_platform = platform.system()

    # Get platform-specific fields safely
    buffers_gb = 0.0
    cached_gb = 0.0

    if current_platform == "Linux":
        buffers_gb = getattr(mem, "buffers", 0) / (1024**3)
        cached_gb = getattr(mem, "cached", 0) / (1024**3)
    elif current_platform == "Darwin":  # macOS
        cached_gb = getattr(mem, "cached", 0) / (1024**3)

    # Process memory
    process = psutil.Process()
    process_mem = process.memory_info()

    # Swap memory
    swap = psutil.swap_memory()

    # Get cache sizes if tracker provided
    cache_sizes: dict[str, int] = {}
    if cache_tracker:
        for name, getter in cache_tracker.items():
            try:
                cache_sizes[name] = getter()
            except Exception as exc:
                log.debug("Failed to get cache size for %s: %s", name, exc)
                cache_sizes[name] = -1

    return MemoryStats(
        total_gb=mem.total / (1024**3),
        available_gb=mem.available / (1024**3),
        used_percent=mem.percent,
        process_rss_mb=process_mem.rss / (1024**2),
        process_vms_mb=process_mem.vms / (1024**2),
        process_percent=process.memory_percent(),
        swap_total_gb=swap.total / (1024**3),
        swap_used_percent=swap.percent,
        platform=current_platform,
        buffers_gb=buffers_gb,
        cached_gb=cached_gb,
        cache_sizes=cache_sizes,
    )


class MemoryMonitor(BaseBackgroundService):
    """
    Memory monitor with periodic checking and threshold warnings.

    Tracks memory usage over time and logs warnings when thresholds
    are exceeded. Supports LRU cache size tracking.

    Usage:
        monitor = MemoryMonitor(warning_threshold_percent=80.0)
        monitor.register_cache("chat_locks", lambda: len(cache))
        await monitor.start_periodic_check(interval_seconds=60.0)

        # Later...
        monitor.stop()
    """

    def __init__(
        self,
        warning_threshold_percent: float = DEFAULT_MEMORY_WARNING_THRESHOLD,
        critical_threshold_percent: float = 90.0,
    ) -> None:
        """
        Initialize the memory monitor.

        Args:
            warning_threshold_percent: Percentage at which to log warnings.
            critical_threshold_percent: Percentage at which to log errors.
        """
        super().__init__()
        self._warning_threshold = warning_threshold_percent
        self._critical_threshold = critical_threshold_percent
        self._cache_trackers: dict[str, Callable[[], int]] = {}
        self._last_stats: Optional[MemoryStats] = None
        self._peak_memory_percent: float = 0.0

    def register_cache(self, name: str, size_getter: Callable[[], int]) -> None:
        """
        Register an LRU cache for size tracking.

        Args:
            name: Human-readable name for the cache.
            size_getter: Function that returns current cache size.
        """
        self._cache_trackers[name] = size_getter
        log.debug("Registered cache tracker: %s", name)

    def unregister_cache(self, name: str) -> None:
        """Remove a cache from tracking."""
        self._cache_trackers.pop(name, None)
        log.debug("Unregistered cache tracker: %s", name)

    def get_stats(self) -> MemoryStats:
        """Get current memory statistics with cache sizes."""
        stats = get_memory_stats(cache_tracker=self._cache_trackers)
        self._last_stats = stats

        # Track peak memory
        if stats.used_percent > self._peak_memory_percent:
            self._peak_memory_percent = stats.used_percent

        return stats

    def check_thresholds(self, stats: Optional[MemoryStats] = None) -> dict[str, Any]:
        """
        Check if memory usage exceeds configured thresholds.

        Args:
            stats: Pre-computed stats, or None to fetch fresh stats.

        Returns:
            Dict with 'warning' and 'critical' flags and message.
        """
        if stats is None:
            stats = self.get_stats()

        result: dict[str, Any] = {
            "warning": False,
            "critical": False,
            "message": None,
            "stats": stats,
        }

        if stats.used_percent >= self._critical_threshold:
            result["critical"] = True
            result["message"] = (
                f"CRITICAL: Memory usage at {stats.used_percent:.1f}% "
                f"(threshold: {self._critical_threshold}%)"
            )
            log.error(
                "%s | Process RSS: %.1f MB | Available: %.2f GB",
                result["message"],
                stats.process_rss_mb,
                stats.available_gb,
            )
        elif stats.used_percent >= self._warning_threshold:
            result["warning"] = True
            result["message"] = (
                f"WARNING: Memory usage at {stats.used_percent:.1f}% "
                f"(threshold: {self._warning_threshold}%)"
            )
            log.warning(
                "%s | Process RSS: %.1f MB | Available: %.2f GB",
                result["message"],
                stats.process_rss_mb,
                stats.available_gb,
            )

        return result

    async def _run_loop(self) -> None:
        """Background task that checks memory periodically."""
        interval_seconds = getattr(self, '_interval', DEFAULT_MEMORY_CHECK_INTERVAL)
        log.info(
            "Memory monitor started (interval=%.1fs, warning_threshold=%.1f%%, critical_threshold=%.1f%%)",
            interval_seconds,
            self._warning_threshold,
            self._critical_threshold,
        )

        while self._running:
            try:
                stats = self.get_stats()
                self.check_thresholds(stats)

                # Log periodic info at debug level
                log.debug(
                    "Memory: %.1f%% used (%.2f GB available) | Process: %.1f MB RSS",
                    stats.used_percent,
                    stats.available_gb,
                    stats.process_rss_mb,
                )

                # Log cache sizes if any are registered
                if stats.cache_sizes:
                    log.debug("Cache sizes: %s", stats.cache_sizes)

            except Exception as exc:
                log.error("Memory check failed: %s", exc, exc_info=True)

            await asyncio.sleep(interval_seconds)

    def start_periodic_check(self, interval_seconds: float = DEFAULT_MEMORY_CHECK_INTERVAL) -> None:
        """
        Start periodic memory checking in the background.

        Args:
            interval_seconds: How often to check memory (default 60s).
        """
        self._interval = interval_seconds
        self.start()
        log.info("Memory monitor periodic check started")


    @property
    def peak_memory_percent(self) -> float:
        """Get the peak memory percentage observed."""
        return self._peak_memory_percent

    @property
    def last_stats(self) -> Optional[MemoryStats]:
        """Get the most recent memory stats."""
        return self._last_stats

    @property
    def is_running(self) -> bool:
        """Check if the monitor is actively running."""
        return self._running


def get_global_monitor(
    warning_threshold_percent: float = DEFAULT_MEMORY_WARNING_THRESHOLD,
    critical_threshold_percent: float = 90.0,
) -> MemoryMonitor:
    """
    Get or create the global memory monitor instance.

    Thread-safe singleton using get_or_create_singleton from utils.

    Args:
        warning_threshold_percent: Warning threshold percentage.
        critical_threshold_percent: Critical threshold percentage.

    Returns:
        The global MemoryMonitor instance.
    """
    return get_or_create_singleton(
        MemoryMonitor,
        warning_threshold_percent=warning_threshold_percent,
        critical_threshold_percent=critical_threshold_percent,
    )


def reset_global_monitor() -> None:
    """Reset the global memory monitor (useful for testing)."""
    reset_singleton(MemoryMonitor)


async def check_memory_health() -> dict[str, Any]:
    """
    Check memory health for the health endpoint.

    Returns a dict suitable for inclusion in HealthReport.

    Returns:
        Dict with memory status and stats.
    """
    from src.health import ComponentHealth, HealthStatus

    try:
        monitor = get_global_monitor()
        stats = monitor.get_stats()
        threshold_result = monitor.check_thresholds(stats)

        if threshold_result["critical"]:
            status = HealthStatus.UNHEALTHY
            message = threshold_result["message"] or "Memory usage critical"
        elif threshold_result["warning"]:
            status = HealthStatus.DEGRADED
            message = threshold_result["message"] or "Memory usage high"
        else:
            status = HealthStatus.HEALTHY
            message = f"Memory usage at {stats.used_percent:.1f}%"

        return {
            "component": ComponentHealth(
                name="memory",
                status=status,
                message=message,
            ),
            "stats": stats.to_dict(),
        }
    except ImportError:
        return {
            "component": ComponentHealth(
                name="memory",
                status=HealthStatus.DEGRADED,
                message="psutil not installed - memory monitoring unavailable",
            ),
            "stats": None,
        }
    except Exception as exc:
        log.error("Memory health check failed: %s", exc, exc_info=True)
        return {
            "component": ComponentHealth(
                name="memory",
                status=HealthStatus.DEGRADED,
                message=f"Memory check error: {type(exc).__name__}",
            ),
            "stats": None,
        }
