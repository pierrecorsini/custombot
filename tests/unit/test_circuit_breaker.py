"""
Tests for src/utils/circuit_breaker.py — concurrent HALF_OPEN state transitions.

Verifies that the CircuitBreaker behaves correctly when multiple coroutines
probe the circuit simultaneously during the HALF_OPEN state:
  (a) a single success closes the breaker
  (b) a single failure re-opens it
  (c) concurrent record_success / record_failure never corrupt internal state

Note: The CircuitBreaker API is fully async (uses AsyncLock). All tests use
async/await.  For concurrent probes we use ``asyncio.gather`` instead of
threading.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.utils.circuit_breaker import CircuitBreaker, CircuitState


async def _force_to_open(breaker: CircuitBreaker) -> None:
    """Drive a CLOSED breaker to OPEN by recording threshold failures."""
    for _ in range(breaker._failure_threshold):
        await breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


async def _force_to_half_open(breaker: CircuitBreaker) -> None:
    """Transition an OPEN breaker to HALF_OPEN by expiring the cooldown."""
    assert breaker.state == CircuitState.OPEN
    # Set last_failure_time far enough in the past that cooldown has elapsed
    breaker._last_failure_time = 0
    assert await breaker.is_open() is False
    assert breaker.state == CircuitState.HALF_OPEN


# ─────────────────────────────────────────────────────────────────────────────
# Single-probe transitions
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenSingleProbe:
    """Verify single-probe success closes and single-probe failure re-opens."""

    async def test_one_success_closes_from_half_open(self):
        """A single record_success in HALF_OPEN should transition to CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        await breaker.record_success()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_one_failure_reopens_from_half_open(self):
        """A single record_failure in HALF_OPEN should transition back to OPEN."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        await breaker.record_failure()

        assert breaker.state == CircuitState.OPEN

    async def test_half_open_allows_probe(self):
        """is_open() should return False in HALF_OPEN, allowing the probe call."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        assert await breaker.is_open() is False


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — all succeed
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentSuccess:
    """Multiple coroutines succeed simultaneously in HALF_OPEN — breaker must close."""

    async def test_all_succeed_closes(self):
        """When all concurrent probes succeed, breaker ends CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        num_probes = 10

        async def probe_success() -> bool:
            return await breaker.is_open()

        results = await asyncio.gather(
            *(probe_success() for _ in range(num_probes))
        )
        # Now record successes concurrently
        await asyncio.gather(
            *(breaker.record_success() for _ in range(num_probes))
        )

        # All probes were allowed through
        assert all(r is False for r in results)
        # Breaker should be CLOSED — last writer wins but any success closes
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_state_remains_valid_under_concurrent_success(self):
        """State must be a valid CircuitState enum after concurrent successes."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        num_probes = 20

        async def probe() -> None:
            await breaker.is_open()
            await breaker.record_success()

        await asyncio.gather(*(probe() for _ in range(num_probes)))

        assert breaker.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN, CircuitState.OPEN)
        assert breaker.state == CircuitState.CLOSED


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — all fail
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentFailure:
    """Multiple coroutines fail simultaneously in HALF_OPEN — breaker must re-open."""

    async def test_all_fail_reopens(self):
        """When all concurrent probes fail, breaker ends OPEN.

        Note: after the first record_failure() transitions HALF_OPEN → OPEN,
        subsequent is_open() calls may return True (breaker is open, cooldown
        not elapsed).  This is correct — the first failure immediately re-opens.
        """
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        async def probe_failure() -> None:
            await breaker.is_open()
            await breaker.record_failure()

        await asyncio.gather(*(probe_failure() for _ in range(10)))

        assert breaker.state == CircuitState.OPEN


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent probes — mixed success and failure
# ─────────────────────────────────────────────────────────────────────────────


class TestHalfOpenConcurrentMixed:
    """Mixed success/failure probes — state must remain a valid enum and be consistent."""

    async def test_mixed_probes_state_remains_valid(self):
        """Under mixed success/failure, state is always a valid CircuitState."""
        for _ in range(50):
            breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
            await _force_to_open(breaker)
            await _force_to_half_open(breaker)

            async def probe_success() -> None:
                await breaker.is_open()
                await breaker.record_success()

            async def probe_failure() -> None:
                await breaker.is_open()
                await breaker.record_failure()

            await asyncio.gather(
                probe_success(),
                probe_failure(),
                probe_success(),
                probe_failure(),
                probe_success(),
                probe_failure(),
                probe_success(),
                probe_failure(),
            )

            assert breaker.state in (CircuitState.CLOSED, CircuitState.OPEN)

    async def test_failure_count_never_negative(self):
        """failure_count must never go negative under concurrent operations."""
        for _ in range(20):
            breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
            await _force_to_open(breaker)
            await _force_to_half_open(breaker)

            async def succeed() -> None:
                await breaker.is_open()
                await breaker.record_success()

            async def fail() -> None:
                await breaker.is_open()
                await breaker.record_failure()

            await asyncio.gather(
                succeed(),
                succeed(),
                succeed(),
                fail(),
                fail(),
                fail(),
            )

            assert breaker.failure_count >= 0

    async def test_concurrent_records_during_half_open_no_exceptions(self):
        """Rapid concurrent record_success/record_failure must not raise exceptions."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)

        num_probes = 30
        errors: list[Exception] = []

        async def probe(idx: int) -> None:
            try:
                await breaker.is_open()
                if idx % 2 == 0:
                    await breaker.record_success()
                else:
                    await breaker.record_failure()
            except Exception as exc:
                errors.append(exc)

        await asyncio.gather(*(probe(i) for i in range(num_probes)))

        assert errors == []
        assert breaker.state in (CircuitState.CLOSED, CircuitState.OPEN)


# ─────────────────────────────────────────────────────────────────────────────
# State property readability during concurrent writes
# ─────────────────────────────────────────────────────────────────────────────


class TestCircuitBreakerStateProperty:
    """Verify the state property is readable without corruption during writes."""

    async def test_state_always_valid_enum_during_concurrent_writes(self):
        """Rapid concurrent writes should never produce an invalid state value."""
        breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=10)

        num_writers = 10
        num_readers = 10
        observed_states: list[CircuitState] = []

        async def writer(idx: int) -> None:
            for _ in range(100):
                if idx % 2 == 0:
                    await breaker.record_success()
                else:
                    await breaker.record_failure()

        async def reader() -> None:
            for _ in range(100):
                state = breaker.state
                observed_states.append(state)

        await asyncio.gather(
            *(writer(i) for i in range(num_writers)),
            *(reader() for _ in range(num_readers)),
        )

        # Every observed state must be a valid CircuitState enum member
        valid_states = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}
        for state in observed_states:
            assert state in valid_states


# ─────────────────────────────────────────────────────────────────────────────
# force_close() — health-probe-driven recovery
# ─────────────────────────────────────────────────────────────────────────────


class TestForceClose:
    """Verify force_close() transitions from OPEN and HALF_OPEN to CLOSED."""

    async def test_force_close_from_open(self):
        """force_close() should transition OPEN → CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        await _force_to_open(breaker)
        assert breaker.state == CircuitState.OPEN

        await breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_force_close_from_half_open(self):
        """force_close() should also transition HALF_OPEN → CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        await _force_to_open(breaker)
        await _force_to_half_open(breaker)
        assert breaker.state == CircuitState.HALF_OPEN

        await breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_force_close_from_closed_is_noop(self):
        """force_close() on an already CLOSED breaker should be a no-op."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
        assert breaker.state == CircuitState.CLOSED

        await breaker.force_close()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
