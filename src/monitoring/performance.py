"""
src/monitoring/performance.py — Performance metrics tracking for custombot.

Provides performance metrics tracking:
- Message processing latency
- LLM API latency
- Skill execution time
- Database operation times
- Queue depth monitoring
- Periodic metrics summary logging

Usage:
    from src.monitoring.performance import PerformanceMetrics, get_metrics_collector

    # Track performance metrics
    metrics = get_metrics_collector()
    metrics.track_message_latency(1.5)  # 1.5 seconds
    metrics.track_llm_latency(2.3)      # 2.3 seconds
    metrics.track_skill_time("bash", 0.8)
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Performance Metrics Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of samples to retain for latency calculations.
# Uses bounded deque to prevent unbounded memory growth.
METRICS_HISTORY_SIZE: int = 100

# Number of messages between periodic summary logs.
METRICS_SUMMARY_INTERVAL: int = 10

# Default interval for periodic metrics logging (seconds).
DEFAULT_METRICS_LOG_INTERVAL: float = 60.0


@dataclass
class LatencyStats:
    """
    Statistics for a latency metric.

    Provides min, max, mean, median, and percentile calculations
    for performance analysis.
    """

    count: int = 0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    median_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "count": self.count,
            "min_ms": round(self.min_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "mean_ms": round(self.mean_ms, 2),
            "median_ms": round(self.median_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
        }


@dataclass
class PerformanceSnapshot:
    """
    Point-in-time snapshot of performance metrics.

    Contains all tracked metrics at a specific moment,
    suitable for logging or health endpoint responses.
    """

    timestamp: float = field(default_factory=time.time)

    # Message processing metrics
    message_count: int = 0
    message_latency: LatencyStats = field(default_factory=LatencyStats)

    # LLM API metrics
    llm_call_count: int = 0
    llm_latency: LatencyStats = field(default_factory=LatencyStats)

    # Skill execution metrics
    skill_call_count: int = 0
    skill_latencies: dict[str, LatencyStats] = field(default_factory=dict)

    # Database operation metrics
    db_op_count: int = 0
    db_latency: LatencyStats = field(default_factory=LatencyStats)

    # Queue metrics
    queue_depth: int = 0
    queue_max_depth: int = 0

    # System metrics
    cpu_percent: float = 0.0
    memory_percent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "messages": {
                "count": self.message_count,
                "latency": self.message_latency.to_dict(),
            },
            "llm": {
                "call_count": self.llm_call_count,
                "latency": self.llm_latency.to_dict(),
            },
            "skills": {
                "call_count": self.skill_call_count,
                "latencies": {k: v.to_dict() for k, v in self.skill_latencies.items()},
            },
            "database": {
                "op_count": self.db_op_count,
                "latency": self.db_latency.to_dict(),
            },
            "queue": {
                "depth": self.queue_depth,
                "max_depth": self.queue_max_depth,
            },
            "system": {
                "cpu_percent": round(self.cpu_percent, 1),
                "memory_percent": round(self.memory_percent, 1),
            },
        }


def _calculate_latency_stats(samples: deque[float]) -> LatencyStats:
    """
    Calculate latency statistics from a deque of samples.

    Args:
        samples: Bounded deque of latency samples in milliseconds.

    Returns:
        LatencyStats with calculated metrics.
    """
    if not samples:
        return LatencyStats()

    sorted_samples = sorted(samples)
    count = len(sorted_samples)

    # Calculate percentiles
    def percentile(data: list[float], p: float) -> float:
        """Calculate the p-th percentile of sorted data."""
        if not data:
            return 0.0
        k = (len(data) - 1) * p / 100
        f = int(k)
        c = f + 1 if f + 1 < len(data) else f
        return data[f] + (k - f) * (data[c] - data[f])

    return LatencyStats(
        count=count,
        min_ms=sorted_samples[0],
        max_ms=sorted_samples[-1],
        mean_ms=statistics.mean(sorted_samples),
        median_ms=statistics.median(sorted_samples),
        p95_ms=percentile(sorted_samples, 95),
        p99_ms=percentile(sorted_samples, 99),
    )


class PerformanceMetrics:
    """
    Performance metrics collector with memory-efficient data structures.

    Tracks various performance metrics using bounded deques to prevent
    unbounded memory growth. Provides periodic summary logging and
    health endpoint integration.

    Memory Efficiency:
        - Uses deque with maxlen for bounded history
        - Lazy initialization of skill-specific trackers
        - Fixed memory footprint regardless of runtime

    Usage:
        metrics = PerformanceMetrics()
        metrics.track_message_latency(1.5)
        metrics.track_llm_latency(2.3)
        snapshot = metrics.get_snapshot()
    """

    def __init__(
        self,
        history_size: int = METRICS_HISTORY_SIZE,
        summary_interval: int = METRICS_SUMMARY_INTERVAL,
    ) -> None:
        """
        Initialize the performance metrics collector.

        Args:
            history_size: Maximum number of samples to retain per metric.
            summary_interval: Number of messages between summary logs.
        """
        self._history_size = history_size
        self._summary_interval = summary_interval

        # Bounded deques for latency tracking (memory-efficient)
        self._message_latencies: deque[float] = deque(maxlen=history_size)
        self._llm_latencies: deque[float] = deque(maxlen=history_size)
        self._db_latencies: deque[float] = deque(maxlen=history_size)

        # Per-skill latency tracking (lazy initialization)
        self._skill_latencies: dict[str, deque[float]] = {}

        # Counters
        self._message_count: int = 0
        self._llm_call_count: int = 0
        self._skill_call_count: int = 0
        self._db_op_count: int = 0

        # Queue depth tracking
        self._queue_depth: int = 0
        self._queue_max_depth: int = 0

        # Background logging task
        self._log_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

    # ── Metric Recording ─────────────────────────────────────────────────────

    def track_message_latency(self, latency_seconds: float) -> None:
        """
        Record a message processing latency.

        Args:
            latency_seconds: Time taken to process the message.
        """
        latency_ms = latency_seconds * 1000
        self._message_latencies.append(latency_ms)
        self._message_count += 1

        # Log summary at interval
        if self._message_count % self._summary_interval == 0:
            self._log_summary()

    def track_llm_latency(self, latency_seconds: float) -> None:
        """
        Record an LLM API call latency.

        Args:
            latency_seconds: Time taken for the LLM call.
        """
        latency_ms = latency_seconds * 1000
        self._llm_latencies.append(latency_ms)
        self._llm_call_count += 1

    def track_skill_time(self, skill_name: str, latency_seconds: float) -> None:
        """
        Record a skill execution time.

        Args:
            skill_name: Name of the executed skill.
            latency_seconds: Time taken to execute the skill.
        """
        latency_ms = latency_seconds * 1000

        # Lazy initialization of per-skill deque
        if skill_name not in self._skill_latencies:
            self._skill_latencies[skill_name] = deque(maxlen=self._history_size)

        self._skill_latencies[skill_name].append(latency_ms)
        self._skill_call_count += 1

    def track_db_time(self, latency_seconds: float) -> None:
        """
        Record a database operation time.

        Args:
            latency_seconds: Time taken for the database operation.
        """
        latency_ms = latency_seconds * 1000
        self._db_latencies.append(latency_ms)
        self._db_op_count += 1

    def update_queue_depth(self, depth: int) -> None:
        """
        Update the current queue depth.

        Args:
            depth: Current number of items in the queue.
        """
        self._queue_depth = depth
        if depth > self._queue_max_depth:
            self._queue_max_depth = depth

    # ── Snapshot & Reporting ─────────────────────────────────────────────────

    def get_snapshot(self, include_system: bool = True) -> PerformanceSnapshot:
        """
        Get a point-in-time snapshot of all metrics.

        Args:
            include_system: Whether to include CPU/memory stats.

        Returns:
            PerformanceSnapshot with current metrics.
        """
        snapshot = PerformanceSnapshot(
            message_count=self._message_count,
            message_latency=_calculate_latency_stats(self._message_latencies),
            llm_call_count=self._llm_call_count,
            llm_latency=_calculate_latency_stats(self._llm_latencies),
            skill_call_count=self._skill_call_count,
            skill_latencies={
                name: _calculate_latency_stats(samples)
                for name, samples in self._skill_latencies.items()
            },
            db_op_count=self._db_op_count,
            db_latency=_calculate_latency_stats(self._db_latencies),
            queue_depth=self._queue_depth,
            queue_max_depth=self._queue_max_depth,
        )

        # Add system metrics if requested
        if include_system:
            try:
                import psutil

                snapshot.cpu_percent = psutil.cpu_percent(interval=0.1)
                snapshot.memory_percent = psutil.virtual_memory().percent
            except (ImportError, Exception):
                pass

        return snapshot

    def _log_summary(self) -> None:
        """Log a structured summary of current metrics."""
        snapshot = self.get_snapshot(include_system=True)

        # Log in structured format for aggregation
        log.info(
            "Performance summary | messages=%d | msg_latency_p95=%.1fms | "
            "llm_calls=%d | llm_latency_p95=%.1fms | skills=%d | "
            "db_ops=%d | db_latency_p95=%.1fms | queue=%d | cpu=%.1f%% | mem=%.1f%%",
            snapshot.message_count,
            snapshot.message_latency.p95_ms,
            snapshot.llm_call_count,
            snapshot.llm_latency.p95_ms,
            snapshot.skill_call_count,
            snapshot.db_op_count,
            snapshot.db_latency.p95_ms,
            snapshot.queue_depth,
            snapshot.cpu_percent,
            snapshot.memory_percent,
            extra={
                "metrics_type": "summary",
                "message_count": snapshot.message_count,
                "message_latency_ms": snapshot.message_latency.to_dict(),
                "llm_call_count": snapshot.llm_call_count,
                "llm_latency_ms": snapshot.llm_latency.to_dict(),
                "skill_call_count": snapshot.skill_call_count,
                "db_op_count": snapshot.db_op_count,
                "db_latency_ms": snapshot.db_latency.to_dict(),
                "queue_depth": snapshot.queue_depth,
                "cpu_percent": snapshot.cpu_percent,
                "memory_percent": snapshot.memory_percent,
            },
        )

    # ── Periodic Logging ─────────────────────────────────────────────────────

    async def _periodic_log(self, interval_seconds: float) -> None:
        """Background task for periodic metrics logging."""
        log.info(
            "Performance metrics logging started (interval=%.1fs)",
            interval_seconds,
        )

        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                self._log_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Metrics logging error: %s", e, exc_info=True)

    def start_periodic_logging(
        self, interval_seconds: float = DEFAULT_METRICS_LOG_INTERVAL
    ) -> None:
        """
        Start periodic metrics logging in the background.

        Args:
            interval_seconds: How often to log metrics (default 60s).
        """
        if self._running:
            log.warning("Performance metrics logging already running")
            return

        self._running = True
        self._log_task = asyncio.create_task(self._periodic_log(interval_seconds))

    async def stop(self) -> None:
        """Stop periodic metrics logging."""
        self._running = False
        if self._log_task:
            self._log_task.cancel()
            try:
                await self._log_task
            except asyncio.CancelledError:
                pass
            self._log_task = None
        log.info("Performance metrics logging stopped")

    @property
    def is_running(self) -> bool:
        """Check if periodic logging is active."""
        return self._running


# Global performance metrics instance (lazy-initialized)
_global_metrics: Optional[PerformanceMetrics] = None


def get_metrics_collector(
    history_size: int = METRICS_HISTORY_SIZE,
    summary_interval: int = METRICS_SUMMARY_INTERVAL,
) -> PerformanceMetrics:
    """
    Get or create the global performance metrics collector.

    Args:
        history_size: Maximum samples to retain per metric.
        summary_interval: Messages between summary logs.

    Returns:
        The global PerformanceMetrics instance.
    """
    global _global_metrics
    if _global_metrics is None:
        _global_metrics = PerformanceMetrics(
            history_size=history_size,
            summary_interval=summary_interval,
        )
    return _global_metrics


async def check_performance_health() -> dict[str, Any]:
    """
    Check performance health for the health endpoint.

    Returns a dict suitable for inclusion in HealthReport.

    Returns:
        Dict with performance status and metrics snapshot.
    """
    from src.health import ComponentHealth, HealthStatus

    try:
        metrics = get_metrics_collector()
        snapshot = metrics.get_snapshot(include_system=True)

        # Determine health status based on latency thresholds
        status = HealthStatus.HEALTHY
        messages: list[str] = []

        # Check message latency (warn if p95 > 5s)
        if snapshot.message_latency.p95_ms > 5000:
            status = HealthStatus.DEGRADED
            messages.append(
                f"High message latency: {snapshot.message_latency.p95_ms:.0f}ms p95"
            )

        # Check LLM latency (warn if p95 > 30s)
        if snapshot.llm_latency.p95_ms > 30000:
            status = HealthStatus.DEGRADED
            messages.append(
                f"High LLM latency: {snapshot.llm_latency.p95_ms:.0f}ms p95"
            )

        # Check system resources (degraded if memory > 90%)
        if snapshot.memory_percent > 90:
            status = HealthStatus.DEGRADED
            messages.append(f"High memory usage: {snapshot.memory_percent:.1f}%")

        message = (
            "; ".join(messages) if messages else "Performance within normal parameters"
        )

        return {
            "component": ComponentHealth(
                name="performance",
                status=status,
                message=message,
            ),
            "metrics": snapshot.to_dict(),
        }
    except Exception as e:
        log.error("Performance health check failed: %s", e, exc_info=True)
        return {
            "component": ComponentHealth(
                name="performance",
                status=HealthStatus.DEGRADED,
                message=f"Performance check error: {type(e).__name__}",
            ),
            "metrics": None,
        }
