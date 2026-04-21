"""
src/monitoring/__init__.py — Memory monitoring and performance metrics.

This package provides two separate concerns:

1. Memory Monitoring (memory.py):
   - Periodic memory usage tracking
   - Threshold-based warnings
   - LRU cache size tracking

2. Performance Metrics (performance.py):
   - Message processing latency
   - LLM API latency
   - Skill execution time
   - Database operation times

Usage:
    from src.monitoring import MemoryMonitor, get_memory_stats
    from src.monitoring import PerformanceMetrics, get_metrics_collector

    # Create monitor with threshold
    monitor = MemoryMonitor(warning_threshold_percent=80.0)
    monitor.start_periodic_check(interval_seconds=60.0)

    # Get current stats
    stats = get_memory_stats()
    print(f"Memory usage: {stats.used_percent}%")

    # Register LRU cache for tracking
    monitor.register_cache("chat_locks", cache_instance)

    # Track performance metrics
    metrics = get_metrics_collector()
    metrics.track_message_latency(1.5)  # 1.5 seconds
    metrics.track_llm_latency(2.3)      # 2.3 seconds
    metrics.track_skill_time("bash", 0.8)
"""

from __future__ import annotations

# Memory monitoring exports
from src.monitoring.memory import (
    DEFAULT_MEMORY_CHECK_INTERVAL,
    DEFAULT_MEMORY_WARNING_THRESHOLD,
    MemoryMonitor,
    MemoryStats,
    check_memory_health,
    get_global_monitor,
    get_memory_stats,
    reset_global_monitor,
)

# Workspace monitor exports
from src.monitoring.workspace_monitor import (
    WorkspaceMonitor,
    WorkspaceStats,
    check_workspace_health,
    get_global_workspace_monitor,
    reset_global_workspace_monitor,
)

# Performance metrics exports
from src.monitoring.performance import (
    DEFAULT_METRICS_LOG_INTERVAL,
    DEFAULT_MAX_TRACKED_CHATS,
    DEFAULT_TOP_CHATS,
    METRICS_HISTORY_SIZE,
    METRICS_SUMMARY_INTERVAL,
    ChatConversationDepth,
    ChatMessageCount,
    LatencyStats,
    PerformanceMetrics,
    PerformanceSnapshot,
    SessionMetrics,
    SkillMetrics,
    _calculate_latency_stats,
    check_performance_health,
    get_metrics_collector,
    reset_metrics_collector,
)

__all__ = [
    # Memory monitoring
    "MemoryStats",
    "MemoryMonitor",
    "get_memory_stats",
    "get_global_monitor",
    "reset_global_monitor",
    "check_memory_health",
    "DEFAULT_MEMORY_WARNING_THRESHOLD",
    "DEFAULT_MEMORY_CHECK_INTERVAL",
    # Performance metrics
    "ChatConversationDepth",
    "ChatMessageCount",
    "LatencyStats",
    "PerformanceSnapshot",
    "PerformanceMetrics",
    "SessionMetrics",
    "SkillMetrics",
    "get_metrics_collector",
    "reset_metrics_collector",
    "check_performance_health",
    "METRICS_HISTORY_SIZE",
    "METRICS_SUMMARY_INTERVAL",
    "DEFAULT_METRICS_LOG_INTERVAL",
    "DEFAULT_MAX_TRACKED_CHATS",
    "DEFAULT_TOP_CHATS",
    # Workspace monitor
    "WorkspaceStats",
    "WorkspaceMonitor",
    "check_workspace_health",
    "get_global_workspace_monitor",
    "reset_global_workspace_monitor",
]
