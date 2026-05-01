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
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

from src.utils.singleton import get_or_create_singleton, reset_singleton

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Session Metrics
# ─────────────────────────────────────────────────────────────────────────────


class SessionMetrics:
    """Session metrics counter for tracking bot activity.

    Note: Relies on asyncio's single-threaded event loop for safety.
    Not safe for use from multiple OS threads.
    """

    __slots__ = ("start_time", "_messages", "_skills", "_errors")

    def __init__(self) -> None:
        self.start_time = time.time()
        self._messages = 0
        self._skills = 0
        self._errors = 0

    @property
    def messages_processed(self) -> int:
        return self._messages

    @property
    def skills_executed(self) -> int:
        return self._skills

    @property
    def errors_count(self) -> int:
        return self._errors

    def increment_messages(self) -> None:
        self._messages += 1

    def increment_skills(self) -> None:
        self._skills += 1

    def increment_errors(self) -> None:
        self._errors += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_time": self.start_time,
            "uptime": time.time() - self.start_time,
            "messages_processed": self._messages,
            "skills_executed": self._skills,
            "errors_count": self._errors,
        }

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

# TTL for cached system metrics (seconds). Avoids blocking psutil calls on
# every snapshot; refresh happens via the async ``refresh_system_metrics()``
# method driven by periodic logging and health-check callers.
SYSTEM_METRICS_TTL: float = 30.0

# Sliding-window sizes (seconds) for error rate tracking.
ERROR_WINDOW_SECONDS: tuple[int, ...] = (300, 900, 3600)  # 5m, 15m, 60m

# Fixed bucket boundaries (milliseconds) for the LLM latency histogram.
# Prometheus histograms use cumulative counts per bucket, enabling
# server-side percentile computation over arbitrary time windows.
LLM_LATENCY_HISTOGRAM_BUCKETS_MS: tuple[float, ...] = (
    500.0, 1000.0, 2000.0, 5000.0, 10000.0, 30000.0, 60000.0, 120000.0,
)

# Fixed bucket boundaries (milliseconds) for the DB write latency histogram.
# DB writes (JSONL appends, index updates) are typically sub-100ms; buckets
# are tuned for the fast write path rather than the slower LLM call path.
DB_WRITE_LATENCY_HISTOGRAM_BUCKETS_MS: tuple[float, ...] = (
    5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0,
)


@dataclass(slots=True)
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


@dataclass(slots=True)
class SkillMetrics:
    """Per-skill execution metrics: calls, successes, errors, and error types."""

    calls: int = 0
    successes: int = 0
    errors: int = 0
    error_types: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "successes": self.successes,
            "errors": self.errors,
            "error_rate": round(self.errors / self.calls, 4) if self.calls else 0.0,
            "error_types": dict(self.error_types),
        }


@dataclass(slots=True)
class SkillTimeoutRatio:
    """Per-skill timeout ratio tracking (actual_time / declared_timeout).

    A ratio near 1.0 indicates the skill is consistently approaching
    its declared timeout and may need optimization or a higher limit.
    """

    count: int = 0
    max_ratio: float = 0.0
    mean_ratio: float = 0.0
    p95_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "max_ratio": round(self.max_ratio, 4),
            "mean_ratio": round(self.mean_ratio, 4),
            "p95_ratio": round(self.p95_ratio, 4),
        }


@dataclass(slots=True)
class OversizedArgsSizeStats:
    """Per-skill oversized argument size distribution.

    Tracks the count, minimum, maximum, and cumulative total bytes of
    rejected oversized argument payloads so operators can identify which
    skills are being abused or misconfigured.
    """

    count: int = 0
    min_bytes: int = 0
    max_bytes: int = 0
    total_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "min_bytes": self.min_bytes,
            "max_bytes": self.max_bytes,
            "total_bytes": self.total_bytes,
            "avg_bytes": round(self.total_bytes / self.count, 1) if self.count else 0.0,
        }


@dataclass(slots=True)
class ChatMessageCount:
    """Per-chat message count entry for top-chats reporting."""

    chat_id: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "message_count": self.message_count}


@dataclass(slots=True)
class ChatConversationDepth:
    """Per-chat conversation depth (last ReAct iteration count)."""

    chat_id: str
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "depth": self.depth}


# Maximum number of chats tracked for per-chat message counting.
DEFAULT_MAX_TRACKED_CHATS: int = 1000

# Default number of top chats returned in snapshots.
DEFAULT_TOP_CHATS: int = 10


@dataclass(slots=True)
class ErrorWindowStats:
    """Error count and rate within a sliding time window."""

    window_seconds: int
    error_count: int = 0
    error_rate_per_minute: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_seconds": self.window_seconds,
            "error_count": self.error_count,
            "error_rate_per_minute": round(self.error_rate_per_minute, 4),
        }


@dataclass(slots=True)
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
    llm_latency_histogram: dict[str, Any] = field(default_factory=dict)

    # Skill execution metrics
    skill_call_count: int = 0
    skill_latencies: dict[str, LatencyStats] = field(default_factory=dict)
    skill_metrics: dict[str, SkillMetrics] = field(default_factory=dict)
    skill_timeout_ratios: dict[str, SkillTimeoutRatio] = field(default_factory=dict)

    # Database operation metrics
    db_op_count: int = 0
    db_latency: LatencyStats = field(default_factory=LatencyStats)

    # Database write operation metrics (separate from reads)
    db_write_op_count: int = 0
    db_write_latency: LatencyStats = field(default_factory=LatencyStats)
    db_write_latency_histogram: dict[str, Any] = field(default_factory=dict)

    # ReAct loop iteration metrics
    react_iteration_count: int = 0
    react_iterations: LatencyStats = field(default_factory=LatencyStats)
    react_iterations_total: int = 0

    # Context budget utilization (ratio of used tokens to max budget)
    context_budget_count: int = 0
    context_budget_mean_ratio: float = 0.0
    context_budget_max_ratio: float = 0.0
    context_budget_p95_ratio: float = 0.0

    # Queue metrics
    queue_depth: int = 0
    queue_max_depth: int = 0

    # Active chat tracking
    active_chat_count: int = 0
    top_chats: list[ChatMessageCount] = field(default_factory=list)
    top_chat_depths: list[ChatConversationDepth] = field(default_factory=list)

    # Memory cache effectiveness
    memory_cache_hits: int = 0
    memory_cache_misses: int = 0

    # Outbound message dedup
    outbound_dedup_hits: int = 0
    outbound_dedup_misses: int = 0

    # Compression summary usage
    compression_summary_used_total: int = 0

    # Embedding cache effectiveness
    embed_cache_hits: int = 0
    embed_cache_misses: int = 0

    # Per-skill oversized argument rejection counts
    skill_oversized_args: dict[str, int] = field(default_factory=dict)

    # Per-skill oversized argument size distribution
    skill_oversized_args_sizes: dict[str, OversizedArgsSizeStats] = field(default_factory=dict)

    # LLM error classification counter (error_code → count)
    llm_error_classifications: dict[str, int] = field(default_factory=dict)

    # System metrics
    cpu_percent: float = 0.0
    memory_percent: float = 0.0

    # Sliding-window error rates
    error_windows: list[ErrorWindowStats] = field(default_factory=list)
    total_error_count: int = 0

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
                "histogram": self.llm_latency_histogram,
                "error_classifications": dict(self.llm_error_classifications),
            },
            "skills": {
                "call_count": self.skill_call_count,
                "latencies": {k: v.to_dict() for k, v in self.skill_latencies.items()},
                "per_skill": {k: v.to_dict() for k, v in self.skill_metrics.items()},
                "timeout_ratios": {
                    k: v.to_dict() for k, v in self.skill_timeout_ratios.items()
                },
            },
            "database": {
                "op_count": self.db_op_count,
                "latency": self.db_latency.to_dict(),
                "write_op_count": self.db_write_op_count,
                "write_latency": self.db_write_latency.to_dict(),
                "write_histogram": self.db_write_latency_histogram,
            },
            "react_iterations": {
                "count": self.react_iteration_count,
                "stats": self.react_iterations.to_dict(),
                "total": self.react_iterations_total,
            },
            "context_budget": {
                "count": self.context_budget_count,
                "mean_ratio": round(self.context_budget_mean_ratio, 4),
                "max_ratio": round(self.context_budget_max_ratio, 4),
                "p95_ratio": round(self.context_budget_p95_ratio, 4),
            },
            "queue": {
                "depth": self.queue_depth,
                "max_depth": self.queue_max_depth,
            },
            "active_chats": self.active_chat_count,
            "top_chats": [c.to_dict() for c in self.top_chats],
            "top_chat_depths": [d.to_dict() for d in self.top_chat_depths],
            "memory_cache": {
                "hits": self.memory_cache_hits,
                "misses": self.memory_cache_misses,
                "hit_ratio": round(
                    self.memory_cache_hits / (self.memory_cache_hits + self.memory_cache_misses), 4
                ) if (self.memory_cache_hits + self.memory_cache_misses) > 0 else 0.0,
            },
            "outbound_dedup": {
                "hits": self.outbound_dedup_hits,
                "misses": self.outbound_dedup_misses,
                "hit_ratio": round(
                    self.outbound_dedup_hits
                    / (self.outbound_dedup_hits + self.outbound_dedup_misses),
                    4,
                )
                if (self.outbound_dedup_hits + self.outbound_dedup_misses) > 0
                else 0.0,
            },
            "compression_summary_used_total": self.compression_summary_used_total,
            "embed_cache": {
                "hits": self.embed_cache_hits,
                "misses": self.embed_cache_misses,
                "hit_ratio": round(
                    self.embed_cache_hits / (self.embed_cache_hits + self.embed_cache_misses), 4
                ) if (self.embed_cache_hits + self.embed_cache_misses) > 0 else 0.0,
            },
            "skill_oversized_args": dict(self.skill_oversized_args),
            "skill_oversized_args_sizes": {
                k: v.to_dict() for k, v in self.skill_oversized_args_sizes.items()
            },
            "system": {
                "cpu_percent": round(self.cpu_percent, 1),
                "memory_percent": round(self.memory_percent, 1),
            },
            "error_rates": {
                "total_errors": self.total_error_count,
                "error_rate_5m": self.error_windows[0].error_rate_per_minute if len(self.error_windows) > 0 else 0.0,
                "error_rate_15m": self.error_windows[1].error_rate_per_minute if len(self.error_windows) > 1 else 0.0,
                "error_rate_60m": self.error_windows[2].error_rate_per_minute if len(self.error_windows) > 2 else 0.0,
                "windows": [w.to_dict() for w in self.error_windows],
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


def _calculate_timeout_ratio(samples: deque[float]) -> SkillTimeoutRatio:
    """Compute timeout-ratio statistics from a deque of ratio samples."""
    if not samples:
        return SkillTimeoutRatio()
    data = sorted(samples)
    count = len(data)

    def pct(p: float) -> float:
        k = (count - 1) * p / 100
        f = int(k)
        c = f + 1 if f + 1 < count else f
        return data[f] + (k - f) * (data[c] - data[f])

    return SkillTimeoutRatio(
        count=count,
        max_ratio=data[-1],
        mean_ratio=statistics.mean(data),
        p95_ratio=pct(95),
    )


def _percentile(sorted_data: list[float], p: float) -> float:
    """Compute the p-th percentile of *sorted* data (0–100 scale)."""
    if not sorted_data:
        return 0.0
    count = len(sorted_data)
    k = (count - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < count else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


class LatencyHistogram:
    """Fixed-bucket histogram for Prometheus exposition of latency distributions.

    Each bucket tracks the count of observations that fall within its range.
    ``cumulative_buckets()`` produces the cumulative ``le``-labelled output
    that Prometheus expects, plus a ``+Inf`` sentinel bucket.
    """

    __slots__ = ("_bounds", "_counts", "_overflow", "_sum", "_total")

    def __init__(self, bounds_ms: tuple[float, ...]) -> None:
        self._bounds = bounds_ms
        self._counts: list[int] = [0] * len(bounds_ms)
        self._overflow: int = 0
        self._sum: float = 0.0
        self._total: int = 0

    def observe(self, value_ms: float) -> None:
        """Record a single observation into the appropriate bucket."""
        self._total += 1
        self._sum += value_ms
        for i, bound in enumerate(self._bounds):
            if value_ms <= bound:
                self._counts[i] += 1
                return
        self._overflow += 1

    @property
    def count(self) -> int:
        return self._total

    @property
    def sum_ms(self) -> float:
        return self._sum

    def cumulative_buckets(self) -> list[tuple[str, int]]:
        """Return ``(le_label, cumulative_count)`` pairs including ``+Inf``."""
        result: list[tuple[str, int]] = []
        cumulative = 0
        for bound, cnt in zip(self._bounds, self._counts):
            cumulative += cnt
            # Format: drop trailing ".0" for integer-valued bounds
            le_label = str(int(bound)) if bound == int(bound) else str(bound)
            result.append((le_label, cumulative))
        cumulative += self._overflow
        result.append(("+Inf", cumulative))
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": {le: cnt for le, cnt in self.cumulative_buckets()},
            "count": self._total,
            "sum_ms": round(self._sum, 2),
        }


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

        # DB write latency histogram (separate from general DB latency)
        self._db_write_latency_histogram = LatencyHistogram(
            DB_WRITE_LATENCY_HISTOGRAM_BUCKETS_MS
        )
        self._db_write_latencies: deque[float] = deque(maxlen=history_size)

        # Fixed-bucket histogram for LLM latency Prometheus exposition
        self._llm_latency_histogram = LatencyHistogram(LLM_LATENCY_HISTOGRAM_BUCKETS_MS)

        # ReAct loop iteration counts per conversation
        self._react_iteration_counts: deque[float] = deque(maxlen=history_size)

        # Cumulative total of ReAct loop iterations across all conversations
        self._react_iterations_total: int = 0

        # Context token-budget utilization ratios (used_tokens / max_budget)
        self._context_budget_ratios: deque[float] = deque(maxlen=history_size)

        # Per-skill latency tracking (lazy initialization)
        self._skill_latencies: dict[str, deque[float]] = {}

        # Per-skill execution metrics registry
        self._skill_metrics: dict[str, SkillMetrics] = {}

        # Per-skill timeout ratio tracking (actual_time / timeout_seconds)
        self._skill_timeout_ratios: dict[str, deque[float]] = {}

        # Per-skill oversized argument tracking (skill_name → count)
        self._skill_oversized_args: dict[str, int] = {}

        # Per-skill oversized argument size distribution (skill_name → stats)
        self._skill_oversized_args_sizes: dict[str, OversizedArgsSizeStats] = {}

        # LLM error classification counter (error_code → count)
        self._llm_error_counts: dict[str, int] = {}

        # Counters
        self._message_count: int = 0
        self._llm_call_count: int = 0
        self._skill_call_count: int = 0
        self._db_op_count: int = 0
        self._db_write_op_count: int = 0

        # Memory cache effectiveness counters
        self._memory_cache_hits: int = 0
        self._memory_cache_misses: int = 0

        # Outbound message dedup counters
        self._outbound_dedup_hits: int = 0
        self._outbound_dedup_misses: int = 0

        # Compression summary usage counter
        self._compression_summary_used_total: int = 0

        # Embedding cache effectiveness counters
        self._embed_cache_hits: int = 0
        self._embed_cache_misses: int = 0

        # Sliding-window error tracking (deque of timestamps per window)
        self._total_error_count: int = 0
        self._error_timestamps: deque[float] = deque(maxlen=10_000)

        # Queue depth tracking
        self._queue_depth: int = 0
        self._queue_max_depth: int = 0

        # Active chat tracking
        self._active_chat_count: int = 0

        # Per-chat message count tracking (bounded LRU)
        self._max_tracked_chats: int = DEFAULT_MAX_TRACKED_CHATS
        self._chat_message_counts: OrderedDict[str, int] = OrderedDict()

        # Per-chat conversation depth tracking (bounded LRU)
        self._chat_conversation_depths: OrderedDict[str, int] = OrderedDict()

        # Cached system metrics (refreshed async via refresh_system_metrics)
        self._cpu_percent: float = 0.0
        self._memory_percent: float = 0.0
        self._last_system_refresh: float = 0.0

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
        self._llm_latency_histogram.observe(latency_ms)
        self._llm_call_count += 1

    def track_react_iterations(self, count: int) -> None:
        """Record the number of ReAct loop iterations for a conversation."""
        self._react_iteration_counts.append(float(count))
        self._react_iterations_total += count

    def track_context_budget_utilization(
        self, used_tokens: int, budget: int
    ) -> None:
        """Record the ratio of tokens used to the max budget on a context build.

        A ratio near 1.0 indicates the context is hitting the budget ceiling
        and the chat may need compression or higher limits.
        """
        if budget <= 0:
            return
        ratio = used_tokens / budget
        self._context_budget_ratios.append(ratio)

    def track_db_latency(self, latency_seconds: float) -> None:
        """
        Record a database operation latency.

        Args:
            latency_seconds: Time taken for the DB operation.
        """
        latency_ms = latency_seconds * 1000
        self._db_latencies.append(latency_ms)
        self._db_op_count += 1

    def track_db_write_latency(self, latency_seconds: float) -> None:
        """Record a database write operation latency (JSONL append, index update).

        Tracked separately from general DB latency so operators can set alerts
        specifically on slow writes without noise from read operations.

        Args:
            latency_seconds: Time taken for the DB write operation.
        """
        latency_ms = latency_seconds * 1000
        self._db_write_latencies.append(latency_ms)
        self._db_write_latency_histogram.observe(latency_ms)
        self._db_write_op_count += 1

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

    def track_skill_success(self, skill_name: str) -> None:
        """Record a successful skill execution."""
        m = self._skill_metrics.setdefault(skill_name, SkillMetrics())
        m.calls += 1
        m.successes += 1

    def track_skill_error(self, skill_name: str, error_type: str) -> None:
        """Record a failed skill execution with its error type."""
        m = self._skill_metrics.setdefault(skill_name, SkillMetrics())
        m.calls += 1
        m.errors += 1
        m.error_types[error_type] = m.error_types.get(error_type, 0) + 1

    def track_llm_error(self, error_code: str) -> None:
        """Record an LLM error by its classified error code.

        Enables operators to set targeted alerts on specific error
        categories (e.g., alert on ``ERR_1001`` within 5 minutes).

        Args:
            error_code: The :class:`ErrorCode` value string (e.g. ``"ERR_1001"``).
        """
        self._llm_error_counts[error_code] = (
            self._llm_error_counts.get(error_code, 0) + 1
        )

    def track_skill_args_oversized(self, skill_name: str, arg_size_bytes: int) -> None:
        """Record a rejected skill call due to oversized arguments.

        Args:
            skill_name: Name of the skill that received oversized args.
            arg_size_bytes: Size of the raw argument payload in bytes.
        """
        self._skill_oversized_args[skill_name] = (
            self._skill_oversized_args.get(skill_name, 0) + 1
        )

        # Update size distribution (min/max/total)
        stats = self._skill_oversized_args_sizes.get(skill_name)
        if stats is None:
            stats = OversizedArgsSizeStats(
                count=1,
                min_bytes=arg_size_bytes,
                max_bytes=arg_size_bytes,
                total_bytes=arg_size_bytes,
            )
            self._skill_oversized_args_sizes[skill_name] = stats
        else:
            stats.count += 1
            stats.min_bytes = min(stats.min_bytes, arg_size_bytes)
            stats.max_bytes = max(stats.max_bytes, arg_size_bytes)
            stats.total_bytes += arg_size_bytes

    def track_skill_timeout_ratio(
        self, skill_name: str, actual_seconds: float, timeout_seconds: float
    ) -> None:
        """Record the ratio of actual execution time to declared skill timeout.

        A ratio near 1.0 signals the skill is approaching its timeout limit.
        """
        if timeout_seconds <= 0:
            return
        ratio = actual_seconds / timeout_seconds
        if skill_name not in self._skill_timeout_ratios:
            self._skill_timeout_ratios[skill_name] = deque(
                maxlen=self._history_size
            )
        self._skill_timeout_ratios[skill_name].append(ratio)

    def update_queue_depth(self, depth: int) -> None:
        """
        Update the current queue depth.

        Args:
            depth: Current number of items in the queue.
        """
        self._queue_depth = depth
        if depth > self._queue_max_depth:
            self._queue_max_depth = depth

    def update_active_chat_count(self, count: int) -> None:
        """
        Update the current active chat count.

        Args:
            count: Current number of active chats.
        """
        self._active_chat_count = count

    def track_chat_message(self, chat_id: str) -> None:
        """Increment the message count for a specific chat (bounded LRU).

        When the max tracked chats limit is reached, the least-recently-used
        entries are evicted to keep memory bounded.
        """
        if chat_id in self._chat_message_counts:
            self._chat_message_counts[chat_id] += 1
            self._chat_message_counts.move_to_end(chat_id)
            return

        # Evict oldest half when at capacity (LRU eviction)
        if len(self._chat_message_counts) >= self._max_tracked_chats:
            for _ in range(len(self._chat_message_counts) // 2):
                self._chat_message_counts.popitem(last=False)

        self._chat_message_counts[chat_id] = 1

    def get_top_chats(self, n: int = DEFAULT_TOP_CHATS) -> list[ChatMessageCount]:
        """Return the top-N chats by message count, descending."""
        sorted_chats = sorted(
            self._chat_message_counts.items(), key=lambda item: item[1], reverse=True
        )
        return [ChatMessageCount(chat_id=cid, message_count=cnt) for cid, cnt in sorted_chats[:n]]

    def track_memory_cache_hit(self) -> None:
        """Record a memory cache hit (file mtime unchanged, reused cached content)."""
        self._memory_cache_hits += 1

    def track_memory_cache_miss(self) -> None:
        """Record a memory cache miss (file changed or not yet cached)."""
        self._memory_cache_misses += 1

    def track_outbound_dedup_hit(self) -> None:
        """Record an outbound message dedup cache hit (duplicate suppressed)."""
        self._outbound_dedup_hits += 1

    def track_outbound_dedup_miss(self) -> None:
        """Record an outbound message dedup cache miss (new message allowed)."""
        self._outbound_dedup_misses += 1

    def track_compression_summary_used(self) -> None:
        """Record that a compressed conversation summary was used during context assembly.

        Incremented only when ``get_compressed_summary()`` returns a non-None
        value, indicating that the chat's history has been compressed and the
        summary was injected into the LLM context.
        """
        self._compression_summary_used_total += 1

    def track_embed_cache_hit(self) -> None:
        """Record an embedding cache hit (text already cached, API call avoided)."""
        self._embed_cache_hits += 1

    def track_embed_cache_miss(self) -> None:
        """Record an embedding cache miss (text not in cache, API call required)."""
        self._embed_cache_misses += 1

    def track_error(self) -> None:
        """Record an error with timestamp for sliding-window rate tracking."""
        self._total_error_count += 1
        self._error_timestamps.append(time.time())

    def _get_error_window_counts(self) -> list[ErrorWindowStats]:
        """Count errors and compute rates for each sliding window, pruning old timestamps."""
        now = time.time()
        timestamps = self._error_timestamps
        results: list[ErrorWindowStats] = []
        # Prune timestamps older than the largest window
        cutoff = now - ERROR_WINDOW_SECONDS[-1]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        for window_secs in ERROR_WINDOW_SECONDS:
            window_cutoff = now - window_secs
            count = sum(1 for ts in timestamps if ts >= window_cutoff)
            rate = count / (window_secs / 60)  # errors per minute
            results.append(ErrorWindowStats(
                window_seconds=window_secs,
                error_count=count,
                error_rate_per_minute=rate,
            ))
        return results

    def track_conversation_depth(self, chat_id: str, depth: int) -> None:
        """Record the last ReAct iteration count for a specific chat (bounded LRU)."""
        self._chat_conversation_depths[chat_id] = depth
        self._chat_conversation_depths.move_to_end(chat_id)
        # Evict oldest half when at capacity
        if len(self._chat_conversation_depths) > self._max_tracked_chats:
            for _ in range(len(self._chat_conversation_depths) // 2):
                self._chat_conversation_depths.popitem(last=False)

    def get_top_chat_depths(self, n: int = DEFAULT_TOP_CHATS) -> list[ChatConversationDepth]:
        """Return the top-N chats by conversation depth, descending."""
        sorted_chats = sorted(
            self._chat_conversation_depths.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return [ChatConversationDepth(chat_id=cid, depth=d) for cid, d in sorted_chats[:n]]

    # ── System Metrics (cached, async-refreshed) ─────────────────────────────

    def _read_system_metrics_sync(self) -> tuple[float, float]:
        """Read CPU and memory percentages synchronously.

        Uses ``interval=None`` so the call is non-blocking (returns the
        utilisation since the last call, or 0.0 on the first invocation).
        """
        try:
            import psutil

            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            return cpu, mem
        except (ImportError, Exception):
            return 0.0, 0.0

    async def refresh_system_metrics(self) -> None:
        """Refresh cached CPU/memory metrics off the event loop.

        Calls psutil in a thread pool so the event loop is never blocked.
        Results are cached and reused by ``get_snapshot()`` until the TTL
        expires.
        """
        cpu, mem = await asyncio.to_thread(self._read_system_metrics_sync)
        self._cpu_percent = cpu
        self._memory_percent = mem
        self._last_system_refresh = time.time()

    def _is_system_cache_valid(self) -> bool:
        """Return True if the cached system metrics are within TTL."""
        return (time.time() - self._last_system_refresh) < SYSTEM_METRICS_TTL

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
            llm_latency_histogram=self._llm_latency_histogram.to_dict(),
            skill_call_count=self._skill_call_count,
            skill_latencies={
                name: _calculate_latency_stats(samples)
                for name, samples in self._skill_latencies.items()
            },
            skill_metrics={
                name: SkillMetrics(
                    calls=m.calls,
                    successes=m.successes,
                    errors=m.errors,
                    error_types=dict(m.error_types),
                )
                for name, m in self._skill_metrics.items()
            },
            skill_timeout_ratios={
                name: _calculate_timeout_ratio(samples)
                for name, samples in self._skill_timeout_ratios.items()
            },
            db_op_count=self._db_op_count,
            db_latency=_calculate_latency_stats(self._db_latencies),
            db_write_op_count=self._db_write_op_count,
            db_write_latency=_calculate_latency_stats(self._db_write_latencies),
            db_write_latency_histogram=self._db_write_latency_histogram.to_dict(),
            react_iteration_count=len(self._react_iteration_counts),
            react_iterations=_calculate_latency_stats(self._react_iteration_counts),
            react_iterations_total=self._react_iterations_total,
            context_budget_count=len(self._context_budget_ratios),
            context_budget_mean_ratio=statistics.mean(self._context_budget_ratios)
            if self._context_budget_ratios
            else 0.0,
            context_budget_max_ratio=max(self._context_budget_ratios)
            if self._context_budget_ratios
            else 0.0,
            context_budget_p95_ratio=_percentile(list(self._context_budget_ratios), 95)
            if self._context_budget_ratios
            else 0.0,
            queue_depth=self._queue_depth,
            queue_max_depth=self._queue_max_depth,
            active_chat_count=self._active_chat_count,
            top_chats=self.get_top_chats(),
            top_chat_depths=self.get_top_chat_depths(),
            memory_cache_hits=self._memory_cache_hits,
            memory_cache_misses=self._memory_cache_misses,
            outbound_dedup_hits=self._outbound_dedup_hits,
            outbound_dedup_misses=self._outbound_dedup_misses,
            compression_summary_used_total=self._compression_summary_used_total,
            embed_cache_hits=self._embed_cache_hits,
            embed_cache_misses=self._embed_cache_misses,
            skill_oversized_args=dict(self._skill_oversized_args),
            skill_oversized_args_sizes={
                name: OversizedArgsSizeStats(
                    count=s.count,
                    min_bytes=s.min_bytes,
                    max_bytes=s.max_bytes,
                    total_bytes=s.total_bytes,
                )
                for name, s in self._skill_oversized_args_sizes.items()
            },
            llm_error_classifications=dict(self._llm_error_counts),
            error_windows=self._get_error_window_counts(),
            total_error_count=self._total_error_count,
        )

        # Use cached system metrics (refreshed async via refresh_system_metrics).
        # Never call psutil synchronously here — even interval=None involves
        # blocking I/O syscalls that stall the event loop.  Async callers
        # (health endpoint, periodic logger) always call refresh_system_metrics()
        # before get_snapshot(), so the cache is fresh.  The only sync caller is
        # _log_summary() via track_message_latency(), where stale/zero values
        # are acceptable.
        if include_system:
            snapshot.cpu_percent = self._cpu_percent
            snapshot.memory_percent = self._memory_percent

        return snapshot

    def _log_summary(self) -> None:
        """Log a structured summary of current metrics."""
        snapshot = self.get_snapshot(include_system=True)

        # Log in structured format for aggregation
        log.info(
            "Performance summary | messages=%d | msg_latency_p95=%.1fms | "
            "llm_calls=%d | llm_latency_p95=%.1fms | skills=%d | "
            "db_ops=%d | db_latency_p95=%.1fms | db_writes=%d | db_write_latency_p95=%.1fms | "
            "react_iters=%d(%.1f/%.1f/%.1f min/med/max) | "
            "queue=%d | cpu=%.1f%% | mem=%.1f%%",
            snapshot.message_count,
            snapshot.message_latency.p95_ms,
            snapshot.llm_call_count,
            snapshot.llm_latency.p95_ms,
            snapshot.skill_call_count,
            snapshot.db_op_count,
            snapshot.db_latency.p95_ms,
            snapshot.db_write_op_count,
            snapshot.db_write_latency.p95_ms,
            snapshot.react_iteration_count,
            snapshot.react_iterations.min_ms,
            snapshot.react_iterations.median_ms,
            snapshot.react_iterations.max_ms,
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
                "db_write_op_count": snapshot.db_write_op_count,
                "db_write_latency_ms": snapshot.db_write_latency.to_dict(),
                "react_iterations": snapshot.react_iterations.to_dict(),
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
                await self.refresh_system_metrics()
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


def get_metrics_collector(
    history_size: int = METRICS_HISTORY_SIZE,
    summary_interval: int = METRICS_SUMMARY_INTERVAL,
) -> PerformanceMetrics:
    """
    Get or create the global performance metrics collector.

    Thread-safe singleton using get_or_create_singleton from utils.

    Args:
        history_size: Maximum samples to retain per metric.
        summary_interval: Messages between summary logs.

    Returns:
        The global PerformanceMetrics instance.
    """
    return get_or_create_singleton(
        PerformanceMetrics,
        history_size=history_size,
        summary_interval=summary_interval,
    )


def reset_metrics_collector() -> None:
    """Reset the global performance metrics collector (useful for testing)."""
    reset_singleton(PerformanceMetrics)


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
        await metrics.refresh_system_metrics()
        snapshot = metrics.get_snapshot(include_system=True)

        # Determine health status based on latency thresholds
        status = HealthStatus.HEALTHY
        messages: list[str] = []

        # Check message latency (warn if p95 > 5s)
        if snapshot.message_latency.p95_ms > 5000:
            status = HealthStatus.DEGRADED
            messages.append(f"High message latency: {snapshot.message_latency.p95_ms:.0f}ms p95")

        # Check LLM latency (warn if p95 > 30s)
        if snapshot.llm_latency.p95_ms > 30000:
            status = HealthStatus.DEGRADED
            messages.append(f"High LLM latency: {snapshot.llm_latency.p95_ms:.0f}ms p95")

        # Check system resources (degraded if memory > 90%)
        if snapshot.memory_percent > 90:
            status = HealthStatus.DEGRADED
            messages.append(f"High memory usage: {snapshot.memory_percent:.1f}%")

        message = "; ".join(messages) if messages else "Performance within normal parameters"

        # Build error rate summary for the health endpoint
        error_rates = snapshot.to_dict()["error_rates"]
        details: dict[str, Any] = {
            "error_rate_5m": error_rates["error_rate_5m"],
            "error_rate_15m": error_rates["error_rate_15m"],
            "error_rate_60m": error_rates["error_rate_60m"],
            "total_errors": error_rates["total_errors"],
        }

        return {
            "component": ComponentHealth(
                name="performance",
                status=status,
                message=message,
                details=details,
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
