"""
src/core/skill_breaker_registry.py — Bounded registry of per-skill circuit breakers.

Wraps an OrderedDict of skill-name → CircuitBreaker with LRU eviction so that
a long-running bot that encounters many unique skill names (e.g. adversarial
tool calls) doesn't leak memory.  Mirrors the LRU eviction pattern already
used by RateLimiter._skill_limiters.

Lock model: Uses ThreadLock (from src.utils.locking) for dict operations
because breaker access may happen from both sync and async contexts.
CircuitBreaker itself uses AsyncLock internally for state transitions.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from src.constants import (
    MAX_TRACKED_SKILL_BREAKERS,
    SKILL_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
)
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.locking import ThreadLock

log = logging.getLogger(__name__)


class SkillBreakerRegistry:
    """Registry of per-skill circuit breakers with bounded LRU eviction.

    Promotes accessed breakers to most-recently-used and evicts the
    least-recently-used when *max_skills* is exceeded, preventing unbounded
    memory growth from adversarial inputs that generate many unique skill names.

    Example::

        registry = SkillBreakerRegistry(
            failure_threshold=3,
            cooldown_seconds=60.0,
        )
        breaker = registry.get_or_create("web_search")
        if await breaker.is_open():
            return "service unavailable"
    """

    def __init__(
        self,
        failure_threshold: int = SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        cooldown_seconds: float = SKILL_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        max_skills: int = MAX_TRACKED_SKILL_BREAKERS,
    ) -> None:
        self._breakers: OrderedDict[str, CircuitBreaker] = OrderedDict()
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._max_skills = max_skills
        self._lock = ThreadLock()

    def get_or_create(self, skill_name: str) -> CircuitBreaker:
        """Return (or lazily create) a CircuitBreaker for *skill_name*.

        Accessing an existing breaker promotes it to most-recently-used.
        New entries evict the least-recently-used if the cap is exceeded.
        """
        with self._lock:
            if skill_name in self._breakers:
                self._breakers.move_to_end(skill_name)
                return self._breakers[skill_name]

            if len(self._breakers) >= self._max_skills:
                evicted_key, _ = self._breakers.popitem(last=False)
                log.debug(
                    "Evicted skill circuit breaker for %r (LRU cap=%d)",
                    evicted_key,
                    self._max_skills,
                )

            breaker = CircuitBreaker(
                failure_threshold=self._failure_threshold,
                cooldown_seconds=self._cooldown_seconds,
            )
            self._breakers[skill_name] = breaker
            return breaker

    def get_breaker_states(self) -> dict[str, str]:
        """Return a snapshot of ``{skill_name: state}`` for diagnostics.

        Used by the health endpoint to expose which skills are degraded
        without requiring operators to inspect logs.
        """
        with self._lock:
            return {
                name: breaker.state.value for name, breaker in self._breakers.items()
            }

    @property
    def size(self) -> int:
        """Current number of tracked skill breakers."""
        return len(self._breakers)

    @property
    def max_skills(self) -> int:
        """Configured upper bound on tracked skill breakers."""
        return self._max_skills

    def clear(self) -> None:
        """Remove all tracked breakers (useful for testing)."""
        with self._lock:
            self._breakers.clear()


__all__ = ["SkillBreakerRegistry"]
