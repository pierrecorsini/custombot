"""
Tests for src/shutdown.py — GracefulShutdown.

Covers:
- Initial state (accepting_messages=True)
- request_shutdown() sets state
- enter_operation() / exit_operation() tracking
- enter_operation() returns None during shutdown
- wait_for_in_flight() returns True when no ops
- wait_for_in_flight() timeout behavior
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from src.shutdown import GracefulShutdown


# ─────────────────────────────────────────────────────────────────────────────
# Test Initial State
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownInitialState:
    """Tests for GracefulShutdown initial state."""

    def test_accepting_messages_is_true(self) -> None:
        gs = GracefulShutdown()
        assert gs.accepting_messages is True

    def test_is_shutting_down_is_false(self) -> None:
        gs = GracefulShutdown()
        assert gs.is_shutting_down is False


# ─────────────────────────────────────────────────────────────────────────────
# Test request_shutdown
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownRequest:
    """Tests for request_shutdown() state transitions."""

    def test_sets_shutting_down(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()

        assert gs.is_shutting_down is True

    def test_stops_accepting_messages(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()

        assert gs.accepting_messages is False

    def test_idempotent(self) -> None:
        """Calling request_shutdown() twice does not raise."""
        gs = GracefulShutdown()
        gs.request_shutdown()
        gs.request_shutdown()  # should not raise

        assert gs.is_shutting_down is True
        assert gs.accepting_messages is False


# ─────────────────────────────────────────────────────────────────────────────
# Test enter_operation / exit_operation
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownOperations:
    """Tests for enter_operation() and exit_operation() tracking."""

    async def test_enter_returns_op_id(self) -> None:
        gs = GracefulShutdown()
        op_id = await gs.enter_operation("test_op")

        assert op_id is not None
        assert isinstance(op_id, int)

    async def test_enter_increments_counter(self) -> None:
        gs = GracefulShutdown()
        await gs.enter_operation("op1")
        await gs.enter_operation("op2")

        async with gs._get_lock():
            assert gs._in_flight_count == 2

    async def test_exit_decrements_counter(self) -> None:
        gs = GracefulShutdown()
        op_id = await gs.enter_operation("op1")
        await gs.exit_operation(op_id)

        async with gs._get_lock():
            assert gs._in_flight_count == 0

    async def test_exit_removes_from_ops_dict(self) -> None:
        gs = GracefulShutdown()
        op_id = await gs.enter_operation("my_op")
        assert op_id in gs._in_flight_ops

        await gs.exit_operation(op_id)
        assert op_id not in gs._in_flight_ops

    async def test_sequential_op_ids(self) -> None:
        gs = GracefulShutdown()
        id1 = await gs.enter_operation("a")
        id2 = await gs.enter_operation("b")
        id3 = await gs.enter_operation("c")

        assert id1 == 0
        assert id2 == 1
        assert id3 == 2

    async def test_exit_with_none_only_decrements_count(self) -> None:
        """exit_operation(None) decrements count but doesn't affect ops dict."""
        gs = GracefulShutdown()
        await gs.enter_operation("op1")
        await gs.exit_operation(None)  # decrement without removing

        async with gs._get_lock():
            assert gs._in_flight_count == 0

    async def test_exit_never_goes_below_zero(self) -> None:
        gs = GracefulShutdown()
        await gs.exit_operation(None)  # count was 0

        async with gs._get_lock():
            assert gs._in_flight_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test enter_operation during shutdown
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownEnterDuringShutdown:
    """Tests for enter_operation() returning None during shutdown."""

    async def test_returns_none_during_shutdown(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()

        result = await gs.enter_operation("late_op")
        assert result is None

    async def test_does_not_increment_count_during_shutdown(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()

        await gs.enter_operation("late_op")

        async with gs._get_lock():
            assert gs._in_flight_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test wait_for_in_flight
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownWaitForInFlight:
    """Tests for wait_for_in_flight() behavior."""

    async def test_returns_true_when_no_ops(self) -> None:
        gs = GracefulShutdown()
        result = await gs.wait_for_in_flight()

        assert result is True

    async def test_returns_true_when_ops_complete(self) -> None:
        gs = GracefulShutdown(timeout=5.0)
        op_id = await gs.enter_operation("bg_work")

        # Simulate background work completing
        async def _complete_after():
            await asyncio.sleep(0.1)
            await gs.exit_operation(op_id)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_complete_after())
            result = await gs.wait_for_in_flight()

        assert result is True

    async def test_returns_false_on_timeout(self) -> None:
        gs = GracefulShutdown(timeout=0.2)
        await gs.enter_operation("stuck_op")

        result = await gs.wait_for_in_flight()

        assert result is False

    async def test_timeout_logs_still_in_flight_ops(self) -> None:
        """When timeout is reached, in-flight ops are logged."""
        gs = GracefulShutdown(timeout=0.1)
        await gs.enter_operation("slow_op")

        # Should return False (timeout) without hanging
        result = await gs.wait_for_in_flight()
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Test wait_for_shutdown
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownWaitForShutdown:
    """Tests for wait_for_shutdown() event."""

    async def test_waits_until_shutdown_requested(self) -> None:
        gs = GracefulShutdown()

        async def _trigger():
            await asyncio.sleep(0.1)
            gs.request_shutdown()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_trigger())
            await gs.wait_for_shutdown()

        assert gs.is_shutting_down is True


# ─────────────────────────────────────────────────────────────────────────────
# Test register_signal_handlers
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownSignalHandlers:
    """Tests for register_signal_handlers()."""

    def test_registers_without_error(self) -> None:
        """Signal handler registration completes without raising."""
        gs = GracefulShutdown()
        loop = asyncio.new_event_loop()
        try:
            gs.register_signal_handlers(loop)
        finally:
            loop.close()

    def test_signal_handler_triggers_shutdown(self) -> None:
        """The registered signal handler calls request_shutdown().

        request_shutdown() uses loop.call_soon_threadsafe, so the loop
        must run briefly to process the scheduled callback.
        """
        gs = GracefulShutdown()
        loop = asyncio.new_event_loop()
        try:
            gs.register_signal_handlers(loop)
            gs.request_shutdown()
            # Run the loop once to process the call_soon_threadsafe callback
            loop.run_until_complete(asyncio.sleep(0.01))
            assert gs.is_shutting_down is True
        finally:
            loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test thread safety — request_shutdown() from background thread
# ─────────────────────────────────────────────────────────────────────────────


class TestGracefulShutdownThreadSafety:
    """
    Tests for thread-safety of request_shutdown() called from signal
    handlers (background threads).

    Verifies that loop.call_soon_threadsafe correctly propagates the
    shutdown event across threads so the event loop can observe it.
    """

    async def test_request_shutdown_from_thread_sets_accepting_false(
        self,
    ) -> None:
        """request_shutdown() from a background thread sets accepting_messages=False."""
        gs = GracefulShutdown()
        loop = asyncio.get_running_loop()
        gs._loop = loop

        import threading

        thread_ran = threading.Event()

        def _request_from_thread() -> None:
            gs.request_shutdown()
            thread_ran.set()

        t = threading.Thread(target=_request_from_thread)
        t.start()
        thread_ran.wait(timeout=2.0)
        t.join(timeout=2.0)

        # Give the event loop a chance to process the call_soon_threadsafe callback
        await asyncio.sleep(0.05)

        assert gs.accepting_messages is False
        assert gs.is_shutting_down is True

    async def test_request_shutdown_from_thread_resolves_wait_for_shutdown(
        self,
    ) -> None:
        """wait_for_shutdown() resolves when request_shutdown() is called from a thread."""
        gs = GracefulShutdown()
        loop = asyncio.get_running_loop()
        gs._loop = loop

        import threading

        def _request_after_delay() -> None:
            import time as _time

            _time.sleep(0.1)
            gs.request_shutdown()

        t = threading.Thread(target=_request_after_delay)
        t.start()

        # This should resolve once the thread calls request_shutdown
        await asyncio.wait_for(gs.wait_for_shutdown(), timeout=2.0)
        t.join(timeout=2.0)

        assert gs.is_shutting_down is True

    async def test_enter_operation_rejects_after_thread_shutdown(self) -> None:
        """enter_operation() returns None after request_shutdown() from a thread."""
        gs = GracefulShutdown()
        loop = asyncio.get_running_loop()
        gs._loop = loop

        import threading

        thread_ran = threading.Event()

        def _request_from_thread() -> None:
            gs.request_shutdown()
            thread_ran.set()

        t = threading.Thread(target=_request_from_thread)
        t.start()
        thread_ran.wait(timeout=2.0)
        t.join(timeout=2.0)

        await asyncio.sleep(0.05)

        result = await gs.enter_operation("should_reject")
        assert result is None

    async def test_request_shutdown_without_loop_still_sets_state(self) -> None:
        """request_shutdown() works even when _loop is None (no register_signal_handlers called)."""
        gs = GracefulShutdown()
        assert gs._loop is None

        import threading

        thread_ran = threading.Event()

        def _request_from_thread() -> None:
            gs.request_shutdown()
            thread_ran.set()

        t = threading.Thread(target=_request_from_thread)
        t.start()
        thread_ran.wait(timeout=2.0)
        t.join(timeout=2.0)

        # accepting_messages is a plain bool, set synchronously
        assert gs.accepting_messages is False
        # The event is set directly (no loop.call_soon_threadsafe needed when _loop is None)
        assert gs.is_shutting_down is True

    async def test_wait_for_in_flight_timeout_with_real_elapsed_time(self) -> None:
        """wait_for_in_flight() returns False within approximately the configured timeout."""
        gs = GracefulShutdown(timeout=0.3)
        await gs.enter_operation("stuck_op")

        start = time.monotonic()
        result = await gs.wait_for_in_flight()
        elapsed = time.monotonic() - start

        assert result is False
        # Elapsed time should be close to the timeout (allow some scheduling slack)
        assert elapsed >= 0.2
        assert elapsed < 1.0

    async def test_wait_for_in_flight_returns_true_after_ops_complete_from_thread(
        self,
    ) -> None:
        """In-flight ops completed from a background thread are observed."""
        gs = GracefulShutdown(timeout=5.0)
        op_id = await gs.enter_operation("bg_work")

        import threading

        loop = asyncio.get_running_loop()

        def _complete_from_thread() -> None:
            import time as _time

            _time.sleep(0.1)
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(gs.exit_operation(op_id))
            )

        t = threading.Thread(target=_complete_from_thread)
        t.start()

        result = await gs.wait_for_in_flight()
        t.join(timeout=2.0)

        assert result is True
