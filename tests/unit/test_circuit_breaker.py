"""
Tests for src/utils/circuit_breaker.py — concurrent HALF_OPEN state transitions.

Verifies that the CircuitBreaker behaves correctly when multiple threads
probe the circuit simultaneously during the HALF_OPEN state:
  (a) a single success closes the breaker
  (b) a single failure re-opens it
  (c) concurrent record_success / record_failure never corrupt internal state
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from src.utils.circuit_breaker import CircuitBreaker, CircuitState


def _force_to_open(breaker: CircuitBreaker) -> None:
    """Drive a CLOSED breaker to OPEN by recording threshold failures."""
    for _ in range(breaker._failure_threshold):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


def _force_to_half_open(breaker: CircuitBreaker) -> None:
    """Transition an OPEN breaker to HALF_OPEN by expiring the cooldown."""
    assert breaker.state == CircuitState.OPEN
    with patch("src.utils.circuit_breaker.time.monotonic", return_value=breaker._last_failure_time + breaker._cooldown_seconds + 1):
        assert breaker.is_open() is False
    assert breaker.state == CircuitState.HALF_OPEN


# ─────────────────────────────────────────────────────────────────────────────
# Single-probe transitions
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenSingleProbe:
    """Verify single-probe success closes and single-probe failure re-opens."""

    def test_one_success_closes_from_half_open(self):
        """A single record_success in HALF_OPEN should transition to CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        breaker.record_success()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_one_failure_reopens_from_half_open(self):
        """A single record_failure in HALF_OPEN should transition back to OPEN."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        breaker.record_failure()

        assert breaker.state == CircuitState.OPEN

    def test_half_open_allows_probe(self):
        """is_open() should return False in HALF_OPEN, allowing the probe call."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        assert breaker.is_open() is False


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — all succeed
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentSuccess:
    """Multiple threads succeed simultaneously in HALF_OPEN — breaker must close."""

    def test_all_succeed_closes(self):
        """When all concurrent probes succeed, breaker ends CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        results: list[bool] = [False] * num_threads

        def probe_success(idx: int) -> None:
            barrier.wait()
            results[idx] = breaker.is_open()
            breaker.record_success()

        threads = [threading.Thread(target=probe_success, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All probes were allowed through
        assert all(r is False for r in results)
        # Breaker should be CLOSED — last writer wins but any success closes
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_state_remains_valid_under_concurrent_success(self):
        """State must be a valid CircuitState enum after concurrent successes."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        num_threads = 20
        barrier = threading.Barrier(num_threads)

        def probe() -> None:
            barrier.wait()
            breaker.is_open()
            breaker.record_success()

        threads = [threading.Thread(target=probe) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert breaker.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN, CircuitState.OPEN)
        assert breaker.state == CircuitState.CLOSED


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — all fail
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentFailure:
    """Multiple threads fail simultaneously in HALF_OPEN — breaker must re-open."""

    def test_all_fail_reopens(self):
        """When all concurrent probes fail, breaker ends OPEN.

        Note: after the first record_failure() transitions HALF_OPEN → OPEN,
        subsequent is_open() calls may return True (breaker is open, cooldown
        not elapsed).  This is correct — the first failure immediately re-opens.
        """
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        num_threads = 10
        barrier = threading.Barrier(num_threads)

        def probe_failure() -> None:
            barrier.wait()
            breaker.is_open()
            breaker.record_failure()

        threads = [threading.Thread(target=probe_failure) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert breaker.state == CircuitState.OPEN


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — mixed success and failure
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentMixed:
    """Mixed success/failure probes — state must remain a valid enum and be consistent."""

    def test_mixed_probes_state_remains_valid(self):
        """Under mixed success/failure, state is always a valid CircuitState."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)

        for _ in range(50):
            _force_to_open(breaker)
            _force_to_half_open(breaker)

            num_threads = 8
            barrier = threading.Barrier(num_threads)

            def probe_success() -> None:
                barrier.wait()
                breaker.is_open()
                breaker.record_success()

            def probe_failure() -> None:
                barrier.wait()
                breaker.is_open()
                breaker.record_failure()

            threads = [
                threading.Thread(target=probe_success),
                threading.Thread(target=probe_failure),
                threading.Thread(target=probe_success),
                threading.Thread(target=probe_failure),
                threading.Thread(target=probe_success),
                threading.Thread(target=probe_failure),
                threading.Thread(target=probe_success),
                threading.Thread(target=probe_failure),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert breaker.state in (CircuitState.CLOSED, CircuitState.OPEN)

    def test_failure_count_never_negative(self):
        """failure_count must never go negative under concurrent operations."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)

        for _ in range(20):
            _force_to_open(breaker)
            _force_to_half_open(breaker)

            barrier = threading.Barrier(6)

            def succeed() -> None:
                barrier.wait()
                breaker.is_open()
                breaker.record_success()

            def fail() -> None:
                barrier.wait()
                breaker.is_open()
                breaker.record_failure()

            threads = [
                threading.Thread(target=succeed),
                threading.Thread(target=succeed),
                threading.Thread(target=succeed),
                threading.Thread(target=fail),
                threading.Thread(target=fail),
                threading.Thread(target=fail),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert breaker.failure_count >= 0

    def test_concurrent_records_during_half_open_no_exceptions(self):
        """Rapid concurrent record_success/record_failure must not raise exceptions."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)

        num_threads = 30
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def probe(idx: int) -> None:
            barrier.wait()
            try:
                breaker.is_open()
                if idx % 2 == 0:
                    breaker.record_success()
                else:
                    breaker.record_failure()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=probe, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert breaker.state in (CircuitState.CLOSED, CircuitState.OPEN)


# ─────────────────────────────────────────────────────────────────────────────
# State property thread-safety
# ─────────────────────────────────────────────────────────────────────────────


class TestCircuitBreakerStateProperty:
    """Verify the state property is readable without corruption during writes."""

    def test_state_always_valid_enum_during_concurrent_writes(self):
        """Rapid concurrent writes should never produce an invalid state value."""
        breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=10)

        num_writers = 10
        num_readers = 10
        barrier = threading.Barrier(num_writers + num_readers)
        observed_states: list[CircuitState] = []
        stop_event = threading.Event()

        def writer(idx: int) -> None:
            barrier.wait()
            for _ in range(100):
                if idx % 2 == 0:
                    breaker.record_success()
                else:
                    breaker.record_failure()

        def reader() -> None:
            barrier.wait()
            for _ in range(100):
                state = breaker.state
                observed_states.append(state)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_writers)]
        threads += [threading.Thread(target=reader) for _ in range(num_readers)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every observed state must be a valid CircuitState enum member
        valid_states = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}
        for state in observed_states:
            assert state in valid_states


# ─────────────────────────────────────────────────────────────────────────────
# force_close() — health-probe-driven recovery
# ─────────────────────────────────────────────────────────────────────────────


class TestForceClose:
    """Verify force_close() transitions from OPEN and HALF_OPEN to CLOSED."""

    def test_force_close_from_open(self):
        """force_close() should transition OPEN → CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        _force_to_open(breaker)
        assert breaker.state == CircuitState.OPEN

        breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_force_close_from_half_open(self):
        """force_close() should also transition HALF_OPEN → CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        _force_to_open(breaker)
        _force_to_half_open(breaker)
        assert breaker.state == CircuitState.HALF_OPEN

        breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_force_close_from_closed_is_noop(self):
        """force_close() on an already CLOSED breaker should be a no-op."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        assert breaker.state == CircuitState.CLOSED

        breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
