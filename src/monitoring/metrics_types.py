"""Data models and statistical helpers for performance metrics."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections import deque


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
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


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
                "timeout_ratios": {k: v.to_dict() for k, v in self.skill_timeout_ratios.items()},
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
                )
                if (self.memory_cache_hits + self.memory_cache_misses) > 0
                else 0.0,
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
                )
                if (self.embed_cache_hits + self.embed_cache_misses) > 0
                else 0.0,
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
                "error_rate_5m": self.error_windows[0].error_rate_per_minute
                if len(self.error_windows) > 0
                else 0.0,
                "error_rate_15m": self.error_windows[1].error_rate_per_minute
                if len(self.error_windows) > 1
                else 0.0,
                "error_rate_60m": self.error_windows[2].error_rate_per_minute
                if len(self.error_windows) > 2
                else 0.0,
                "windows": [w.to_dict() for w in self.error_windows],
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Statistical Helpers
# ─────────────────────────────────────────────────────────────────────────────


def calculate_latency_stats(samples: deque[float]) -> LatencyStats:
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

    return LatencyStats(
        count=count,
        min_ms=sorted_samples[0],
        max_ms=sorted_samples[-1],
        mean_ms=statistics.mean(sorted_samples),
        median_ms=statistics.median(sorted_samples),
        p95_ms=percentile(sorted_samples, 95),
        p99_ms=percentile(sorted_samples, 99),
    )


def calculate_timeout_ratio(samples: deque[float]) -> SkillTimeoutRatio:
    """Compute timeout-ratio statistics from a deque of ratio samples."""
    if not samples:
        return SkillTimeoutRatio()
    data = sorted(samples)
    count = len(data)

    return SkillTimeoutRatio(
        count=count,
        max_ratio=data[-1],
        mean_ratio=statistics.mean(data),
        p95_ratio=percentile(data, 95),
    )


def percentile(sorted_data: list[float], p: float) -> float:
    """Compute the p-th percentile of *sorted* data (0–100 scale)."""
    if not sorted_data:
        return 0.0
    count = len(sorted_data)
    k = (count - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < count else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


# ─────────────────────────────────────────────────────────────────────────────
# Latency Histogram
# ─────────────────────────────────────────────────────────────────────────────


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
