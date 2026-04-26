"""
circuit_breaker.py — Circuit breaker for protecting against cascading failures.

Implements the classic three-state circuit breaker pattern:
  CLOSED   → normal operation; failures are counted
  OPEN     → all calls rejected immediately; waits for cooldown
  HALF_OPEN → allows a single probe to test if the provider recovered

Usage:
    from src.utils.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=60)

    if await breaker.is_open():
        raise ServiceUnavailableError(...)

    try:
        result = await call_external_service()
        await breaker.record_success()
    except Exception:
        await breaker.record_failure()
"""

from __future__ import annotations

import enum
import logging
import time

from src.utils.locking import AsyncLock

log = logging.getLogger(__name__)


class CircuitState(str, enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Protects against cascading failures by short-circuiting calls.

    After *failure_threshold* consecutive failures the breaker opens and
    rejects all calls for *cooldown_seconds*.  After the cooldown it
    transitions to HALF_OPEN and allows a single probe call.  A successful
    probe closes the breaker; a failed probe re-opens it.

    Thread-safety: uses ``AsyncLock`` (see src.utils.locking) — safe for
    single-event-loop use and lazy-initialised for pre-loop construction.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._lock = AsyncLock()

    @property
    def state(self) -> CircuitState:
        """Current breaker state (for diagnostics / health endpoints)."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Current consecutive failure count (for diagnostics / metrics)."""
        return self._failure_count

    @property
    def last_failure_time(self) -> float:
        """Monotonic timestamp of the last recorded failure."""
        return self._last_failure_time

    async def is_open(self) -> bool:
        """Return ``True`` when the breaker is OPEN and should reject calls.

        Side-effect: transitions from OPEN → HALF_OPEN once the cooldown
        elapses, so the *next* call is allowed as a probe.
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return False
            if self._state == CircuitState.HALF_OPEN:
                return False
            # OPEN — check cooldown
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                log.info("Circuit breaker transitioned to HALF_OPEN after %.1fs cooldown", elapsed)
                return False
            return True

    async def record_success(self) -> None:
        """Record a successful call.  Closes the breaker from HALF_OPEN."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                log.info("Circuit breaker CLOSED — probe succeeded")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call.  Opens the breaker when threshold is hit."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                log.warning("Circuit breaker re-OPENED — probe failed")
            elif self._failure_count >= self._failure_threshold:
                self._state = CircuitState.OPEN
                log.warning(
                    "Circuit breaker OPENED — %d consecutive failures (cooldown=%.0fs)",
                    self._failure_count,
                    self._cooldown_seconds,
                )

    async def force_close(self) -> None:
        """Force the breaker to CLOSED from OPEN or HALF_OPEN.

        Used by health-check-driven recovery: when an external probe
        confirms the provider has recovered, the breaker can be closed
        immediately without waiting for the full cooldown period to expire.
        """
        async with self._lock:
            if self._state in (CircuitState.OPEN, CircuitState.HALF_OPEN):
                elapsed = time.monotonic() - self._last_failure_time
                prev_state = self._state.value
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                log.info(
                    "Circuit breaker force-CLOSED by health probe "
                    "(was %s for %.1fs, cooldown was %.0fs)",
                    prev_state,
                    elapsed,
                    self._cooldown_seconds,
                )


__all__ = ["CircuitBreaker", "CircuitState"]
