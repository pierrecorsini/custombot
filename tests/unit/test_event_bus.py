"""Tests for EventBus handler error isolation and emission rate tracking.

Verifies:
- A failing handler doesn't prevent other handlers from executing
- A failing handler's exception is logged with correct event metadata
- emit() returns normally even when all handlers fail
- Emission rate tracking over sliding windows
- Event storm detection logging when threshold is exceeded
- get_metrics() exposes rate data
- strict_event_names mode rejects unknown events with ValueError (parametrized)
- default mode logs WARNING for unknown event names
- strict mode error message includes all known events
- default mode still delivers unknown events to subscribers
- known events emit no unknown-event warning in default mode
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from src.core.event_bus import KNOWN_EVENTS, Event, EventBus


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

    with caplog.at_level(logging.DEBUG, logger="src.core.event_bus"):
        await bus.emit(event)

    # The exception log should reference the event name
    assert any("skill_executed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_no_handlers_is_noop(bus: EventBus) -> None:
    """Emitting an event with no subscribers is a no-op."""
    event = Event(name="unsubscribed_event", data={})
    await bus.emit(event)  # Should not raise


@pytest.mark.asyncio
async def test_concurrent_emit_failing_handler_isolated(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """Concurrent handlers: one failure must not prevent others from completing.

    Uses asyncio.Event to prove handlers actually run in parallel via
    asyncio.gather.  The failing handler waits until a sibling has started
    before raising — if handlers were sequential this would deadlock.
    """
    started = asyncio.Event()
    results: list[str] = []

    async def _handler_a_failing(event: Event) -> None:
        """Fails after waiting for handler B to start (proves concurrency)."""
        await started.wait()
        raise RuntimeError("handler_a failed")

    async def _handler_b_ok(event: Event) -> None:
        """Signals start, then completes successfully."""
        started.set()
        await asyncio.sleep(0.01)
        results.append("b")

    async def _handler_c_ok(event: Event) -> None:
        """Records that it ran."""
        results.append("c")

    bus.on("test_event", _handler_a_failing)
    bus.on("test_event", _handler_b_ok)
    bus.on("test_event", _handler_c_ok)

    with caplog.at_level(logging.DEBUG, logger="src.core.event_bus"):
        # emit() must return normally — no exception propagated
        await bus.emit(Event(name="test_event", data={}, source="concurrent_test"))

    # Both ok handlers must have completed despite handler A failing
    assert "b" in results, "handler_b should have completed"
    assert "c" in results, "handler_c should have completed"

    # The failure must have been logged with event metadata
    assert any("test_event" in record.message for record in caplog.records), (
        "error should be logged with event name"
    )


@pytest.mark.asyncio
async def test_closed_bus_ignores_emission(bus: EventBus) -> None:
    """A closed bus silently ignores emit() calls."""
    bus.on("test_event", _ok_handler)
    await bus.close()

    _ok_handler.calls = []
    await bus.emit(Event(name="test_event", data={"x": 1}))
    assert len(_ok_handler.calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Backpressure / bounded concurrency
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_backpressure_caps_concurrent_handlers() -> None:
    """emit() must not run more than max_concurrent_handlers at once."""
    max_concurrent = 3
    bus = EventBus(max_concurrent_handlers=max_concurrent)

    peak_concurrent = 0
    current_concurrent = 0

    # Create distinct handler instances so on() doesn't deduplicate them
    for _ in range(10):

        async def _tracking_handler(event: Event) -> None:
            nonlocal peak_concurrent, current_concurrent
            current_concurrent += 1
            peak_concurrent = max(peak_concurrent, current_concurrent)
            await asyncio.sleep(0.05)  # Hold slot open so others queue
            current_concurrent -= 1

        bus.on("test_event", _tracking_handler)

    await bus.emit(Event(name="test_event", data={}))

    assert peak_concurrent <= max_concurrent, (
        f"Peak concurrent {peak_concurrent} exceeded cap {max_concurrent}"
    )
    assert peak_concurrent == max_concurrent, (
        f"Expected to saturate the cap ({max_concurrent}), but peak was {peak_concurrent}"
    )


@pytest.mark.asyncio
async def test_emit_backpressure_all_handlers_still_complete() -> None:
    """Backpressure must not prevent any handler from completing."""
    max_concurrent = 2
    bus = EventBus(max_concurrent_handlers=max_concurrent)

    completed: list[int] = []

    # Create distinct handler instances so on() doesn't deduplicate them
    for i in range(8):

        async def _handler(event: Event, _idx: int = i) -> None:
            await asyncio.sleep(0.02)
            completed.append(_idx)

        bus.on("test_event", _handler)

    await bus.emit(Event(name="test_event", data={}))

    assert len(completed) == 8, "All handlers must complete despite backpressure"


# ─────────────────────────────────────────────────────────────────────────────
# Emission rate tracking
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emission_rate_tracking_in_metrics(bus: EventBus) -> None:
    """Emission rates appear in get_metrics() after events are emitted."""
    now = time.time()
    for i in range(5):
        await bus.emit(Event(name="skill_executed", data={"i": i}, timestamp=now))

    metrics = bus.get_metrics()
    assert "emission_rates" in metrics
    rates = metrics["emission_rates"]
    assert "skill_executed" in rates
    assert rates["skill_executed"]["count_in_window"] == 5
    assert rates["skill_executed"]["rate_per_minute"] > 0


@pytest.mark.asyncio
async def test_emission_rate_sliding_window_expiry() -> None:
    """Emission rate drops after the sliding window expires."""
    window_seconds = 0.2
    bus = EventBus(emission_rate_window_seconds=window_seconds, storm_threshold=0)

    # Emit events with real wall-clock timestamps
    for _ in range(10):
        await bus.emit(Event(name="test_event", data={}))

    metrics_before = bus.get_metrics()
    assert metrics_before["emission_rates"]["test_event"]["count_in_window"] == 10

    # Wait for the window to expire, then emit one more
    await asyncio.sleep(window_seconds + 0.05)
    await bus.emit(Event(name="test_event", data={}))

    metrics_after = bus.get_metrics()
    # Only the 1 recent event should remain in the window
    assert metrics_after["emission_rates"]["test_event"]["count_in_window"] == 1


@pytest.mark.asyncio
async def test_emission_rate_per_event_type_isolation(bus: EventBus) -> None:
    """Rate tracking is independent per event type."""
    now = time.time()
    for _ in range(10):
        await bus.emit(Event(name="skill_executed", data={}, timestamp=now))
    for _ in range(3):
        await bus.emit(Event(name="response_sent", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert metrics["emission_rates"]["skill_executed"]["count_in_window"] == 10
    assert metrics["emission_rates"]["response_sent"]["count_in_window"] == 3


@pytest.mark.asyncio
async def test_emission_rate_disabled_with_zero_threshold() -> None:
    """When storm_threshold=0, rate tracking is still performed but no warnings."""
    bus = EventBus(storm_threshold=0)
    now = time.time()
    for _ in range(200):
        await bus.emit(Event(name="spam_event", data={}, timestamp=now))

    metrics = bus.get_metrics()
    # Rates are still tracked even though storm detection is off
    assert "spam_event" in metrics["emission_rates"]
    assert metrics["emission_rates"]["spam_event"]["count_in_window"] == 200


@pytest.mark.asyncio
async def test_rate_trackers_lru_eviction() -> None:
    """Rate trackers are capped; oldest are evicted when the cap is reached."""
    max_trackers = 5
    bus = EventBus(max_rate_trackers=max_trackers, storm_threshold=0)
    now = time.time()

    # Emit events for 7 unique names — only 5 trackers should be retained
    event_names = [f"event_{i}" for i in range(7)]
    for name in event_names:
        await bus.emit(Event(name=name, data={}, timestamp=now))

    # The LRU dict should have evicted the oldest 2 entries
    assert len(bus._rate_trackers) == max_trackers

    # Most recent 5 should still be present (event_2 through event_6)
    metrics = bus.get_metrics()
    for name in event_names[2:]:
        assert name in metrics["emission_rates"], f"Expected {name} in rates after eviction"

    # Oldest 2 should have been evicted (event_0, event_1)
    for name in event_names[:2]:
        assert name not in metrics["emission_rates"], f"Expected {name} evicted"


@pytest.mark.asyncio
async def test_rate_trackers_lru_promotes_on_reemit() -> None:
    """Re-emitting to an old event name promotes it, preventing its eviction."""
    max_trackers = 3
    bus = EventBus(max_rate_trackers=max_trackers, storm_threshold=0)
    now = time.time()

    # Emit to event_0, event_1, event_2
    for name in ("event_0", "event_1", "event_2"):
        await bus.emit(Event(name=name, data={}, timestamp=now))

    assert len(bus._rate_trackers) == 3

    # Re-emit to event_0 — this should promote it to most-recently-used
    await bus.emit(Event(name="event_0", data={}, timestamp=now))

    # Now emit event_3 which should evict event_1 (LRU), not event_0
    await bus.emit(Event(name="event_3", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert "event_0" in metrics["emission_rates"], "event_0 should survive (was promoted)"
    assert "event_1" not in metrics["emission_rates"], "event_1 should be evicted (LRU)"
    assert len(bus._rate_trackers) == max_trackers


# ─────────────────────────────────────────────────────────────────────────────
# Storm detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_storm_detection_logs_warning(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning is logged when emission rate exceeds the storm threshold."""
    # Default threshold is 100/min with a 60s window.
    # Emit 101 events at the same timestamp to guarantee rate > 100/min.
    now = time.time()
    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        for _ in range(101):
            await bus.emit(Event(name="storm_event", data={}, timestamp=now))

    storm_warnings = [
        r for r in caplog.records if "Event storm detected" in r.message
    ]
    assert len(storm_warnings) >= 1, (
        f"Expected at least 1 storm warning, got {len(storm_warnings)}"
    )
    assert "storm_event" in storm_warnings[0].message


@pytest.mark.asyncio
async def test_no_storm_warning_below_threshold(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """No storm warning is logged when emission rate stays below threshold."""
    now = time.time()
    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        for _ in range(50):
            await bus.emit(Event(name="calm_event", data={}, timestamp=now))

    storm_warnings = [
        r for r in caplog.records if "Event storm detected" in r.message
    ]
    assert len(storm_warnings) == 0, (
        f"Expected no storm warnings, got {len(storm_warnings)}"
    )


@pytest.mark.asyncio
async def test_custom_storm_threshold() -> None:
    """A custom storm_threshold is respected."""
    bus = EventBus(storm_threshold=5.0, emission_rate_window_seconds=60.0)
    now = time.time()
    # Emit 6 events at the same instant → rate = 6 / 60s * 60 = 6/min > 5
    for _ in range(6):
        await bus.emit(Event(name="sensitive_event", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert metrics["emission_rates"]["sensitive_event"]["rate_per_minute"] > 5.0


@pytest.mark.asyncio
async def test_closed_bus_skips_rate_tracking() -> None:
    """A closed bus does not update rate trackers."""
    bus = EventBus(storm_threshold=10.0)
    now = time.time()
    await bus.emit(Event(name="pre_close", data={}, timestamp=now))
    await bus.close()
    await bus.emit(Event(name="post_close", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert "pre_close" in metrics["emission_rates"]
    assert "post_close" not in metrics["emission_rates"]


# ─────────────────────────────────────────────────────────────────────────────
# Strict event name validation
# ─────────────────────────────────────────────────────────────────────────────


async def _record_handler(event: Event) -> None:
    _record_handler.calls.append(event.name)


_record_handler.calls: list[str] = []


@pytest.mark.asyncio
async def test_strict_mode_raises_on_unknown_event_name() -> None:
    """strict_event_names=True raises ValueError for unknown event names."""
    bus = EventBus(strict_event_names=True)

    with pytest.raises(ValueError, match="Unknown event name 'typo_event'"):
        await bus.emit(Event(name="typo_event", data={}))


@pytest.mark.asyncio
async def test_strict_mode_allows_known_event_name() -> None:
    """strict_event_names=True allows known event names without error."""
    bus = EventBus(strict_event_names=True)
    _record_handler.calls = []
    bus.on("skill_executed", _record_handler)

    await bus.emit(Event(name="skill_executed", data={"skill": "bash"}))

    assert _record_handler.calls == ["skill_executed"]


@pytest.mark.asyncio
async def test_default_mode_warns_on_unknown_event_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default mode logs WARNING for unknown event names (no raise)."""
    bus = EventBus()
    _record_handler.calls = []
    bus.on("custom_event", _record_handler)

    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        await bus.emit(Event(name="custom_event", data={}))

    assert _record_handler.calls == ["custom_event"]
    assert any("unknown event 'custom_event'" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize(
    "unknown_name",
    ["typo_event", "skil_executed", "message_recieved", ""],
    ids=["typo", "misspelling", "transposition", "empty"],
)
@pytest.mark.asyncio
async def test_strict_mode_rejects_various_unknown_names(
    unknown_name: str,
) -> None:
    """strict_event_names=True raises ValueError for diverse unknown names."""
    bus = EventBus(strict_event_names=True)

    with pytest.raises(ValueError, match="Unknown event name"):
        await bus.emit(Event(name=unknown_name, data={}))


@pytest.mark.asyncio
async def test_strict_mode_error_message_includes_known_events() -> None:
    """ValueError message in strict mode lists all known events."""
    bus = EventBus(strict_event_names=True)

    with pytest.raises(ValueError) as exc_info:
        await bus.emit(Event(name="bogus_event", data={}))

    msg = str(exc_info.value)
    for known in KNOWN_EVENTS:
        assert known in msg, f"Known event '{known}' missing from error message"


@pytest.mark.asyncio
async def test_default_mode_delivers_unknown_event_to_subscribers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default mode emits unknown event to subscribers despite WARNING log."""
    bus = EventBus()
    received: list[str] = []
    bus.on("completely_unknown", lambda e: received.append(e.name))

    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        await bus.emit(Event(name="completely_unknown", data={"key": "val"}))

    assert received == ["completely_unknown"], "Unknown event should still reach subscribers"
    assert any(
        "unknown event 'completely_unknown'" in r.message.lower() for r in caplog.records
    ), "Should log WARNING for unknown event name"


@pytest.mark.asyncio
async def test_default_mode_no_warning_for_known_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default mode emits no WARNING when all events are in KNOWN_EVENTS."""
    bus = EventBus()
    _record_handler.calls = []
    bus.on("skill_executed", _record_handler)

    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        await bus.emit(Event(name="skill_executed", data={}))

    assert _record_handler.calls == ["skill_executed"]
    assert not any(
        "unknown event" in r.message.lower() for r in caplog.records
    ), "Known events should not trigger unknown-event warning"


# ─────────────────────────────────────────────────────────────────────────────
# Lazy semaphore initialization
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emit_semaphore_lazy_initialized() -> None:
    """_emit_semaphore is None after construction and created on multi-handler emit."""
    bus = EventBus(max_concurrent_handlers=5)
    assert bus._emit_semaphore is None, "Semaphore should not exist before first emit"

    async def _noop(event: Event) -> None:
        pass

    # Single-handler fast path skips the semaphore — add two distinct
    # handlers to force the gather+semaphore code path.
    async def _noop1(event: Event) -> None:
        pass

    async def _noop2(event: Event) -> None:
        pass

    bus.on("skill_executed", _noop1)
    bus.on("skill_executed", _noop2)
    await bus.emit(Event(name="skill_executed", data={}))

    assert bus._emit_semaphore is not None, "Semaphore should be created after multi-handler emit"


@pytest.mark.asyncio
async def test_emit_semaphore_not_created_without_handlers() -> None:
    """_emit_semaphore stays None when emit is called with no handlers."""
    bus = EventBus()
    assert bus._emit_semaphore is None

    await bus.emit(Event(name="unsubscribed_event", data={}))
    assert bus._emit_semaphore is None, "No handlers means no semaphore needed"


@pytest.mark.asyncio
async def test_lazy_semaphore_respects_max_concurrent() -> None:
    """Lazy-init semaphore still enforces the max_concurrent_handlers cap."""
    max_concurrent = 2
    bus = EventBus(max_concurrent_handlers=max_concurrent)

    peak_concurrent = 0
    current_concurrent = 0

    for _ in range(6):

        async def _tracking(event: Event) -> None:
            nonlocal peak_concurrent, current_concurrent
            current_concurrent += 1
            peak_concurrent = max(peak_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            current_concurrent -= 1

        bus.on("test_event", _tracking)

    await bus.emit(Event(name="test_event", data={}))
    assert peak_concurrent == max_concurrent


# ─────────────────────────────────────────────────────────────────────────────
# Event data validation (_validate_data_serializable)
# ─────────────────────────────────────────────────────────────────────────────


class TestEventDataValidation:
    """Tests for data validation via _validate_data_serializable in EventBus.emit().

    Validation was moved from Event.__post_init__ to EventBus.emit() so that
    events that are never emitted (e.g. bus closed, no handlers) avoid the
    json.dumps() serialization cost at construction time.
    """

    @pytest.mark.asyncio
    async def test_serializable_data_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """JSON-safe data produces no warnings."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(
                Event(name="test_event", data={"key": "value", "count": 42, "ratio": 3.14})
            )
        assert not any(
            "non-JSON-serializable" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_empty_data_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty data dict skips validation entirely."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={}))
        assert not any(
            "non-JSON-serializable" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_default_data_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Default data (empty dict) skips validation."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event"))
        assert not any(
            "non-JSON-serializable" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_file_handle_warns_with_key_and_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A file-handle value produces a warning naming the key and type."""
        bus = EventBus()
        fh = open(__file__, "r")
        try:
            with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
                await bus.emit(
                    Event(name="test_event", data={"handle": fh, "ok_key": "fine"})
                )
            msgs = [r.message for r in caplog.records if "non-JSON-serializable" in r.message]
            assert len(msgs) >= 1
            msg = msgs[0]
            assert "handle=" in msg, f"Warning should name the bad key, got: {msg}"
            assert "TextIOWrapper" in msg or "BufferedReader" in msg, (
                f"Warning should name the type, got: {msg}"
            )
        finally:
            fh.close()

    @pytest.mark.asyncio
    async def test_set_value_warns_with_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A set value (not JSON-serializable) produces a warning with key and type."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"tags": {1, 2, 3}}))
        msgs = [r.message for r in caplog.records if "non-JSON-serializable" in r.message]
        assert len(msgs) >= 1
        assert "tags=" in msgs[0]
        assert "set" in msgs[0]

    @pytest.mark.asyncio
    async def test_bytes_value_warns_with_type(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bytes value produces a warning with key and type."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"payload": b"\x00\x01"}))
        msgs = [r.message for r in caplog.records if "non-JSON-serializable" in r.message]
        assert len(msgs) >= 1
        assert "payload=" in msgs[0]
        assert "bytes" in msgs[0]

    @pytest.mark.asyncio
    async def test_multiple_bad_keys_all_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Multiple non-serializable keys are all listed in the warning."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(
                Event(
                    name="test_event",
                    data={"good": 1, "bad_set": {1, 2}, "bad_bytes": b"x"},
                )
            )
        msgs = [r.message for r in caplog.records if "non-JSON-serializable" in r.message]
        assert len(msgs) >= 1
        msg = msgs[0]
        assert "bad_set=" in msg
        assert "bad_bytes=" in msg

    @pytest.mark.asyncio
    async def test_nested_non_serializable_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-serializable value nested inside a dict is still caught."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(
                Event(
                    name="test_event",
                    data={"nested": {"inner": {1, 2}}},
                )
            )
        msgs = [r.message for r in caplog.records if "non-JSON-serializable" in r.message]
        assert len(msgs) >= 1

    @pytest.mark.asyncio
    async def test_event_still_emitted_on_bad_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Validation is warn-only — Event is still emitted after warning."""
        bus = EventBus()
        received: list[dict] = []

        async def _capture(event: Event) -> None:
            received.append(event.data)

        bus.on("test_event", _capture)
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"bad": {1, 2}}))
        assert received == [{"bad": {1, 2}}]

    @pytest.mark.asyncio
    async def test_complex_serializable_data_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Complex but JSON-safe nested structures produce no warnings."""
        bus = EventBus()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(
                Event(
                    name="test_event",
                    data={
                        "list": [1, 2, 3],
                        "nested": {"a": {"b": [4, 5]}},
                        "null": None,
                        "bool": True,
                    },
                )
            )
        assert not any(
            "non-JSON-serializable" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_closed_bus_skips_validation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Validation is skipped entirely when the bus is closed."""
        bus = EventBus()
        await bus.close()
        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"bad": {1, 2}}))
        # Only the "bus closed" warning should appear — no serialization warning
        serializable_warnings = [
            r for r in caplog.records if "non-JSON-serializable" in r.message
        ]
        assert len(serializable_warnings) == 0, (
            "Validation should be skipped when bus is closed"
        )

    @pytest.mark.asyncio
    async def test_circular_dict_in_event_data_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Circular dict in event data logs warning instead of RecursionError."""
        bus = EventBus()
        circular: dict[str, Any] = {}
        circular["self"] = circular

        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"circular": circular}))

        # Verify a warning about circular references was logged
        assert any("circular" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_circular_list_in_event_data_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Circular list in event data logs warning instead of RecursionError."""
        bus = EventBus()
        circular: list[Any] = []
        circular.append(circular)

        with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
            await bus.emit(Event(name="test_event", data={"circular": circular}))

        # Verify a warning about circular references was logged
        assert any("circular" in r.message.lower() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Rate-tracker memory usage in get_metrics()
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_includes_rate_tracker_stats(bus: EventBus) -> None:
    """get_metrics() includes rate_tracker_stats with tracked count and max."""
    now = time.time()
    await bus.emit(Event(name="skill_executed", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert "rate_tracker_stats" in metrics, "get_metrics() should include rate_tracker_stats"

    stats = metrics["rate_tracker_stats"]
    assert "tracked_event_type_count" in stats
    assert "max_tracked_event_types" in stats
    assert stats["tracked_event_type_count"] == 1, "One event type should be tracked"
    assert stats["max_tracked_event_types"] == bus._max_rate_trackers


@pytest.mark.asyncio
async def test_rate_tracker_stats_count_matches_emitted_types() -> None:
    """tracked_event_type_count reflects the number of distinct event types emitted."""
    max_trackers = 10
    bus = EventBus(max_rate_trackers=max_trackers, storm_threshold=0)
    now = time.time()

    for i in range(4):
        await bus.emit(Event(name=f"unique_event_{i}", data={}, timestamp=now))

    metrics = bus.get_metrics()
    assert metrics["rate_tracker_stats"]["tracked_event_type_count"] == 4
    assert metrics["rate_tracker_stats"]["max_tracked_event_types"] == max_trackers


@pytest.mark.asyncio
async def test_rate_tracker_stats_at_cap_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning is logged when tracked_event_type_count reaches max_rate_trackers."""
    max_trackers = 3
    bus = EventBus(max_rate_trackers=max_trackers, storm_threshold=0)
    now = time.time()

    # Fill exactly to cap
    for i in range(max_trackers):
        await bus.emit(Event(name=f"event_{i}", data={}, timestamp=now))

    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        metrics = bus.get_metrics()

    assert metrics["rate_tracker_stats"]["tracked_event_type_count"] == max_trackers
    assert metrics["rate_tracker_stats"]["max_tracked_event_types"] == max_trackers

    # Warning should be emitted since count == max
    cap_warnings = [
        r for r in caplog.records
        if "rate-tracker capacity reached" in r.message
    ]
    assert len(cap_warnings) >= 1, (
        "Expected a warning when tracked count equals max_rate_trackers"
    )


@pytest.mark.asyncio
async def test_rate_tracker_stats_below_cap_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warning is logged when tracked_event_type_count is below the cap."""
    max_trackers = 10
    bus = EventBus(max_rate_trackers=max_trackers, storm_threshold=0)
    now = time.time()

    for i in range(3):
        await bus.emit(Event(name=f"event_{i}", data={}, timestamp=now))

    with caplog.at_level(logging.WARNING, logger="src.core.event_bus"):
        metrics = bus.get_metrics()

    assert metrics["rate_tracker_stats"]["tracked_event_type_count"] == 3
    cap_warnings = [
        r for r in caplog.records
        if "rate-tracker capacity reached" in r.message
    ]
    assert len(cap_warnings) == 0, (
        "No warning expected when tracked count is below max_rate_trackers"
    )


@pytest.mark.asyncio
async def test_rate_tracker_stats_zero_when_no_events_emitted(bus: EventBus) -> None:
    """tracked_event_type_count is 0 when no events have been emitted."""
    metrics = bus.get_metrics()
    assert metrics["rate_tracker_stats"]["tracked_event_type_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Emission / invocation count LRU bounding
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emission_counts_lru_eviction() -> None:
    """Emission counts are capped; oldest event names are evicted when the cap is reached."""
    max_names = 5
    bus = EventBus(max_tracked_event_names=max_names, storm_threshold=0)
    now = time.time()

    # Emit events for 7 unique names — only 5 should be retained
    event_names = [f"count_event_{i}" for i in range(7)]
    for name in event_names:
        await bus.emit(Event(name=name, data={}, timestamp=now))

    assert len(bus._emission_counts) == max_names

    metrics = bus.get_metrics()
    emissions = metrics["emissions"]
    # Most recent 5 should still be present (count_event_2 through count_event_6)
    for name in event_names[2:]:
        assert name in emissions, f"Expected {name} in emissions after eviction"

    # Oldest 2 should have been evicted (count_event_0, count_event_1)
    for name in event_names[:2]:
        assert name not in emissions, f"Expected {name} evicted from emissions"


@pytest.mark.asyncio
async def test_invocation_counts_lru_eviction() -> None:
    """Handler invocation counts are capped; oldest event names are evicted."""
    max_names = 4
    bus = EventBus(max_tracked_event_names=max_names, storm_threshold=0)

    # Subscribe a handler to all event names so invocations are tracked
    async def _counter(event: Event) -> None:
        pass

    event_names = [f"inv_event_{i}" for i in range(6)]
    for name in event_names:
        bus.on(name, _counter)
        await bus.emit(Event(name=name, data={}))

    assert len(bus._handler_invocation_counts) == max_names

    metrics = bus.get_metrics()
    invocations = metrics["invocations"]
    # Most recent 4 should survive (inv_event_2 through inv_event_5)
    for name in event_names[2:]:
        assert name in invocations, f"Expected {name} in invocations after eviction"

    # Oldest 2 should have been evicted
    for name in event_names[:2]:
        assert name not in invocations, f"Expected {name} evicted from invocations"


@pytest.mark.asyncio
async def test_emission_counts_lru_promotes_on_reemit() -> None:
    """Re-emitting to an old event name promotes it, preventing its eviction."""
    max_names = 3
    bus = EventBus(max_tracked_event_names=max_names, storm_threshold=0)

    # Emit to event_0, event_1, event_2
    for name in ("ev_0", "ev_1", "ev_2"):
        await bus.emit(Event(name=name, data={}))

    assert len(bus._emission_counts) == 3

    # Re-emit to ev_0 — this should promote it to most-recently-used
    await bus.emit(Event(name="ev_0", data={}))

    # Now emit ev_3 which should evict ev_1 (LRU), not ev_0
    await bus.emit(Event(name="ev_3", data={}))

    metrics = bus.get_metrics()
    assert "ev_0" in metrics["emissions"], "ev_0 should survive (was promoted)"
    assert "ev_1" not in metrics["emissions"], "ev_1 should be evicted (LRU)"
    assert len(bus._emission_counts) == max_names


@pytest.mark.asyncio
async def test_emission_counts_bounded_by_lru() -> None:
    """Emission counts are bounded by max_tracked_event_names LRU cap."""
    max_tracked = 3
    bus = EventBus(max_tracked_event_names=max_tracked)

    # Emit events with different names
    for i in range(5):
        await bus.emit(Event(name=f"event_{i}", data={}))

    metrics = bus.get_metrics()
    emissions = metrics["emissions"]
    # Should have at most max_tracked entries
    assert len(emissions) <= max_tracked
    # Most recent events should be present
    assert "event_4" in emissions
    assert "event_3" in emissions
    # Oldest events should have been evicted
    assert "event_0" not in emissions
    assert "event_1" not in emissions


# ─────────────────────────────────────────────────────────────────────────────
# BaseException hardening — _safe_call catches SystemExit / GeneratorExit
# ─────────────────────────────────────────────────────────────────────────────


async def _system_exit_handler(event: Event) -> None:
    """Handler that raises SystemExit."""
    raise SystemExit(1)


async def _generator_exit_handler(event: Event) -> None:
    """Handler that raises GeneratorExit."""
    raise GeneratorExit("forced")


async def _keyboard_interrupt_handler(event: Event) -> None:
    """Handler that raises KeyboardInterrupt."""
    raise KeyboardInterrupt("user press")


_ok_for_base: list[dict] = []


async def _ok_handler_base(event: Event) -> None:
    """Handler that records calls, shared across base-exception tests."""
    _ok_for_base.append(event.data)


@pytest.mark.asyncio
async def test_system_exit_handler_does_not_crash_bus(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """SystemExit from a handler must be caught and logged, not crash the bus."""
    _ok_for_base.clear()
    bus.on("test_event", _system_exit_handler)
    bus.on("test_event", _ok_handler_base)

    event = Event(name="test_event", data={"k": "v"}, source="test")
    with caplog.at_level(logging.DEBUG, logger="src.core.event_bus"):
        await bus.emit(event)

    # Bus should NOT have crashed — sibling handler ran
    assert len(_ok_for_base) == 1
    # The SystemExit should have been logged as non-critical
    assert any("test_event" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_generator_exit_handler_does_not_crash_bus(
    bus: EventBus, caplog: pytest.LogCaptureFixture
) -> None:
    """GeneratorExit from a handler must be caught and logged, not crash the bus."""
    _ok_for_base.clear()
    bus.on("test_event", _generator_exit_handler)
    bus.on("test_event", _ok_handler_base)

    event = Event(name="test_event", data={"k": "v"}, source="test")
    with caplog.at_level(logging.DEBUG, logger="src.core.event_bus"):
        await bus.emit(event)

    assert len(_ok_for_base) == 1
    assert any("test_event" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_keyboard_interrupt_propagates_from_safe_call() -> None:
    """KeyboardInterrupt must propagate through _safe_call (not swallowed)."""
    from src.core.event_bus import _safe_call

    event = Event(name="test_event", data={})
    with pytest.raises(KeyboardInterrupt):
        await _safe_call(_keyboard_interrupt_handler, event)


@pytest.mark.asyncio
async def test_cancelled_error_propagates_from_safe_call() -> None:
    """CancelledError must propagate through _safe_call (not swallowed)."""
    from src.core.event_bus import _safe_call

    async def _cancel_handler(event: Event) -> None:
        raise asyncio.CancelledError()

    event = Event(name="test_event", data={})
    with pytest.raises(asyncio.CancelledError):
        await _safe_call(_cancel_handler, event)
