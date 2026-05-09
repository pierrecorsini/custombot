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
from typing import Any, Optional

from src.utils.background_service import BaseBackgroundService
from src.utils.singleton import get_or_create_singleton, reset_singleton

from src.monitoring.metrics_types import (
    ChatConversationDepth,
    ChatLatencyPercentiles,
    ChatMessageCount,
    ChatReactIterations,
    ErrorWindowStats,
    LatencyHistogram,
    OversizedArgsSizeStats,
    PerformanceSnapshot,
    SessionMetrics,
    SkillMetrics,
    calculate_latency_stats,
    calculate_timeout_ratio,
    percentile,
)

# Lazy imports to avoid circular dependencies:
# - ConversationQualityMetrics from src.monitoring.quality_metrics
# - LatencyAnomalyDetector from src.monitoring.anomaly_detector

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

# TTL for cached system metrics (seconds). Avoids blocking psutil calls on
# every snapshot; refresh happens via the async ``refresh_system_metrics()``
# method driven by periodic logging and health-check callers.
SYSTEM_METRICS_TTL: float = 30.0

# Sliding-window sizes (seconds) for error rate tracking.
ERROR_WINDOW_SECONDS: tuple[int, ...] = (300, 900, 3600)  # 5m, 15m, 60m

# Default alert thresholds (errors/minute) per sliding window.
# When the error rate in any window exceeds its threshold, a structured
# ``error_rate_exceeded`` warning is logged so external alerting systems
# (ELK, Datadog, etc.) can trigger without polling the health endpoint.
# Set to 0.0 to disable alerting for a specific window.
DEFAULT_ERROR_ALERT_THRESHOLDS: dict[int, float] = {
    300: 10.0,   # 5-minute window:  alert if > 10 errors/min
    900: 5.0,    # 15-minute window: alert if > 5 errors/min
    3600: 2.0,   # 60-minute window: alert if > 2 errors/min
}

# Minimum seconds between consecutive error-rate threshold checks.
# Prevents alert spam when errors arrive in rapid succession.
ERROR_ALERT_COOLDOWN_SECONDS: float = 60.0

# Fixed bucket boundaries (milliseconds) for the LLM latency histogram.
# Prometheus histograms use cumulative counts per bucket, enabling
# server-side percentile computation over arbitrary time windows.
LLM_LATENCY_HISTOGRAM_BUCKETS_MS: tuple[float, ...] = (
    500.0,
    1000.0,
    2000.0,
    5000.0,
    10000.0,
    30000.0,
    60000.0,
    120000.0,
)

# Fixed bucket boundaries (milliseconds) for the DB write latency histogram.
# DB writes (JSONL appends, index updates) are typically sub-100ms; buckets
# are tuned for the fast write path rather than the slower LLM call path.
DB_WRITE_LATENCY_HISTOGRAM_BUCKETS_MS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    5000.0,
)

# Maximum number of chats tracked for per-chat message counting.
DEFAULT_MAX_TRACKED_CHATS: int = 1000

# Default number of top chats returned in snapshots.
DEFAULT_TOP_CHATS: int = 10

# Maximum number of chats tracked for per-chat latency percentiles.
# Kept smaller than DEFAULT_MAX_TRACKED_CHATS because each chat stores a
# deque of latency samples (costlier than a single counter).
MAX_PER_CHAT_LATENCY_CHATS: int = 100

# Maximum number of latency samples retained per chat for percentile
# computation.
PER_CHAT_LATENCY_SAMPLES: int = 50


class PerformanceMetrics(BaseBackgroundService):
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
        super().__init__()
        self._history_size = history_size
        self._summary_interval = summary_interval

        # Bounded deques for latency tracking (memory-efficient)
        self._message_latencies: deque[float] = deque(maxlen=history_size)
        self._llm_latencies: deque[float] = deque(maxlen=history_size)
        self._db_latencies: deque[float] = deque(maxlen=history_size)

        # DB write latency histogram (separate from general DB latency)
        self._db_write_latency_histogram = LatencyHistogram(DB_WRITE_LATENCY_HISTOGRAM_BUCKETS_MS)
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
        self._scheduled_task_count: int = 0

        # Scheduled task latency tracking (separate from interactive messages)
        self._scheduled_task_latencies: deque[float] = deque(maxlen=history_size)

        # Message rejection tracking (reason → count)
        self._message_rejection_counts: dict[str, int] = {}

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

        # Routing match cache effectiveness counters
        self._routing_cache_hits: int = 0
        self._routing_cache_misses: int = 0

        # Skill registry lookup cache effectiveness counters
        self._skill_registry_cache_hits: int = 0
        self._skill_registry_cache_misses: int = 0

        # Sliding-window error tracking (deque of timestamps per window)
        self._total_error_count: int = 0
        self._error_timestamps: deque[float] = deque(maxlen=10_000)

        # Error-rate alert thresholds (window_seconds → errors/minute)
        self._error_alert_thresholds: dict[int, float] = dict(DEFAULT_ERROR_ALERT_THRESHOLDS)
        self._last_error_alert_check: float = 0.0

        # Queue depth tracking
        self._queue_depth: int = 0
        self._queue_max_depth: int = 0

        # Queue flush latency tracking (last flush duration in milliseconds)
        self._last_flush_duration_ms: float = 0.0

        # Active chat tracking
        self._active_chat_count: int = 0

        # Per-chat message count tracking (bounded LRU)
        self._max_tracked_chats: int = DEFAULT_MAX_TRACKED_CHATS
        self._chat_message_counts: OrderedDict[str, int] = OrderedDict()

        # Per-chat conversation depth tracking (bounded LRU)
        self._chat_conversation_depths: OrderedDict[str, int] = OrderedDict()

        # Per-chat ReAct iteration count distribution (bounded LRU)
        self._chat_react_iterations: OrderedDict[str, int] = OrderedDict()

        # Per-chat latency tracking (bounded LRU, top-N chats by volume)
        self._chat_latencies: OrderedDict[str, deque[float]] = OrderedDict()

        # Cached system metrics (refreshed async via refresh_system_metrics)
        self._cpu_percent: float = 0.0
        self._memory_percent: float = 0.0
        self._last_system_refresh: float = 0.0

        # DB retry budget metrics (pushed from Database)
        self._retry_budget_ratio: float = 1.0
        self._retry_budget_recovery_eta_seconds: float = 0.0

        # Routing engine latency tracking (bounded deque, last 100 samples)
        self._routing_latencies: deque[float] = deque(maxlen=history_size)
        self._routing_match_count: int = 0
        self._routing_miss_count: int = 0

        # Per-skill sliding-window error rate tracking
        # skill_name → deque of (monotonic_timestamp, is_error)
        self._skill_error_windows: dict[str, deque[tuple[float, bool]]] = {}

        # Background logging task

        # Conversation quality metrics (lazy module import for init)
        from src.monitoring.quality_metrics import ConversationQualityMetrics
        self._quality_metrics = ConversationQualityMetrics()

        # LLM latency anomaly detector (lazy module import for init)
        from src.monitoring.anomaly_detector import LatencyAnomalyDetector
        self._anomaly_detector = LatencyAnomalyDetector()

    # ── Metric Recording ─────────────────────────────────────────────────────

    def track_message_latency(self, latency_seconds: float, chat_id: str | None = None) -> None:
        """
        Record a message processing latency.

        Args:
            latency_seconds: Time taken to process the message.
            chat_id: Optional chat identifier for per-chat latency tracking.
        """
        latency_ms = latency_seconds * 1000
        self._message_latencies.append(latency_ms)
        self._message_count += 1

        if chat_id is not None:
            self._record_chat_latency(chat_id, latency_ms)

        # Log summary at interval
        if self._message_count % self._summary_interval == 0:
            self._log_summary()

    def track_scheduled_task_latency(self, latency_seconds: float) -> None:
        """Record a scheduled task processing latency (separate from interactive messages).

        Scheduled tasks have fundamentally different latency profiles from
        interactive messages — they bypass routing, ACL checks, and the
        message queue, so tracking them separately avoids skewing interactive
        latency percentiles.

        Args:
            latency_seconds: Wall-clock time taken for the scheduled task.
        """
        latency_ms = latency_seconds * 1000
        self._scheduled_task_latencies.append(latency_ms)
        self._scheduled_task_count += 1

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
        self._anomaly_detector.observe(latency_seconds)

    def track_react_iterations(self, count: int) -> None:
        """Record the number of ReAct loop iterations for a conversation."""
        self._react_iteration_counts.append(float(count))
        self._react_iterations_total += count

    def track_context_budget_utilization(self, used_tokens: int, budget: int) -> None:
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
        self._llm_error_counts[error_code] = self._llm_error_counts.get(error_code, 0) + 1

    def track_message_rejected(self, reason: str) -> None:
        """Record a message rejection with reason label for monitoring.

        Tracks rejection counts by reason so operators can build dashboards
        showing which rejection paths are most active.

        Args:
            reason: Rejection reason label (e.g. ``"acl_rejected"``,
                ``"rate_limited"``, ``"empty_text"``).
        """
        self._message_rejection_counts[reason] = self._message_rejection_counts.get(reason, 0) + 1

    def track_skill_args_oversized(self, skill_name: str, arg_size_bytes: int) -> None:
        """Record a rejected skill call due to oversized arguments.

        Args:
            skill_name: Name of the skill that received oversized args.
            arg_size_bytes: Size of the raw argument payload in bytes.
        """
        self._skill_oversized_args[skill_name] = self._skill_oversized_args.get(skill_name, 0) + 1

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
            self._skill_timeout_ratios[skill_name] = deque(maxlen=self._history_size)
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

    def track_flush_duration(self, duration_seconds: float) -> None:
        """Record the duration of the last queue flush operation.

        Called by ``FlushManager`` after each flush (both background loop
        and inline threshold-triggered flushes) so operators can detect
        slow disk I/O before it causes queue backlog.

        Args:
            duration_seconds: Wall-clock time taken for the flush.
        """
        self._last_flush_duration_ms = duration_seconds * 1000

    def update_retry_budget(self, ratio: float, recovery_eta_seconds: float) -> None:
        """Push DB retry budget metrics from the Database layer.

        Called by health-check code or periodic polling so the budget
        state is included in :meth:`get_snapshot` output.
        """
        self._retry_budget_ratio = ratio
        self._retry_budget_recovery_eta_seconds = recovery_eta_seconds

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

    def record_quality_user_message(self, chat_id: str) -> None:
        """Record a user message for quality metrics tracking."""
        self._quality_metrics.record_user_message(chat_id)

    def record_quality_bot_response(self, chat_id: str) -> None:
        """Record a bot response for quality metrics tracking."""
        self._quality_metrics.record_bot_response(chat_id)

    def record_quality_tool_call(self, chat_id: str, success: bool) -> None:
        """Record a tool call outcome for quality metrics tracking."""
        self._quality_metrics.record_tool_call(chat_id, success)

    @property
    def quality_metrics(self) -> Any:
        """Access the conversation quality metrics instance."""
        return self._quality_metrics

    @property
    def anomaly_detector(self) -> Any:
        """Access the LLM latency anomaly detector instance."""
        return self._anomaly_detector

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

    def track_routing_cache_hit(self) -> None:
        """Record a routing match cache hit (previously computed match reused)."""
        self._routing_cache_hits += 1

    def track_routing_cache_miss(self) -> None:
        """Record a routing match cache miss (rules evaluated from scratch)."""
        self._routing_cache_misses += 1

    def track_skill_registry_cache_hit(self) -> None:
        """Record a skill registry lookup cache hit (skill found by name)."""
        self._skill_registry_cache_hits += 1

    def track_skill_registry_cache_miss(self) -> None:
        """Record a skill registry lookup cache miss (skill name not registered)."""
        self._skill_registry_cache_misses += 1

    def track_error(self) -> None:
        """Record an error with timestamp for sliding-window rate tracking."""
        self._total_error_count += 1
        self._error_timestamps.append(time.time())
        self._check_error_rate_thresholds()

    def track_routing_latency(
        self, duration_seconds: float, matched: bool, rule_count: int = 0
    ) -> None:
        """Record routing engine match latency.

        Args:
            duration_seconds: Wall-clock time for the routing match.
            matched: Whether a rule matched.
            rule_count: Number of candidate rules evaluated.
        """
        latency_ms = duration_seconds * 1000
        self._routing_latencies.append(latency_ms)
        if matched:
            self._routing_match_count += 1
        else:
            self._routing_miss_count += 1

    def track_skill_result(self, skill_name: str, success: bool) -> None:
        """Record a skill execution result for sliding-window error rate.

        Args:
            skill_name: Name of the skill.
            success: True if the skill succeeded.
        """
        if skill_name not in self._skill_error_windows:
            self._skill_error_windows[skill_name] = deque(maxlen=self._history_size)
        self._skill_error_windows[skill_name].append((time.monotonic(), success))

    def get_skill_error_rate(
        self, skill_name: str, window_seconds: int = 300
    ) -> float:
        """Compute the error rate for a skill over a sliding window.

        Args:
            skill_name: Name of the skill.
            window_seconds: Sliding window size in seconds (default 5 min).

        Returns:
            Error rate as 0.0–1.0 within the window.
        """
        samples = self._skill_error_windows.get(skill_name)
        if not samples:
            return 0.0
        cutoff = time.monotonic() - window_seconds
        total = 0
        errors = 0
        for ts, success in samples:
            if ts >= cutoff:
                total += 1
                if not success:
                    errors += 1
        return errors / total if total > 0 else 0.0

    def _check_error_rate_thresholds(self) -> None:
        """Check error windows against alert thresholds and log if exceeded.

        Runs at most once per ``ERROR_ALERT_COOLDOWN_SECONDS`` to avoid alert
        spam.  Each breached window emits a structured ``warning`` log with an
        ``alert_type: error_rate_exceeded`` extra field, enabling external
        log-aggregation systems (ELK, Datadog, PagerDuty) to trigger alerts
        without polling the health endpoint.
        """
        now = time.time()
        if now - self._last_error_alert_check < ERROR_ALERT_COOLDOWN_SECONDS:
            return
        self._last_error_alert_check = now

        for stats in self._get_error_window_counts():
            threshold = self._error_alert_thresholds.get(stats.window_seconds, 0.0)
            if threshold <= 0 or stats.error_rate_per_minute <= threshold:
                continue
            window_label = stats.window_seconds // 60
            log.warning(
                "error_rate_exceeded | window=%dm | rate=%.2f/min | "
                "threshold=%.2f/min | count=%d",
                window_label,
                stats.error_rate_per_minute,
                threshold,
                stats.error_count,
                extra={
                    "alert_type": "error_rate_exceeded",
                    "window_seconds": stats.window_seconds,
                    "error_rate_per_minute": round(stats.error_rate_per_minute, 4),
                    "threshold_per_minute": threshold,
                    "error_count": stats.error_count,
                },
            )

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
            results.append(
                ErrorWindowStats(
                    window_seconds=window_secs,
                    error_count=count,
                    error_rate_per_minute=rate,
                )
            )
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

    def track_react_iterations_per_chat(self, chat_id: str, count: int) -> None:
        """Record ReAct iteration count for a specific chat (bounded LRU).

        Each call overwrites the previous count for the given chat, keeping
        only the most recent iteration count.  When the max tracked chats
        limit is exceeded, the least-recently-used half is evicted.
        """
        self._chat_react_iterations[chat_id] = count
        self._chat_react_iterations.move_to_end(chat_id)
        if len(self._chat_react_iterations) > self._max_tracked_chats:
            for _ in range(len(self._chat_react_iterations) // 2):
                self._chat_react_iterations.popitem(last=False)

    def get_top_chat_react_iterations(self, n: int = DEFAULT_TOP_CHATS) -> list[ChatReactIterations]:
        """Return the top-N chats by ReAct iteration count, descending."""
        sorted_chats = sorted(
            self._chat_react_iterations.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        return [ChatReactIterations(chat_id=cid, iteration_count=cnt) for cid, cnt in sorted_chats[:n]]

    def _record_chat_latency(self, chat_id: str, latency_ms: float) -> None:
        """Record a message latency sample for a specific chat (bounded LRU).

        Uses OrderedDict LRU eviction like ``track_chat_message``. When the
        max tracked chats limit is reached, the least-recently-used entries
        (and their latency deques) are evicted.
        """
        if chat_id in self._chat_latencies:
            self._chat_latencies[chat_id].append(latency_ms)
            self._chat_latencies.move_to_end(chat_id)
            return

        # Evict oldest half when at capacity
        if len(self._chat_latencies) >= MAX_PER_CHAT_LATENCY_CHATS:
            for _ in range(len(self._chat_latencies) // 2):
                evicted_key, _ = self._chat_latencies.popitem(last=False)
                # (deque is garbage-collected with the dict entry)

        self._chat_latencies[chat_id] = deque(maxlen=PER_CHAT_LATENCY_SAMPLES)
        self._chat_latencies[chat_id].append(latency_ms)

    def get_top_chat_latencies(self, n: int = DEFAULT_TOP_CHATS) -> list[ChatLatencyPercentiles]:
        """Return the top-N chats by message volume with latency percentiles.

        Sorted by sample count descending so the highest-volume chats appear
        first — these are the conversations where latency matters most.
        """
        results: list[ChatLatencyPercentiles] = []
        for chat_id, samples in self._chat_latencies.items():
            if not samples:
                continue
            sorted_samples = sorted(samples)
            results.append(
                ChatLatencyPercentiles(
                    chat_id=chat_id,
                    message_count=len(sorted_samples),
                    p50_ms=percentile(sorted_samples, 50),
                    p95_ms=percentile(sorted_samples, 95),
                    p99_ms=percentile(sorted_samples, 99),
                )
            )
        results.sort(key=lambda r: r.message_count, reverse=True)
        return results[:n]

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
            message_latency=calculate_latency_stats(self._message_latencies),
            llm_call_count=self._llm_call_count,
            llm_latency=calculate_latency_stats(self._llm_latencies),
            llm_latency_histogram=self._llm_latency_histogram.to_dict(),
            skill_call_count=self._skill_call_count,
            skill_latencies={
                name: calculate_latency_stats(samples)
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
                name: calculate_timeout_ratio(samples)
                for name, samples in self._skill_timeout_ratios.items()
            },
            db_op_count=self._db_op_count,
            db_latency=calculate_latency_stats(self._db_latencies),
            db_write_op_count=self._db_write_op_count,
            db_write_latency=calculate_latency_stats(self._db_write_latencies),
            db_write_latency_histogram=self._db_write_latency_histogram.to_dict(),
            react_iteration_count=len(self._react_iteration_counts),
            react_iterations=calculate_latency_stats(self._react_iteration_counts),
            react_iterations_total=self._react_iterations_total,
            context_budget_count=len(self._context_budget_ratios),
            context_budget_mean_ratio=statistics.mean(self._context_budget_ratios)
            if self._context_budget_ratios
            else 0.0,
            context_budget_max_ratio=max(self._context_budget_ratios)
            if self._context_budget_ratios
            else 0.0,
            context_budget_p95_ratio=percentile(list(self._context_budget_ratios), 95)
            if self._context_budget_ratios
            else 0.0,
            queue_depth=self._queue_depth,
            queue_max_depth=self._queue_max_depth,
            last_flush_duration_ms=self._last_flush_duration_ms,
            active_chat_count=self._active_chat_count,
            top_chats=self.get_top_chats(),
            top_chat_depths=self.get_top_chat_depths(),
            top_chat_latencies=self.get_top_chat_latencies(),
            top_chat_react_iterations=self.get_top_chat_react_iterations(),
            memory_cache_hits=self._memory_cache_hits,
            memory_cache_misses=self._memory_cache_misses,
            outbound_dedup_hits=self._outbound_dedup_hits,
            outbound_dedup_misses=self._outbound_dedup_misses,
            compression_summary_used_total=self._compression_summary_used_total,
            embed_cache_hits=self._embed_cache_hits,
            embed_cache_misses=self._embed_cache_misses,
            routing_cache_hits=self._routing_cache_hits,
            routing_cache_misses=self._routing_cache_misses,
            skill_registry_cache_hits=self._skill_registry_cache_hits,
            skill_registry_cache_misses=self._skill_registry_cache_misses,
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
            error_alert_thresholds=dict(self._error_alert_thresholds),
            message_rejection_counts=dict(self._message_rejection_counts),
            scheduled_task_count=self._scheduled_task_count,
            scheduled_task_latency=calculate_latency_stats(self._scheduled_task_latencies),
            retry_budget_ratio=self._retry_budget_ratio,
            retry_budget_recovery_eta_seconds=self._retry_budget_recovery_eta_seconds,
            quality_metrics=self._quality_metrics.get_stats().to_dict(),
            anomaly_stats=self._anomaly_detector.get_stats().to_dict(),
            routing_latency=calculate_latency_stats(self._routing_latencies),
            routing_match_count=self._routing_match_count,
            routing_miss_count=self._routing_miss_count,
            skill_error_rates={
                name: self.get_skill_error_rate(name)
                for name in self._skill_error_windows
            },
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

        # Build dedup stats string if dedup counters are non-zero
        dedup_total = self._outbound_dedup_hits + self._outbound_dedup_misses
        dedup_line = ""
        if dedup_total > 0:
            hit_rate = self._outbound_dedup_hits / dedup_total if dedup_total else 0.0
            dedup_line = f" | dedup_outbound_hit_rate={hit_rate:.2f}"

        # Log in structured format for aggregation
        log.info(
            "Performance summary | messages=%d | msg_latency_p95=%.1fms | "
            "llm_calls=%d | llm_latency_p95=%.1fms | skills=%d | "
            "db_ops=%d | db_latency_p95=%.1fms | db_writes=%d | db_write_latency_p95=%.1fms | "
            "react_iters=%d(%.1f/%.1f/%.1f min/med/max) | "
            "queue=%d | cpu=%.1f%% | mem=%.1f%%%s",
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
            dedup_line,
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

    async def _run_loop(self) -> None:
        """Background task for periodic metrics logging."""
        interval_seconds = getattr(self, "_interval", DEFAULT_METRICS_LOG_INTERVAL)
        log.info(
            "Performance metrics logging started (interval=%.1fs)",
            interval_seconds,
        )

        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                await self.refresh_system_metrics()
                self._log_summary()
                self._log_dedup_stats()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Metrics logging error: %s", exc, exc_info=True)

    def _log_dedup_stats(self) -> None:
        """Log periodic dedup statistics for operational awareness."""
        total_out = self._outbound_dedup_hits + self._outbound_dedup_misses
        if total_out == 0 and self._outbound_dedup_hits == 0:
            return

        out_rate = self._outbound_dedup_hits / total_out if total_out else 0.0
        log.info(
            "Dedup stats: outbound_hit_rate=%.2f, outbound_hits=%d, outbound_misses=%d",
            out_rate,
            self._outbound_dedup_hits,
            self._outbound_dedup_misses,
        )

    def start_periodic_logging(
        self, interval_seconds: float = DEFAULT_METRICS_LOG_INTERVAL
    ) -> None:
        """
        Start periodic metrics logging in the background.

        Args:
            interval_seconds: How often to log metrics (default 60s).
        """
        self._interval = interval_seconds
        self.start()


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

        # Push DB retry budget metrics into the collector so they appear in
        # the snapshot.  get_database_metrics is a lazy import to avoid
        # circular deps at module level.
        try:
            from src.db._budget_helpers import get_retry_budget_snapshot

            budget = get_retry_budget_snapshot()
            if budget is not None:
                metrics.update_retry_budget(budget["ratio"], budget["recovery_eta_seconds"])
        except Exception:
            pass  # best-effort — don't fail the health check

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
    except Exception as exc:
        log.error("Performance health check failed: %s", exc, exc_info=True)
        return {
            "component": ComponentHealth(
                name="performance",
                status=HealthStatus.DEGRADED,
                message=f"Performance check error: {type(exc).__name__}",
            ),
            "metrics": None,
        }
