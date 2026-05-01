"""
src/health/prometheus.py — Prometheus text exposition format renderer.

Provides functions for formatting metrics into the Prometheus text exposition
format, including gauges, counters, summaries, and histograms.

Public API used by ``server.py``:
- ``build_prometheus_output`` — main metrics renderer for the ``/metrics`` endpoint
- ``build_scheduler_prometheus_output`` — scheduler-specific metrics
- ``build_circuit_breaker_prometheus_output`` — LLM circuit breaker metrics
- ``build_db_write_breaker_prometheus_output`` — DB write circuit breaker metrics
- ``build_dedup_prometheus_output`` — dedup service metrics
- ``build_event_bus_prometheus_output`` — EventBus emission/handler metrics
"""

from __future__ import annotations

import hashlib
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# PII Redaction
# ─────────────────────────────────────────────────────────────────────────────


def redact_chat_id(chat_id: str) -> str:
    """Hash a chat_id for Prometheus labels to avoid exposing PII (phone numbers)."""
    return hashlib.sha256(chat_id.encode()).hexdigest()[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Prometheus Text Format Renderer
# ─────────────────────────────────────────────────────────────────────────────


def format_prometheus_metric(
    name: str,
    help_text: str,
    metric_type: str,
    value: float | int,
    labels: dict[str, str] | None = None,
) -> str:
    """Format a single Prometheus metric line."""
    label_str = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        label_str = "{" + ",".join(parts) + "}"
    return (
        f"# HELP {name} {help_text}\n"
        f"# TYPE {name} {metric_type}\n"
        f"{name}{label_str} {value}\n"
    )


def format_prometheus_summary(
    name: str,
    help_text: str,
    count: int,
    sum_ms: float | None = None,
    quantiles: dict[str, float] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Format a Prometheus summary metric with quantiles and optional labels."""
    # Build label prefix for quantile lines (trailing comma) and full label
    # string for _sum/_count lines (no trailing comma)
    quantile_prefix = ""
    suffix_labels = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        joined = ",".join(parts)
        quantile_prefix = f"{joined},"
        suffix_labels = f"{{{joined}}}"

    lines = [
        f"# HELP {name} {help_text}\n",
        f"# TYPE {name} summary\n",
    ]
    if quantiles:
        for q_label, q_val in sorted(quantiles.items()):
            lines.append(f'{name}{{{quantile_prefix}quantile="{q_label}"}} {q_val}\n')
    if sum_ms is not None:
        sum_suffix = f"_sum{suffix_labels}" if suffix_labels else "_sum"
        lines.append(f"{name}{sum_suffix} {sum_ms}\n")
    count_suffix = f"_count{suffix_labels}" if suffix_labels else "_count"
    lines.append(f"{name}{count_suffix} {count}\n")
    return "".join(lines)


def format_prometheus_histogram(
    name: str,
    help_text: str,
    histogram: dict[str, Any],
    labels: dict[str, str] | None = None,
) -> str:
    """Format a Prometheus histogram metric with ``le``-bucket lines, ``_sum``, and ``_count``.

    *histogram* is expected to have the shape produced by
    ``LatencyHistogram.to_dict()``::

        {
            "buckets": {"500": 3, "1000": 5, ..., "+Inf": 10},
            "count": 10,
            "sum_ms": 12345.67,
        }
    """
    if not histogram or histogram.get("count", 0) == 0:
        return ""

    # Build label prefix for bucket lines (trailing comma) and full label
    # string for _sum/_count lines
    bucket_prefix = ""
    suffix_labels = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        joined = ",".join(parts)
        bucket_prefix = f"{joined},"
        suffix_labels = f"{{{joined}}}"

    lines = [
        f"# HELP {name} {help_text}\n",
        f"# TYPE {name} histogram\n",
    ]
    for le_label, count in histogram.get("buckets", {}).items():
        lines.append(
            f'{name}_bucket{{{bucket_prefix}le="{le_label}"}} {count}\n'
        )

    sum_suffix = f"_sum{suffix_labels}" if suffix_labels else "_sum"
    lines.append(f"{name}{sum_suffix} {histogram.get('sum_ms', 0)}\n")

    count_suffix = f"_count{suffix_labels}" if suffix_labels else "_count"
    lines.append(f"{name}{count_suffix} {histogram.get('count', 0)}\n")

    return "".join(lines)


def build_prometheus_output(
    token_usage: dict[str, Any],
    snapshot: Any,
    llm_log_dir_bytes: int | None = None,
    db_size_bytes: int | None = None,
    workspace_size_bytes: int | None = None,
    workspace_growth_mb_per_hour: float | None = None,
    disk_free_bytes: int | None = None,
    disk_total_bytes: int | None = None,
    per_chat_tokens: list[dict[str, Any]] | None = None,
) -> str:
    """Build the full Prometheus text exposition from metrics data."""
    lines: list[str] = []

    # ── Token Usage ──────────────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_token_usage_prompt_total",
            "Total prompt tokens consumed",
            "counter",
            token_usage.get("prompt_tokens", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_token_usage_completion_total",
            "Total completion tokens consumed",
            "counter",
            token_usage.get("completion_tokens", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_token_usage_total",
            "Total tokens consumed (prompt + completion)",
            "counter",
            token_usage.get("total_tokens", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_llm_requests_total",
            "Total LLM API requests made",
            "counter",
            token_usage.get("request_count", 0),
        )
    )

    # ── Per-Chat Token Usage ────────────────────────────────────────────────
    if per_chat_tokens:
        for entry in per_chat_tokens:
            chat_id = redact_chat_id(entry.get("chat_id", "unknown"))
            lines.append(
                format_prometheus_metric(
                    "custombot_chat_prompt_tokens",
                    "Per-chat prompt tokens consumed (top chats)",
                    "counter",
                    entry.get("prompt", 0),
                    labels={"chat_id": chat_id},
                )
            )
            lines.append(
                format_prometheus_metric(
                    "custombot_chat_completion_tokens",
                    "Per-chat completion tokens consumed (top chats)",
                    "counter",
                    entry.get("completion", 0),
                    labels={"chat_id": chat_id},
                )
            )

    # ── Message Metrics ─────────────────────────────────────────────────────
    msg_lat = snapshot.message_latency
    lines.append(
        format_prometheus_summary(
            "custombot_message_latency_milliseconds",
            "Message processing latency in milliseconds",
            count=msg_lat.count,
            sum_ms=round(msg_lat.mean_ms * msg_lat.count, 2) if msg_lat.count else 0,
            quantiles={
                "0.5": round(msg_lat.median_ms, 2),
                "0.95": round(msg_lat.p95_ms, 2),
                "0.99": round(msg_lat.p99_ms, 2),
            }
            if msg_lat.count
            else None,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_messages_processed_total",
            "Total messages processed",
            "counter",
            snapshot.message_count,
        )
    )

    # ── LLM Latency ─────────────────────────────────────────────────────────
    llm_lat = snapshot.llm_latency
    lines.append(
        format_prometheus_summary(
            "custombot_llm_latency_milliseconds",
            "LLM API call latency in milliseconds",
            count=llm_lat.count,
            sum_ms=round(llm_lat.mean_ms * llm_lat.count, 2) if llm_lat.count else 0,
            quantiles={
                "0.5": round(llm_lat.median_ms, 2),
                "0.95": round(llm_lat.p95_ms, 2),
                "0.99": round(llm_lat.p99_ms, 2),
            }
            if llm_lat.count
            else None,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_llm_calls_total",
            "Total LLM API calls made",
            "counter",
            snapshot.llm_call_count,
        )
    )

    # ── LLM Error Classification Counter ────────────────────────────────────
    # Prometheus exposition format requires exactly one HELP and one TYPE line
    # per metric name, followed by all label variants.
    if snapshot.llm_error_classifications:
        lines.append("# HELP custombot_llm_errors_total LLM errors classified by error code\n")
        lines.append("# TYPE custombot_llm_errors_total counter\n")
        for code, count in sorted(snapshot.llm_error_classifications.items()):
            safe_code = code.replace('"', '\\"')
            lines.append(f'custombot_llm_errors_total{{code="{safe_code}"}} {count}\n')

    # ── LLM Latency Histogram ──────────────────────────────────────────────
    lines.append(
        format_prometheus_histogram(
            "custombot_llm_latency",
            "LLM API call latency histogram in milliseconds (fixed buckets)",
            snapshot.llm_latency_histogram,
        )
    )

    # ── ReAct Iteration Metrics ──────────────────────────────────────────────
    react_iters = snapshot.react_iterations
    lines.append(
        format_prometheus_summary(
            "custombot_react_iterations",
            "Number of ReAct loop iterations per conversation",
            count=react_iters.count,
            sum_ms=round(react_iters.mean_ms * react_iters.count, 2)
            if react_iters.count
            else 0,
            quantiles={
                "0.5": round(react_iters.median_ms, 2),
                "0.95": round(react_iters.p95_ms, 2),
                "0.99": round(react_iters.p99_ms, 2),
            }
            if react_iters.count
            else None,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_react_loop_iterations_total",
            "Cumulative total ReAct loop iterations across all conversations",
            "counter",
            snapshot.react_iterations_total,
        )
    )

    # ── Context Budget Utilization ──────────────────────────────────────────
    if snapshot.context_budget_count > 0:
        lines.append(
            format_prometheus_metric(
                "custombot_context_budget_utilization_mean",
                "Mean ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_mean_ratio, 4),
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_context_budget_utilization_max",
                "Maximum observed ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_max_ratio, 4),
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_context_budget_utilization_p95",
                "P95 ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_p95_ratio, 4),
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_context_budget_utilization_samples",
                "Number of context-budget utilization samples collected",
                "gauge",
                snapshot.context_budget_count,
            )
        )

    # ── Database Metrics ────────────────────────────────────────────────────
    db_lat = snapshot.db_latency
    lines.append(
        format_prometheus_summary(
            "custombot_db_latency_milliseconds",
            "Database operation latency in milliseconds",
            count=db_lat.count,
            sum_ms=round(db_lat.mean_ms * db_lat.count, 2) if db_lat.count else 0,
            quantiles={
                "0.5": round(db_lat.median_ms, 2),
                "0.95": round(db_lat.p95_ms, 2),
                "0.99": round(db_lat.p99_ms, 2),
            }
            if db_lat.count
            else None,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_db_operations_total",
            "Total database operations executed",
            "counter",
            snapshot.db_op_count,
        )
    )

    # ── Database Write Latency Metrics ──────────────────────────────────────
    dbw_lat = snapshot.db_write_latency
    lines.append(
        format_prometheus_summary(
            "custombot_db_write_latency_milliseconds",
            "Database write operation latency in milliseconds",
            count=dbw_lat.count,
            sum_ms=round(dbw_lat.mean_ms * dbw_lat.count, 2) if dbw_lat.count else 0,
            quantiles={
                "0.5": round(dbw_lat.median_ms, 2),
                "0.95": round(dbw_lat.p95_ms, 2),
                "0.99": round(dbw_lat.p99_ms, 2),
            }
            if dbw_lat.count
            else None,
        )
    )
    lines.append(
        format_prometheus_histogram(
            "custombot_db_write_latency",
            "Database write operation latency histogram in milliseconds (fixed buckets)",
            snapshot.db_write_latency_histogram,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_db_write_operations_total",
            "Total database write operations executed",
            "counter",
            snapshot.db_write_op_count,
        )
    )

    # ── Queue Metrics ────────────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_queue_depth",
            "Current message queue depth",
            "gauge",
            snapshot.queue_depth,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_queue_max_depth",
            "Maximum observed queue depth",
            "gauge",
            snapshot.queue_max_depth,
        )
    )

    # ── Active Chats ─────────────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_active_chat_count",
            "Number of currently active chats",
            "gauge",
            snapshot.active_chat_count,
        )
    )

    # ── Memory Cache Metrics ─────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_memory_cache_hits_total",
            "Total memory cache hits (mtime unchanged, content reused)",
            "counter",
            snapshot.memory_cache_hits,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_memory_cache_misses_total",
            "Total memory cache misses (file changed or not yet cached)",
            "counter",
            snapshot.memory_cache_misses,
        )
    )

    # ── Embedding Cache Metrics ──────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_embed_cache_hits_total",
            "Total embedding cache hits (text already cached, API call avoided)",
            "counter",
            snapshot.embed_cache_hits,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_embed_cache_misses_total",
            "Total embedding cache misses (text not in cache, API call required)",
            "counter",
            snapshot.embed_cache_misses,
        )
    )

    # ── Compression Summary Metrics ──────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_compression_summary_used_total",
            "Total times a compressed conversation summary was used during context assembly",
            "counter",
            snapshot.compression_summary_used_total,
        )
    )

    # ── Skill Metrics ────────────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_skill_calls_total",
            "Total skill executions",
            "counter",
            snapshot.skill_call_count,
        )
    )
    for skill_name, skill_lat in snapshot.skill_latencies.items():
        lines.append(
            format_prometheus_summary(
                "custombot_skill_latency_milliseconds",
                "Skill execution latency in milliseconds",
                count=skill_lat.count,
                sum_ms=round(skill_lat.mean_ms * skill_lat.count, 2)
                if skill_lat.count
                else 0,
                quantiles={
                    "0.5": round(skill_lat.median_ms, 2),
                    "0.95": round(skill_lat.p95_ms, 2),
                    "0.99": round(skill_lat.p99_ms, 2),
                }
                if skill_lat.count
                else None,
                labels={"skill": skill_name},
            )
        )
        # Per-skill call count as a labeled metric
        lines.append(
            f'custombot_skill_calls_total{{skill="{skill_name}"}} {skill_lat.count}\n'
        )

    # ── Per-Skill Execution & Error Metrics ──────────────────────────────────
    for skill_name, sm in snapshot.skill_metrics.items():
        # Total executions (success + error)
        lines.append(
            format_prometheus_metric(
                "custombot_skill_executions_total",
                f"Total executions for {skill_name} (success + error)",
                "counter",
                sm.calls,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_successes_total",
                f"Successful executions for {skill_name}",
                "counter",
                sm.successes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_errors_total",
                f"Failed executions for {skill_name}",
                "counter",
                sm.errors,
                labels={"skill": skill_name},
            )
        )
        for err_type, count in sm.error_types.items():
            safe_err = err_type.replace('"', '\\"')
            lines.append(
                format_prometheus_metric(
                    "custombot_skill_errors_total",
                    f"Failed executions for {skill_name} by error type",
                    "counter",
                    count,
                    labels={"skill": skill_name, "error_type": safe_err},
                )
            )
        # Error rate gauge (errors / total executions)
        if sm.calls > 0:
            lines.append(
                format_prometheus_metric(
                    "custombot_skill_error_rate",
                    f"Error rate for {skill_name} (errors / executions)",
                    "gauge",
                    round(sm.errors / sm.calls, 4),
                    labels={"skill": skill_name},
                )
            )

    # ── Per-Skill Timeout Ratio ──────────────────────────────────────────────
    for skill_name, tr in snapshot.skill_timeout_ratios.items():
        lines.append(
            format_prometheus_metric(
                "custombot_skill_timeout_ratio_mean",
                "Mean ratio of actual execution time to declared skill timeout",
                "gauge",
                round(tr.mean_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_timeout_ratio_max",
                "Maximum observed ratio of actual time to declared skill timeout",
                "gauge",
                round(tr.max_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_timeout_ratio_p95",
                "P95 ratio of actual execution time to declared skill timeout",
                "gauge",
                round(tr.p95_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_timeout_ratio_samples",
                "Number of timeout-ratio samples collected per skill",
                "gauge",
                tr.count,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Skill Oversized Argument Rejections ──────────────────────────────
    for skill_name, count in snapshot.skill_oversized_args.items():
        lines.append(
            format_prometheus_metric(
                "custombot_skill_args_oversized_total",
                f"Number of rejected calls for {skill_name} due to oversized arguments",
                "counter",
                count,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Skill Oversized Argument Size Distribution ────────────────────────
    for skill_name, stats in snapshot.skill_oversized_args_sizes.items():
        lines.append(
            format_prometheus_metric(
                "custombot_skill_args_oversized_min_bytes",
                f"Smallest oversized argument payload size for {skill_name}",
                "gauge",
                stats.min_bytes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_args_oversized_max_bytes",
                f"Largest oversized argument payload size for {skill_name}",
                "gauge",
                stats.max_bytes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_skill_args_oversized_total_bytes",
                f"Cumulative oversized argument payload size for {skill_name}",
                "counter",
                stats.total_bytes,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Chat Message Counts ──────────────────────────────────────────────
    for chat_metric in snapshot.top_chats:
        lines.append(
            format_prometheus_metric(
                "custombot_chat_messages_total",
                "Per-chat message count (top chats)",
                "counter",
                chat_metric.message_count,
                labels={"chat_id": redact_chat_id(chat_metric.chat_id)},
            )
        )

    # ── Per-Chat Conversation Depth ──────────────────────────────────────────
    for depth_entry in snapshot.top_chat_depths:
        lines.append(
            format_prometheus_metric(
                "custombot_chat_conversation_depth",
                "Last ReAct iteration count per chat (top chats by depth)",
                "gauge",
                depth_entry.depth,
                labels={"chat_id": redact_chat_id(depth_entry.chat_id)},
            )
        )

    # ── System Metrics ───────────────────────────────────────────────────────
    if snapshot.cpu_percent > 0:
        lines.append(
            format_prometheus_metric(
                "custombot_cpu_percent",
                "CPU usage percentage",
                "gauge",
                round(snapshot.cpu_percent, 1),
            )
        )
    if snapshot.memory_percent > 0:
        lines.append(
            format_prometheus_metric(
                "custombot_memory_percent",
                "Memory usage percentage",
                "gauge",
                round(snapshot.memory_percent, 1),
            )
        )

    # ── Error Rate Trends ────────────────────────────────────────────────────
    lines.append(
        format_prometheus_metric(
            "custombot_errors_total",
            "Total errors recorded since startup",
            "counter",
            snapshot.total_error_count,
        )
    )
    for ew in snapshot.error_windows:
        window_label = f"{ew.window_seconds // 60}m"
        lines.append(
            format_prometheus_metric(
                "custombot_error_rate",
                f"Errors in the last {window_label}",
                "gauge",
                ew.error_count,
                labels={"window": window_label},
            )
        )
        lines.append(
            format_prometheus_metric(
                "custombot_error_rate_per_minute",
                f"Average errors per minute over the last {window_label}",
                "gauge",
                round(ew.error_rate_per_minute, 4),
                labels={"window": window_label},
            )
        )

    # ── LLM Log Directory Size ──────────────────────────────────────────────
    if llm_log_dir_bytes is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_llm_log_dir_bytes",
                "Total size of LLM request/response log directory in bytes",
                "gauge",
                llm_log_dir_bytes,
            )
        )

    # ── Disk Usage ──────────────────────────────────────────────────────────
    if db_size_bytes is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_db_size_bytes",
                "Total size of database directory (workspace/.data/) in bytes",
                "gauge",
                db_size_bytes,
            )
        )
    if workspace_size_bytes is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_workspace_size_bytes",
                "Total size of workspace directory in bytes",
                "gauge",
                workspace_size_bytes,
            )
        )
    if workspace_growth_mb_per_hour is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_workspace_growth_mb_per_hour",
                "Workspace disk usage growth rate in MB per hour",
                "gauge",
                round(workspace_growth_mb_per_hour, 3),
            )
        )

    # ── Filesystem Disk Space ──────────────────────────────────────────────
    if disk_free_bytes is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_disk_free_bytes",
                "Available disk space on the workspace partition in bytes",
                "gauge",
                disk_free_bytes,
            )
        )
    if disk_total_bytes is not None:
        lines.append(
            format_prometheus_metric(
                "custombot_disk_total_bytes",
                "Total disk capacity on the workspace partition in bytes",
                "gauge",
                disk_total_bytes,
            )
        )

    return "".join(lines)


def build_scheduler_prometheus_output(scheduler: Any) -> str:
    """Build Prometheus metrics for the task scheduler."""
    if scheduler is None:
        return ""

    status = scheduler.get_status()
    lines: list[str] = []

    running = 1 if status["running"] else 0
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_running",
            "Whether the task scheduler is running (1=yes, 0=no)",
            "gauge",
            running,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_tasks_total",
            "Total number of scheduled tasks",
            "gauge",
            status["total_tasks"],
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_enabled_tasks",
            "Number of enabled scheduled tasks",
            "gauge",
            status["enabled_tasks"],
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_chats_with_tasks",
            "Number of chats with at least one scheduled task",
            "gauge",
            status["chats_with_tasks"],
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_successes_total",
            "Total successful scheduled task executions",
            "counter",
            status["success_count"],
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_scheduler_failures_total",
            "Total failed scheduled task executions",
            "counter",
            status["failure_count"],
        )
    )

    return "".join(lines)


def build_circuit_breaker_prometheus_output(circuit_breaker: Any) -> str:
    """Build Prometheus metrics for the LLM circuit breaker."""
    if circuit_breaker is None:
        return ""

    from src.utils.circuit_breaker import CircuitState

    state = circuit_breaker.state
    state_value = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,
        CircuitState.OPEN: 2,
    }.get(state, 0)

    lines: list[str] = []
    lines.append(
        format_prometheus_metric(
            "custombot_llm_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=half-open, 2=open)",
            "gauge",
            state_value,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_llm_circuit_breaker_failures_total",
            "Total consecutive LLM failures recorded by the circuit breaker",
            "counter",
            circuit_breaker.failure_count,
        )
    )
    return "".join(lines)


def build_db_write_breaker_prometheus_output(circuit_breaker: Any) -> str:
    """Build Prometheus metrics for the database write circuit breaker."""
    if circuit_breaker is None:
        return ""

    from src.utils.circuit_breaker import CircuitState

    state = circuit_breaker.state
    state_value = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,
        CircuitState.OPEN: 2,
    }.get(state, 0)

    lines: list[str] = []
    lines.append(
        format_prometheus_metric(
            "custombot_db_write_circuit_breaker_state",
            "DB write circuit breaker state (0=closed, 1=half-open, 2=open)",
            "gauge",
            state_value,
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_db_write_circuit_breaker_failures_total",
            "Consecutive DB write failures recorded by the circuit breaker",
            "counter",
            circuit_breaker.failure_count,
        )
    )
    return "".join(lines)


def build_dedup_prometheus_output(dedup_stats: Any) -> str:
    """Build Prometheus metrics for the unified dedup service."""
    if dedup_stats is None:
        return ""

    stats = dedup_stats.to_dict()
    lines: list[str] = []

    lines.append(
        format_prometheus_metric(
            "custombot_dedup_inbound_hits_total",
            "Number of duplicate inbound messages detected by message-id dedup",
            "counter",
            stats.get("inbound_hits", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_dedup_inbound_misses_total",
            "Number of unique inbound messages passed by message-id dedup",
            "counter",
            stats.get("inbound_misses", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_dedup_outbound_hits_total",
            "Number of duplicate outbound messages suppressed by content-hash dedup",
            "counter",
            stats.get("outbound_hits", 0),
        )
    )
    lines.append(
        format_prometheus_metric(
            "custombot_dedup_outbound_misses_total",
            "Number of unique outbound messages delivered (content-hash dedup)",
            "counter",
            stats.get("outbound_misses", 0),
        )
    )
    return "".join(lines)


def build_event_bus_prometheus_output(event_bus: Any) -> str:
    """Build Prometheus metrics for EventBus emission and handler counts."""
    if event_bus is None:
        return ""

    metrics = event_bus.get_metrics()
    lines: list[str] = []

    for event_name, count in sorted(metrics["emissions"].items()):
        lines.append(
            format_prometheus_metric(
                "custombot_event_emitted_total",
                "Total number of EventBus emissions per event name",
                "counter",
                count,
                labels={"event": event_name},
            )
        )

    for event_name, count in sorted(metrics["invocations"].items()):
        lines.append(
            format_prometheus_metric(
                "custombot_event_handler_invocations_total",
                "Total number of handler invocations per event name",
                "counter",
                count,
                labels={"event": event_name},
            )
        )

    return "".join(lines)
