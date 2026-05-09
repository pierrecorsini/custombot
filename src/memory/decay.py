"""
decay.py — Time-based memory decay with importance scoring.

Each memory carries an importance score (0.0–1.0). Effective weight
decays exponentially with age. Low-importance memories decay faster.

Formula: effective_weight = importance * exp(-lambda * age_hours)
Default lambda: 0.01 (half-life ~69 hours).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

DEFAULT_DECAY_LAMBDA = 0.01
LOW_IMPORTANCE_THRESHOLD = 0.3
LOW_IMPORTANCE_MULTIPLIER = 3.0
DEFAULT_PRUNE_THRESHOLD = 0.1


@dataclass(slots=True, frozen=True)
class DecayConfig:
    """Configurable parameters for memory decay."""

    decay_lambda: float = DEFAULT_DECAY_LAMBDA
    low_importance_threshold: float = LOW_IMPORTANCE_THRESHOLD
    low_importance_multiplier: float = LOW_IMPORTANCE_MULTIPLIER
    prune_threshold: float = DEFAULT_PRUNE_THRESHOLD


class MemoryDecayManager:
    """Computes decayed importance weights and prunes stale memories."""

    def __init__(self, config: DecayConfig | None = None) -> None:
        self._config = config or DecayConfig()

    def get_decay_config(self) -> dict[str, Any]:
        """Return current decay configuration."""
        return {
            "decay_lambda": self._config.decay_lambda,
            "low_importance_threshold": self._config.low_importance_threshold,
            "low_importance_multiplier": self._config.low_importance_multiplier,
            "prune_threshold": self._config.prune_threshold,
        }

    def calculate_effective_weight(self, memory: dict[str, Any]) -> float:
        """Compute the decayed effective weight for a memory.

        Expected keys in *memory*:
            - ``importance`` (float, 0.0–1.0)
            - ``age_hours`` (float, hours since creation)
        """
        importance = float(memory.get("importance", 0.5))
        age_hours = float(memory.get("age_hours", 0.0))

        decay_lambda = self._config.decay_lambda
        if importance < self._config.low_importance_threshold:
            decay_lambda *= self._config.low_importance_multiplier

        return importance * math.exp(-decay_lambda * age_hours)

    def prune_decayed(
        self,
        memories: Sequence[dict[str, Any]],
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return memories whose effective weight is above *threshold*.

        Memories below the threshold are considered decayed and excluded.
        """
        cutoff = threshold if threshold is not None else self._config.prune_threshold
        kept: list[dict[str, Any]] = []
        pruned = 0
        for mem in memories:
            if self.calculate_effective_weight(mem) >= cutoff:
                kept.append(mem)
            else:
                pruned += 1
        if pruned:
            log.debug("Pruned %d memories below threshold %.2f", pruned, cutoff)
        return kept
