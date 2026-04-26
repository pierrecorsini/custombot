"""Structured handling for non-critical errors.

Provides categorized logging for operations where failure must never
break the caller's control flow (metrics tracking, cleanup, compression,
etc.).  Each non-critical failure is tagged with a ``non_critical``
extra field so it can be filtered/matched in structured log output.

Usage::

    from src.core.errors import NonCriticalCategory, log_noncritical

    try:
        get_metrics_collector().track_cache_hit()
    except Exception:
        log_noncritical(
            NonCriticalCategory.METRICS,
            "Failed to track cache hit",
            logger=log,
        )
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any


class NonCriticalCategory(str, Enum):
    """Categories for non-critical operations."""

    EVENT_EMISSION = "event_emission"
    METRICS = "metrics"
    COMPRESSION = "compression"
    CLEANUP = "cleanup"
    EMBEDDING = "embedding"
    HEALTH_CHECK = "health_check"
    SKILL_DISCOVERY = "skill_discovery"
    STREAMING = "streaming"
    LOGGING = "logging"
    CACHE_TRACKING = "cache_tracking"
    DB_TRACKING = "db_tracking"
    CONFIG_RESOLUTION = "config_resolution"
    SKILL_PARSING = "skill_parsing"
    FILE_PARSING = "file_parsing"
    URL_SANITIZATION = "url_sanitization"
    CHANNEL_SEND = "channel_send"
    MIDDLEWARE_LOADING = "middleware_loading"
    TYPE_RESOLUTION = "type_resolution"
    CONNECTION_CLEANUP = "connection_cleanup"
    QUEUE_OPERATION = "queue_operation"
    DB_OPERATION = "db_operation"
    STARTUP_PROBE = "startup_probe"
    CONTEXT_ASSEMBLY = "context_assembly"
    CHANNEL_INPUT = "channel_input"
    SKILL_EXECUTION = "skill_execution"
    CONFIG_LOAD = "config_load"
    URL_PARSING = "url_parsing"
    MESSAGE_SEND = "message_send"
    SHUTDOWN = "shutdown"
    MONITORING = "monitoring"
    SCHEDULER = "scheduler"


def log_noncritical(
    category: NonCriticalCategory,
    message: str,
    *,
    logger: logging.Logger,
    level: int = logging.DEBUG,
    exc_info: bool = True,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a non-critical error with structured categorization.

    Use for fire-and-forget operations where failure must never
    propagate to the caller but should be observable in logs.
    """
    merged_extra: dict[str, Any] = {"non_critical": category.value}
    if extra:
        merged_extra.update(extra)
    logger.log(
        level,
        "[%s] %s",
        category.value,
        message,
        exc_info=exc_info,
        extra=merged_extra,
    )
