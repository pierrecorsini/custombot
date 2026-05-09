"""
src/llm/reflection.py — Self-reflection and response quality scoring.

After generating a response, runs a lightweight LLM evaluation pass that
scores coherence, relevance, and completeness on a 1–5 scale.  When the
average score falls below a configurable threshold, a warning is logged
and (optionally) the response is regenerated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.llm import LLMProvider

log = logging.getLogger(__name__)

_REFLECTION_PROMPT = (
    "Rate this response on three criteria (1–5 each). "
    "Reply with ONLY a JSON object: "
    '{"coherence": <1-5>, "relevance": <1-5>, "completeness": <1-5>}\n\n'
    "User query: {query}\n\n"
    "Response: {response}"
)

_SCORE_RE = re.compile(r"(\d)")


@dataclass(slots=True, frozen=True)
class ReflectionScores:
    """Quality scores for a single response."""

    coherence: int
    relevance: int
    completeness: int

    @property
    def average(self) -> float:
        return (self.coherence + self.relevance + self.completeness) / 3.0


@dataclass(slots=True)
class _ScoreTracker:
    """Running statistics for reflection scores."""

    count: int = 0
    total_avg: float = 0.0
    below_threshold: int = 0


class ResponseReflector:
    """Evaluates LLM responses via a lightweight self-reflection LLM call."""

    def __init__(
        self,
        llm: LLMProvider,
        threshold: float = 3.0,
        auto_regenerate: bool = False,
    ) -> None:
        self._llm = llm
        self._threshold = threshold
        self._auto_regenerate = auto_regenerate
        self._tracker = _ScoreTracker()

    async def evaluate(
        self,
        query: str,
        response: str,
    ) -> ReflectionScores | None:
        """Score *response* against *query*.  Returns ``None`` on parse failure."""
        prompt = _REFLECTION_PROMPT.format(
            query=query[:500],
            response=response[:2000],
        )
        try:
            completion = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                timeout=15.0,
            )
            text = completion.choices[0].message.content or ""
            return self._parse_scores(text)
        except Exception:
            log.warning("Reflection call failed", exc_info=True)
            return None

    def _parse_scores(self, text: str) -> ReflectionScores | None:
        """Extract the three numeric scores from the LLM reply."""
        import json

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # Fallback: extract first three digits
            digits = _SCORE_RE.findall(text)
            if len(digits) >= 3:
                vals = [min(5, max(1, int(d))) for d in digits[:3]]
                return ReflectionScores(*vals)
            return None

        if not isinstance(data, dict):
            return None

        try:
            return ReflectionScores(
                coherence=min(5, max(1, int(data.get("coherence", 0)))),
                relevance=min(5, max(1, int(data.get("relevance", 0)))),
                completeness=min(5, max(1, int(data.get("completeness", 0)))),
            )
        except (ValueError, TypeError):
            return None

    def record(self, scores: ReflectionScores) -> None:
        """Update running stats and log warnings for low-quality responses."""
        self._tracker.count += 1
        self._tracker.total_avg += scores.average
        if scores.average < self._threshold:
            self._tracker.below_threshold += 1
            log.warning(
                "Low response quality (avg=%.1f): coherence=%d relevance=%d completeness=%d",
                scores.average,
                scores.coherence,
                scores.relevance,
                scores.completeness,
            )

    @property
    def should_regenerate(self) -> bool:
        """Whether auto-regeneration is enabled."""
        return self._auto_regenerate

    def get_metrics(self) -> dict[str, Any]:
        """Return summary statistics for monitoring."""
        avg = self._tracker.total_avg / self._tracker.count if self._tracker.count else 0.0
        return {
            "total_evaluated": self._tracker.count,
            "average_score": round(avg, 2),
            "below_threshold": self._tracker.below_threshold,
            "threshold": self._threshold,
        }
