"""
core/degradation.py — Graceful feature degradation protocol.

Defines degradation levels and automatic feature disabling based on
system health metrics.  Components call ``is_feature_enabled()`` before
using expensive features (vector search, streaming, etc.).

Levels:
  FULL      — All features enabled (normal operation).
  REDUCED   — No vector search, no streaming (memory pressure).
  MINIMAL   — Basic text responses only (LLM errors high).
  EMERGENCY — Reject new messages, process queue only (critical).
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────

# Cooldown between automatic level adjustments (seconds).
ADJUSTMENT_COOLDOWN: float = 30.0

# Health thresholds for automatic degradation.
MEMORY_REDUCED_THRESHOLD: float = 90.0  # percent
LLM_ERRORS_MINIMAL_THRESHOLD: float = 10  # errors per minute
QUEUE_EMERGENCY_MULTIPLIER: float = 2.0  # vs max_concurrent_messages

# Queue-depth thresholds for load-based degradation.
DEGRADATION_QUEUE_WARNING: int = 50
DEGRADATION_QUEUE_CRITICAL: int = 100

# Features grouped by the minimum level at which they're available.
_FEATURE_LEVELS: dict[str, "DegradationLevel"] = {
    "vector_search": "REDUCED",
    "streaming": "REDUCED",
    "complex_tools": "REDUCED",
    "tool_execution": "MINIMAL",
    "scheduled_tasks": "MINIMAL",
    "outbound_dedup": "MINIMAL",
    "basic_responses": "EMERGENCY",
}


class DegradationLevel(str, enum.Enum):
    """Degradation severity levels."""

    FULL = "full"
    REDUCED = "reduced"
    MINIMAL = "minimal"
    EMERGENCY = "emergency"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    def __lt__(self, other: "DegradationLevel") -> bool:
        return self.severity < other.severity

    def __le__(self, other: "DegradationLevel") -> bool:
        return self.severity <= other.severity


_SEVERITY: dict[DegradationLevel, int] = {
    DegradationLevel.FULL: 0,
    DegradationLevel.REDUCED: 1,
    DegradationLevel.MINIMAL: 2,
    DegradationLevel.EMERGENCY: 3,
}

_LEVEL_FROM_NAME: dict[str, DegradationLevel] = {
    v.value: v for v in DegradationLevel
}


@dataclass(slots=True)
class HealthSnapshot:
    """Current health metrics used for degradation decisions."""

    memory_percent: float = 0.0
    llm_errors_per_minute: float = 0.0
    queue_depth: int = 0
    max_concurrent: int = 100
    circuit_breaker_open: bool = False


class DegradationManager:
    """Automatic feature degradation based on system health.

    Components call ``is_feature_enabled(feature_name)`` before using
    expensive features.  The manager auto-adjusts the degradation level
    based on health metrics with a cooldown to prevent oscillation.
    """

    def __init__(self) -> None:
        self._level: DegradationLevel = DegradationLevel.FULL
        self._last_adjustment: float = 0.0
        self._overrides: dict[str, bool] = {}

    @property
    def current_level(self) -> DegradationLevel:
        return self._level

    def is_feature_enabled(self, feature_name: str) -> bool:
        """Check if a feature is available at the current degradation level.

        Manual overrides (set via ``set_override``) take precedence over
        automatic level-based decisions.
        """
        if feature_name in self._overrides:
            return self._overrides[feature_name]

        min_level_name = _FEATURE_LEVELS.get(feature_name)
        if min_level_name is None:
            return True  # Unknown features are always enabled.

        min_level = _LEVEL_FROM_NAME[min_level_name]
        return self._level <= min_level

    def set_override(self, feature_name: str, enabled: bool) -> None:
        """Manually override a feature's enabled state."""
        self._overrides[feature_name] = enabled

    def adjust(self, health: HealthSnapshot) -> bool:
        """Re-evaluate degradation level based on health metrics.

        Returns True if the level changed.
        """
        now = time.time()
        if now - self._last_adjustment < ADJUSTMENT_COOLDOWN:
            return False

        target = self._compute_target_level(health)
        if target == self._level:
            return False

        old = self._level
        self._level = target
        self._last_adjustment = now
        log.warning(
            "Degradation level changed: %s → %s (memory=%.1f%%, "
            "llm_errors/min=%.1f, queue=%d, breaker_open=%s)",
            old.value,
            target.value,
            health.memory_percent,
            health.llm_errors_per_minute,
            health.queue_depth,
            health.circuit_breaker_open,
        )
        return True

    def update_queue_depth(self, queue_depth: int, max_concurrent: int = 100) -> bool:
        """Convenience: adjust degradation from queue depth alone.

        Returns True if the level changed.
        """
        return self.adjust(
            HealthSnapshot(
                queue_depth=queue_depth,
                max_concurrent=max_concurrent,
            )
        )

    def _compute_target_level(self, health: HealthSnapshot) -> DegradationLevel:
        if health.circuit_breaker_open and health.llm_errors_per_minute > LLM_ERRORS_MINIMAL_THRESHOLD:
            return DegradationLevel.EMERGENCY

        # Queue-depth thresholds take priority — high load disables expensive
        # features before other metrics push the level higher.
        if health.queue_depth >= DEGRADATION_QUEUE_CRITICAL:
            return DegradationLevel.EMERGENCY

        queue_limit = int(health.max_concurrent * QUEUE_EMERGENCY_MULTIPLIER)
        if health.queue_depth > queue_limit:
            return DegradationLevel.EMERGENCY

        if health.queue_depth >= DEGRADATION_QUEUE_WARNING:
            return DegradationLevel.REDUCED

        if health.llm_errors_per_minute > LLM_ERRORS_MINIMAL_THRESHOLD:
            return DegradationLevel.MINIMAL

        if health.memory_percent > MEMORY_REDUCED_THRESHOLD:
            return DegradationLevel.REDUCED

        return DegradationLevel.FULL

    def reset(self) -> None:
        """Reset to FULL level (e.g., for testing)."""
        self._level = DegradationLevel.FULL
        self._last_adjustment = 0.0
        self._overrides.clear()
