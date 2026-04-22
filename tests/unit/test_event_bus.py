"""Tests for EventBus handler error isolation.

Verifies:
- A failing handler doesn't prevent other handlers from executing
- A failing handler's exception is logged with correct event metadata
- emit() returns normally even when all handlers fail
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.core.event_bus import Event, EventBus


@pytest.fixture
def bus() -> EventBus:
    """Provide a fresh EventBus for each test."""
    return EventBus()


# ─────────────────────────────────────────────────────────────────────────────
# Handler error isolation
# ─────────────────────────────────────────────────────────────────────────────


async def _failing_handler(event: Event) -> None:
    """Handler that always raises."""
    raise RuntimeError("boom")


async def _ok_handler(event: Event) -> None:
    """Handler that records the event data."""
    _ok_handler.calls.append(event.data)


_ok_handler.calls: list[dict] = []


@pytest.mark.asyncio
async def test_failing_handler_does_not_block_others(bus: EventBus) -> None:
    """A failing handler must not prevent sibling handlers from executing."""
    _ok_handler.calls = []

    bus.on("test_event", _failing_handler)
    bus.on("test_event", _ok_handler)

    event = Event(name="test_event", data={"key": "value"}, source="test")
    # emit() should complete without raising
    await bus.emit(event)

    # The OK handler should still have been called
    assert len(_ok_handler.calls) == 1
    assert _ok_handler.calls[0]["key"] == "value"


@pytest.mark.asyncio
async def test_emit_returns_normally_when_all_handlers_fail(bus: EventBus) -> None:
    """emit() must not propagate exceptions from handlers."""
    bus.on("test_event", _failing_handler)
    bus.on("test_event", _failing_handler)

    event = Event(name="test_event", data={})
    # Should NOT raise
    await bus.emit(event)


@pytest.mark.asyncio
async def test_handler_exception_logged_with_event_metadata(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """Failing handler exception should be logged with event metadata."""
    bus.on("skill_executed", _failing_handler)

    event = Event(
        name="skill_executed",
        data={"skill_name": "bash"},
        source="ToolExecutor",
        correlation_id="corr-123",
    )

    with caplog.at_level(logging.ERROR, logger="src.core.event_bus"):
        await bus.emit(event)

    # The exception log should reference the event name
    assert any("skill_executed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_no_handlers_is_noop(bus: EventBus) -> None:
    """Emitting an event with no subscribers is a no-op."""
    event = Event(name="unsubscribed_event", data={})
    await bus.emit(event)  # Should not raise


@pytest.mark.asyncio
async def test_closed_bus_ignores_emission(bus: EventBus) -> None:
    """A closed bus silently ignores emit() calls."""
    bus.on("test_event", _ok_handler)
    await bus.close()

    _ok_handler.calls = []
    await bus.emit(Event(name="test_event", data={"x": 1}))
    assert len(_ok_handler.calls) == 0
