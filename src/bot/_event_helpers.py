"""
_event_helpers.py — Shared fire-and-forget event emission helpers.

Centralizes the try/except + ``log_noncritical`` fallback pattern used
across all bot submodules so callers never crash on a non-critical
observability path.
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import Event, emit_error_event, get_event_bus

__all__ = ["_emit_event_safe", "_emit_error_event_safe"]

log = logging.getLogger(__name__)


async def _emit_event_safe(
    event_name: str,
    data: dict[str, Any],
    source: str,
    correlation_id: str | None = None,
) -> None:
    """Fire-and-forget event emission with non-critical error fallback.

    Wraps :meth:`EventBus.emit` in a ``try/except`` that logs emission
    failures via :func:`log_noncritical` so callers never crash on a
    non-critical observability path.
    """
    try:
        await get_event_bus().emit(
            Event(name=event_name, data=data, source=source, correlation_id=correlation_id)
        )
    except Exception:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            f"Failed to emit {event_name} event from {source}",
            logger=log,
        )


async def _emit_error_event_safe(
    error: Exception,
    source: str,
    extra_data: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Fire-and-forget error event emission with non-critical error fallback.

    Same as :func:`_emit_event_safe` but wraps :func:`emit_error_event`.
    """
    try:
        await emit_error_event(error, source, extra_data=extra_data, correlation_id=correlation_id)
    except Exception:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            f"Failed to emit error event from {source}",
            logger=log,
        )
