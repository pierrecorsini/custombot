"""
src/core/event_bus.py — Lightweight typed event bus for cross-component decoupling.

Provides a simple publish/subscribe event bus so that extensions and plugins
can subscribe to application events without modifying core classes (Bot,
ToolExecutor, Application).

Built-in event names:
    - ``message_received``        — inbound message accepted for processing
    - ``skill_executed``          — a skill/tool finished execution
    - ``response_sent``           — outbound response delivered to the user
    - ``error_occurred``          — an unhandled error was caught
    - ``shutdown_started``        — graceful shutdown initiated
    - ``scheduled_task_started``  — scheduled task began processing
    - ``scheduled_task_completed`` — scheduled task finished successfully
    - ``message_dropped``          — message dropped without processing (no routing/match)
    - ``generation_conflict``      — write conflict detected during response delivery
    - ``startup_completed``         — application startup finished successfully
    - ``config_changed``            — configuration hot-reload applied changes
    - ``error_rate_exceeded``        — error rate exceeded a configured alert threshold
    - ``scheduled_task_failed``       — scheduled task encountered an exception

Usage::

    from src.core.event_bus import EventBus, Event, get_event_bus

    bus = get_event_bus()

    # Subscribe
    async def on_skill(event: Event) -> None:
        log.info("Skill %s finished in %.1fms",
                 event.data["skill_name"], event.data["duration_ms"])

    bus.on("skill_executed", on_skill)

    # Emit
    await bus.emit(Event(
        name="skill_executed",
        data={"skill_name": "bash", "duration_ms": 120.5},
        source="ToolExecutor",
    ))

    # Unsubscribe
    bus.off("skill_executed", on_skill)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from collections import deque

from src.constants.cache import (
    DEFAULT_EMISSION_RATE_WINDOW_SECONDS,
    DEFAULT_EMISSION_STORM_THRESHOLD,
    DEFAULT_MAX_CONCURRENT_EMIT_HANDLERS,
    DEFAULT_MAX_RATE_TRACKERS,
    DEFAULT_MAX_TRACKED_EVENT_NAMES,
)
from src.core.errors import NonCriticalCategory, log_noncritical
from src.logging.logging_config import get_correlation_id
from src.utils.locking import AsyncLock
from src.utils.singleton import get_or_create_singleton, reset_singleton
from src.utils import LRUDict

log = logging.getLogger(__name__)

# ── Type aliases ─────────────────────────────────────────────────────────

EventHandler = Callable[["Event"], Awaitable[None]]

# ── Built-in event names ─────────────────────────────────────────────────

EVENT_MESSAGE_RECEIVED: str = "message_received"
EVENT_SKILL_EXECUTED: str = "skill_executed"
EVENT_RESPONSE_SENT: str = "response_sent"
EVENT_ERROR_OCCURRED: str = "error_occurred"
EVENT_SHUTDOWN_STARTED: str = "shutdown_started"
EVENT_SCHEDULED_TASK_STARTED: str = "scheduled_task_started"
EVENT_SCHEDULED_TASK_COMPLETED: str = "scheduled_task_completed"
EVENT_MESSAGE_DROPPED: str = "message_dropped"
EVENT_GENERATION_CONFLICT: str = "generation_conflict"
EVENT_STARTUP_COMPLETED: str = "startup_completed"
EVENT_CONFIG_CHANGED: str = "config_changed"
EVENT_ERROR_RATE_EXCEEDED: str = "error_rate_exceeded"
EVENT_SCHEDULED_TASK_FAILED: str = "scheduled_task_failed"

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_MESSAGE_RECEIVED,
        EVENT_SKILL_EXECUTED,
        EVENT_RESPONSE_SENT,
        EVENT_ERROR_OCCURRED,
        EVENT_SHUTDOWN_STARTED,
        EVENT_SCHEDULED_TASK_STARTED,
        EVENT_SCHEDULED_TASK_COMPLETED,
        EVENT_MESSAGE_DROPPED,
        EVENT_GENERATION_CONFLICT,
        EVENT_STARTUP_COMPLETED,
        EVENT_CONFIG_CHANGED,
        EVENT_ERROR_RATE_EXCEEDED,
        EVENT_SCHEDULED_TASK_FAILED,
    }
)


# ── Event dataclass ──────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Event:
    """Immutable event payload.

    Attributes:
        name: Event type identifier (e.g. ``"skill_executed"``).
        data: Arbitrary payload dict for handler consumption.
        timestamp: Epoch seconds when the event was created.
        source: Optional identifier of the emitting component.
        correlation_id: Optional correlation ID for log tracing.
    """

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str | None = None
    correlation_id: str | None = None


# ── Data validation ────────────────────────────────────────────────────────


def _check_top_level_serializable(event_name: str, data: dict[str, Any]) -> None:
    """Lightweight production-mode check for non-serializable top-level values.

    Probes each top-level value individually with ``json.dumps`` — cheaper
    than serializing the entire dict because each value is checked in
    isolation (no cross-referencing or full-document scan).
    """
    bad_keys: list[str] = []
    for key, value in data.items():
        try:
            json.dumps(value)
        except RecursionError:
            bad_keys.append(f"{key}=<circular>")
        except (TypeError, ValueError, OverflowError):
            bad_keys.append(f"{key}=<{type(value).__name__}>")
    if bad_keys:
        log.warning(
            "Event '%s' data contains non-JSON-serializable values: %s",
            event_name,
            "; ".join(bad_keys),
        )


def _validate_data_serializable(event_name: str, data: dict[str, Any]) -> None:
    """Warn if *data* contains values that cannot be serialized to JSON.

    Scans top-level keys and reports the offending key name and value type
    so that callers can quickly locate the source of non-serializable data.
    Falls back to a generic message for deeply-nested issues.

    Catches ``RecursionError`` from circular references so that event
    construction never crashes on self-referential data structures.

    In production (log level >= INFO), the expensive ``json.dumps(data)``
    fast-path is skipped — individual key-level checks still run but are
    cheap because they operate on single values.
    """
    # Fast path: try the whole dict first — only in debug mode to avoid
    # the O(n) serialization cost on every emit() in production.
    first_exc: TypeError | ValueError | OverflowError | RecursionError | None = None
    if log.isEnabledFor(logging.DEBUG):
        try:
            json.dumps(data)
            return
        except RecursionError as exc:
            log.warning(
                "Event '%s' data contains circular references: %s",
                event_name,
                exc,
            )
            return
        except (TypeError, ValueError, OverflowError) as exc:
            first_exc = exc
    else:
        # Production mode: skip the expensive full-serialization fast path.
        # Instead, probe individual top-level values for common non-serializable
        # types without paying the O(n) cost of serializing the entire dict.
        _check_top_level_serializable(event_name, data)
        return

    # Debug-mode slow path: identify which top-level key(s) are problematic
    bad_keys: list[str] = []
    for key, value in data.items():
        try:
            json.dumps(value)
        except RecursionError:
            bad_keys.append(f"{key}=<circular>")
        except (TypeError, ValueError, OverflowError):
            bad_keys.append(f"{key}=<{type(value).__name__}>")

    if bad_keys:
        log.warning(
            "Event '%s' data contains non-JSON-serializable values: %s",
            event_name,
            "; ".join(bad_keys),
        )
    else:
        # Nested issue — reuse cached exception from fast path
        log.warning(
            "Event '%s' data contains non-JSON-serializable values: %s",
            event_name,
            first_exc,
        )


# ── Emission rate tracker ─────────────────────────────────────────────────


class _EmissionRateTracker:
    """Lightweight sliding-window emission rate tracker for a single event type.

    Uses a ``deque`` of timestamps for O(1) amortised pruning.  NOT
    thread-safe — designed for single-threaded asyncio event-loop use
    (same guarantee as ``SessionMetrics``).
    """

    __slots__ = ("_window_seconds", "_timestamps")

    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def record(self, now: float) -> None:
        """Record an emission at *now* (epoch seconds)."""
        self._timestamps.append(now)

    def rate_per_minute(self, now: float) -> float:
        """Return the current emission rate per minute over the sliding window."""
        self._prune(now)
        if not self._timestamps:
            return 0.0
        count = len(self._timestamps)
        elapsed = now - self._timestamps[0]
        if elapsed <= 0:
            # All timestamps are at the same instant — avoid div-by-zero.
            # Scale by window: if N events in 0s, rate = N / window * 60.
            return count / self._window_seconds * 60.0
        return count / elapsed * 60.0

    def count(self, now: float) -> int:
        """Return the number of emissions in the current window."""
        self._prune(now)
        return len(self._timestamps)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


# ── EventBus ─────────────────────────────────────────────────────────────


class EventBus:
    """Lightweight typed event bus for cross-component decoupling.

    * Async-first — all handlers are ``async def`` coroutines.
    * Error-isolated — a failing handler does not affect other handlers
      or the emitter.
    * Graceful close — ``close()`` prevents new emissions and clears
      subscriptions.
    * Lazy asyncio.Lock & Semaphore — safe to instantiate before the
      event loop is running (Windows ProactorEventLoop compatibility).

    Args:
        max_handlers_per_event: Max handlers per event name (prevents leaks).
        max_concurrent_handlers: Bounded semaphore cap for concurrent
            handler invocations (backpressure).
        emission_rate_window_seconds: Sliding-window duration for rate
            tracking.
        storm_threshold: Emissions-per-minute threshold that triggers a
            storm warning.  Set to ``0`` to disable storm detection.
        strict_event_names: When ``True``, ``emit()`` raises
            :class:`ValueError` for event names not in :data:`KNOWN_EVENTS`.
            When ``False`` (default), unknown names are logged at WARNING
            level but still emitted.
        max_rate_trackers: Maximum number of per-event-type rate trackers
            to retain.  Oldest trackers are evicted when the cap is reached,
            preventing memory leaks from many unique event names.
        max_tracked_event_names: Maximum number of distinct event names
            tracked in emission/handler-invocation counts.  Oldest names
            are evicted when the cap is reached, matching the rate-tracker
            LRU pattern.
    """

    __slots__ = (
        "_handlers",
        "_max_handlers_per_event",
        "_max_concurrent_handlers",
        "_emit_semaphore",
        "_closed",
        "_lock",
        "_emission_counts",
        "_handler_invocation_counts",
        "_rate_trackers",
        "_rate_window_seconds",
        "_storm_threshold",
        "_strict_event_names",
        "_max_rate_trackers",
        "_max_tracked_event_names",
    )

    def __init__(
        self,
        max_handlers_per_event: int = 50,
        max_concurrent_handlers: int = DEFAULT_MAX_CONCURRENT_EMIT_HANDLERS,
        emission_rate_window_seconds: float = DEFAULT_EMISSION_RATE_WINDOW_SECONDS,
        storm_threshold: float = DEFAULT_EMISSION_STORM_THRESHOLD,
        strict_event_names: bool = False,
        max_rate_trackers: int = DEFAULT_MAX_RATE_TRACKERS,
        max_tracked_event_names: int = DEFAULT_MAX_TRACKED_EVENT_NAMES,
    ) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._max_handlers_per_event = max_handlers_per_event
        self._max_concurrent_handlers = max_concurrent_handlers
        self._emit_semaphore: asyncio.Semaphore | None = None
        self._closed = False
        # Lazy-initialised via AsyncLock — see src.utils.locking policy
        self._lock = AsyncLock()
        self._emission_counts: LRUDict = LRUDict(max_size=max_tracked_event_names)
        self._handler_invocation_counts: LRUDict = LRUDict(max_size=max_tracked_event_names)
        # Emission rate tracking (per event type) — bounded via LRUDict
        self._rate_trackers: LRUDict = LRUDict(max_size=max_rate_trackers)
        self._rate_window_seconds = emission_rate_window_seconds
        self._storm_threshold = storm_threshold
        self._strict_event_names = strict_event_names
        self._max_rate_trackers = max_rate_trackers
        self._max_tracked_event_names = max_tracked_event_names

    # ── Subscribe / Unsubscribe ──────────────────────────────────────────

    def on(self, event_name: str, handler: EventHandler) -> None:
        """Subscribe *handler* to be called whenever *event_name* is emitted.

        The same handler is silently ignored if already subscribed to the
        given event.
        """
        handlers = self._handlers.setdefault(event_name, [])
        if len(handlers) >= self._max_handlers_per_event:
            log.warning(
                "Max handlers (%d) reached for event '%s'; ignoring new handler",
                self._max_handlers_per_event,
                event_name,
            )
            return
        if handler not in handlers:
            handlers.append(handler)
            log.debug(
                "Subscribed handler to event '%s' (%d total)",
                event_name,
                len(handlers),
            )

    def off(self, event_name: str, handler: EventHandler) -> None:
        """Remove *handler* from *event_name* subscriptions."""
        if event_name not in self._handlers:
            return
        self._handlers[event_name] = [h for h in self._handlers[event_name] if h is not handler]
        log.debug(
            "Unsubscribed handler from event '%s' (%d remaining)",
            event_name,
            len(self._handlers[event_name]),
        )

    # ── Emit ─────────────────────────────────────────────────────────────

    async def emit(self, event: Event) -> None:
        """Emit *event* to all subscribed handlers concurrently.

        Handlers are executed via ``asyncio.gather`` through a bounded
        semaphore (``max_concurrent_handlers``) that caps concurrent
        invocations to prevent unbounded coroutine fan-out.  Errors in
        individual handlers are caught, logged, and **do not** propagate
        to the caller or affect sibling handlers.

        Optimisations:
            - Early return when bus is closed or no handlers subscribed
              (skips rate tracking, storm detection, and emission counts).
            - Single-handler fast path bypasses ``asyncio.gather`` and
              semaphore overhead for the common 0-1 handler case.
        """
        if self._closed:
            log.warning("Event emitted after bus closed: %s", event.name)
            return

        # Validate event data is JSON-serializable.  The validation function
        # internally adapts: in debug mode it runs the full json.dumps() fast
        # path; in production it runs cheaper per-key checks only.
        if event.data:
            _validate_data_serializable(event.name, event.data)

        # ── Event name validation ───────────────────────────────────────
        if event.name not in KNOWN_EVENTS:
            if self._strict_event_names:
                raise ValueError(
                    f"Unknown event name '{event.name}' rejected in strict mode. "
                    f"Known events: {sorted(KNOWN_EVENTS)}"
                )
            log.warning(
                "Emitting unknown event '%s' — possible typo (known: %s)",
                event.name,
                sorted(KNOWN_EVENTS),
            )

        # ── Emission bookkeeping ────────────────────────────────────────
        self._emission_counts[event.name] = self._emission_counts.get(event.name, 0) + 1

        # ── Emission rate tracking ──────────────────────────────────────
        tracker = self._rate_trackers.get(event.name)
        if tracker is None:
            tracker = _EmissionRateTracker(self._rate_window_seconds)
            self._rate_trackers[event.name] = tracker
        tracker.record(event.timestamp)

        # ── Storm detection ────────────────────────────────────────────
        if self._storm_threshold > 0:
            rate = tracker.rate_per_minute(event.timestamp)
            if rate >= self._storm_threshold:
                log.warning(
                    "Event storm detected: '%s' emitted at %.1f/min "
                    "(threshold: %.1f/min, window: %.0fs)",
                    event.name,
                    rate,
                    self._storm_threshold,
                    self._rate_window_seconds,
                )

        # ── Handler lookup (after rate tracking, before invocation) ────
        handlers = self._handlers.get(event.name)
        if not handlers:
            return

        self._handler_invocation_counts[event.name] = self._handler_invocation_counts.get(
            event.name, 0
        ) + len(handlers)

        # ── Single-handler fast path ───────────────────────────────────
        if len(handlers) == 1:
            await _safe_call(handlers[0], event)
            return

        # Use return_exceptions=True so that Exception from one handler does NOT
        # cancel sibling handlers mid-flight.  BaseException (e.g. SystemExit,
        # GeneratorExit) is caught by _safe_call and logged — it never reaches
        # gather.  However, _safe_call re-raises KeyboardInterrupt and
        # CancelledError which asyncio.gather does NOT intercept even with
        # return_exceptions=True, so those propagate naturally.
        results = await asyncio.gather(
            *(self._emit_gated(h, event) for h in handlers),
            return_exceptions=True,
        )
        # Re-raise any non-Exception BaseException that somehow leaked through
        # (defensive — _safe_call should have handled these).
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _emit_gated(self, handler: EventHandler, event: Event) -> None:
        """Run a handler through the backpressure semaphore, then delegate to _safe_call."""
        if self._emit_semaphore is None:
            self._emit_semaphore = asyncio.Semaphore(self._max_concurrent_handlers)
        async with self._emit_semaphore:
            await _safe_call(handler, event)

    async def close(self) -> None:
        """Close the event bus and clear all subscriptions.

        After calling ``close()``, subsequent ``emit()`` calls are no-ops.
        """
        self._closed = True
        self._handlers.clear()
        log.debug("Event bus closed")

    @property
    def is_closed(self) -> bool:
        """Whether the bus has been closed."""
        return self._closed

    # ── Introspection ────────────────────────────────────────────────────

    def handler_count(self, event_name: str) -> int:
        """Return the number of handlers subscribed to *event_name*."""
        return len(self._handlers.get(event_name, []))

    def event_names(self) -> list[str]:
        """Return names of events that have at least one handler."""
        return [name for name, hs in self._handlers.items() if hs]

    def get_metrics(self) -> dict[str, dict[str, Any]]:
        """Return emission and handler-invocation counts per event name.

        Returns:
            ``{"emissions": {event: count, ...},
               "invocations": {event: count, ...},
               "emission_rates": {event: {"rate_per_minute": float,
                                          "count_in_window": int,
                                          "window_seconds": float}, ...},
               "rate_tracker_stats": {"tracked_event_type_count": int,
                                      "max_tracked_event_types": int}}``
        """
        now = time.time()
        rates: dict[str, dict[str, Any]] = {}
        for name, tracker in self._rate_trackers.items():
            rates[name] = {
                "rate_per_minute": round(tracker.rate_per_minute(now), 2),
                "count_in_window": tracker.count(now),
                "window_seconds": self._rate_window_seconds,
            }

        tracked_count = len(self._rate_trackers)
        if tracked_count >= self._max_rate_trackers:
            log.warning(
                "EventBus rate-tracker capacity reached: %d/%d tracked event types — "
                "oldest trackers are being evicted, which may indicate unknown event "
                "name accumulation or a memory leak",
                tracked_count,
                self._max_rate_trackers,
            )

        return {
            "emissions": dict(self._emission_counts.items()),
            "invocations": dict(self._handler_invocation_counts.items()),
            "emission_rates": rates,
            "rate_tracker_stats": {
                "tracked_event_type_count": tracked_count,
                "max_tracked_event_types": self._max_rate_trackers,
            },
        }


# ── Internal helpers ─────────────────────────────────────────────────────


async def _safe_call(handler: EventHandler, event: Event) -> None:
    """Invoke a handler, catching and logging any exception.

    Catches :class:`BaseException` so that ``SystemExit``, ``GeneratorExit``,
    and similar non-exception base-exceptions from subscriber handlers do not
    propagate through ``asyncio.gather`` and crash the emitter.  Only
    :class:`KeyboardInterrupt` and :class:`CancelledError` are re-raised so
    that graceful shutdown and task cancellation continue to work correctly.
    """
    try:
        await handler(event)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except BaseException:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            f"Error in event handler for '{event.name}'",
            logger=log,
            extra={
                "event_name": event.name,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )


# ── Public helpers ──────────────────────────────────────────────────────


async def emit_error_event(
    exc: BaseException,
    source: str,
    *,
    extra_data: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> None:
    """Emit an ``EVENT_ERROR_OCCURRED`` event with standardised error metadata.

    Constructs an :class:`Event` containing ``error_type`` and
    ``error_message`` keys plus any caller-supplied *extra_data*.
    Emission failures are caught and logged as non-critical so they
    never break the caller's error-handling path.

    Args:
        exc: The exception to report.
        source: Identifier of the emitting component (e.g. ``"Application.run"``).
        extra_data: Optional additional key-value pairs merged into the
            event ``data`` dict.
        correlation_id: Optional correlation ID; defaults to the current
            context-local ID via :func:`get_correlation_id`.
    """
    data: dict[str, Any] = {
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }
    if extra_data:
        data.update(extra_data)
    cid = correlation_id if correlation_id is not None else get_correlation_id()
    try:
        await get_event_bus().emit(
            Event(
                name=EVENT_ERROR_OCCURRED,
                data=data,
                source=source,
                correlation_id=cid,
            )
        )
    except Exception:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            f"Failed to emit error_occurred event from {source}",
            logger=log,
        )


# ── Singleton access ─────────────────────────────────────────────────────


def get_event_bus() -> EventBus:
    """Get or create the global ``EventBus`` singleton."""
    return get_or_create_singleton(EventBus)


def reset_event_bus() -> None:
    """Reset the global ``EventBus`` singleton (for testing)."""
    reset_singleton(EventBus)


__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "KNOWN_EVENTS",
    "emit_error_event",
    "get_event_bus",
    "reset_event_bus",
    # Event name constants
    "EVENT_MESSAGE_RECEIVED",
    "EVENT_SKILL_EXECUTED",
    "EVENT_RESPONSE_SENT",
    "EVENT_ERROR_OCCURRED",
    "EVENT_SHUTDOWN_STARTED",
    "EVENT_SCHEDULED_TASK_STARTED",
    "EVENT_SCHEDULED_TASK_COMPLETED",
    "EVENT_MESSAGE_DROPPED",
    "EVENT_GENERATION_CONFLICT",
    "EVENT_STARTUP_COMPLETED",
    "EVENT_CONFIG_CHANGED",
    "EVENT_ERROR_RATE_EXCEEDED",
    "EVENT_SCHEDULED_TASK_FAILED",
]
