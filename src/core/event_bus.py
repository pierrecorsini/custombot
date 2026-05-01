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
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.errors import NonCriticalCategory, log_noncritical
from src.utils.locking import AsyncLock
from src.utils.singleton import get_or_create_singleton, reset_singleton

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

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_MESSAGE_RECEIVED,
        EVENT_SKILL_EXECUTED,
        EVENT_RESPONSE_SENT,
        EVENT_ERROR_OCCURRED,
        EVENT_SHUTDOWN_STARTED,
        EVENT_SCHEDULED_TASK_STARTED,
        EVENT_SCHEDULED_TASK_COMPLETED,
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


# ── EventBus ─────────────────────────────────────────────────────────────


class EventBus:
    """Lightweight typed event bus for cross-component decoupling.

    * Async-first — all handlers are ``async def`` coroutines.
    * Error-isolated — a failing handler does not affect other handlers
      or the emitter.
    * Graceful close — ``close()`` prevents new emissions and clears
      subscriptions.
    * Lazy asyncio.Lock — safe to instantiate before the event loop is
      running (Windows ProactorEventLoop compatibility).
    """

    __slots__ = (
        "_handlers",
        "_max_handlers_per_event",
        "_closed",
        "_lock",
        "_emission_counts",
        "_handler_invocation_counts",
    )

    def __init__(self, max_handlers_per_event: int = 50) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._max_handlers_per_event = max_handlers_per_event
        self._closed = False
        # Lazy-initialised via AsyncLock — see src.utils.locking policy
        self._lock = AsyncLock()
        self._emission_counts: dict[str, int] = {}
        self._handler_invocation_counts: dict[str, int] = {}

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
        self._handlers[event_name] = [
            h for h in self._handlers[event_name] if h is not handler
        ]
        log.debug(
            "Unsubscribed handler from event '%s' (%d remaining)",
            event_name,
            len(self._handlers[event_name]),
        )

    # ── Emit ─────────────────────────────────────────────────────────────

    async def emit(self, event: Event) -> None:
        """Emit *event* to all subscribed handlers concurrently.

        Handlers are executed via ``asyncio.gather``.  Errors in individual
        handlers are caught, logged, and **do not** propagate to the caller
        or affect sibling handlers.
        """
        if self._closed:
            log.warning("Event emitted after bus closed: %s", event.name)
            return

        self._emission_counts[event.name] = (
            self._emission_counts.get(event.name, 0) + 1
        )

        handlers = self._handlers.get(event.name)
        if not handlers:
            return

        self._handler_invocation_counts[event.name] = (
            self._handler_invocation_counts.get(event.name, 0) + len(handlers)
        )

        if event.name not in KNOWN_EVENTS:
            log.debug(
                "Emitting custom event '%s' to %d handler(s)",
                event.name,
                len(handlers),
            )

        await asyncio.gather(
            *(_safe_call(h, event) for h in handlers),
            return_exceptions=False,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────

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

    def get_metrics(self) -> dict[str, dict[str, int]]:
        """Return emission and handler-invocation counts per event name.

        Returns:
            ``{"emissions": {event: count, ...}, "invocations": {event: count, ...}}``
        """
        return {
            "emissions": dict(self._emission_counts),
            "invocations": dict(self._handler_invocation_counts),
        }


# ── Internal helpers ─────────────────────────────────────────────────────


async def _safe_call(handler: EventHandler, event: Event) -> None:
    """Invoke a handler, catching and logging any exception."""
    try:
        await handler(event)
    except Exception:
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
]
