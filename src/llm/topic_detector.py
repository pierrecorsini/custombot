"""
src/llm/topic_detector.py — Conversation topic detection and segmentation.

Detects topic shifts in conversation history so that natural boundaries
can be created for memory indexing.  Uses keyword-overlap analysis for
low-latency detection, with an optional LLM-based fallback.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.context_builder import ChatMessage
    from src.llm import LLMProvider

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-zA-Z\u4e00-\u9fff]{3,}")
_TOP_K_KEYWORDS = 15
_SHIFT_THRESHOLD = 0.25  # Jaccard dissimilarity threshold


@dataclass(slots=True, frozen=True)
class TopicSegment:
    """Result of a topic-shift detection."""

    shifted: bool
    current_topic: str
    previous_topic: str | None = None
    confidence: float = 0.0


class TopicDetector:
    """Detects topic changes between conversation segments."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._enabled = enabled
        self._topics: dict[str, str] = {}
        self._stats = {"checks": 0, "shifts": 0}

    async def detect_topic_shift(
        self,
        chat_id: str,
        recent_messages: list[ChatMessage],
    ) -> TopicSegment:
        """Compare recent messages against the stored topic for *chat_id*.

        Returns a :class:`TopicSegment` indicating whether the topic changed.
        """
        if not self._enabled or len(recent_messages) < 2:
            return TopicSegment(shifted=False, current_topic="general")

        self._stats["checks"] += 1

        keywords = self._extract_keywords(recent_messages)
        current_topic = self._label_topic(keywords)
        previous_topic = self._topics.get(chat_id)

        if previous_topic is None:
            self._topics[chat_id] = current_topic
            return TopicSegment(shifted=False, current_topic=current_topic)

        similarity = self._jaccard(
            set(keywords),
            self._keywords_for_label(previous_topic),
        )
        shifted = similarity < (1.0 - _SHIFT_THRESHOLD)

        if shifted:
            self._stats["shifts"] += 1
            self._topics[chat_id] = current_topic
            log.info(
                "Topic shift detected in chat %s: %r → %r (similarity=%.2f)",
                chat_id,
                previous_topic,
                current_topic,
                similarity,
            )
            return TopicSegment(
                shifted=True,
                current_topic=current_topic,
                previous_topic=previous_topic,
                confidence=1.0 - similarity,
            )

        return TopicSegment(shifted=False, current_topic=current_topic)

    @staticmethod
    def _extract_keywords(messages: list[ChatMessage]) -> list[str]:
        """Return the top-K keywords from recent user messages."""
        text = " ".join(m.content for m in messages if m.role == "user")
        words = _WORD_RE.findall(text.lower())
        counter = Counter(words)
        return [w for w, _ in counter.most_common(_TOP_K_KEYWORDS)]

    @staticmethod
    def _label_topic(keywords: list[str]) -> str:
        """Derive a short topic label from top keywords."""
        if not keywords:
            return "general"
        return "_".join(keywords[:3])

    @staticmethod
    def _keywords_for_label(label: str) -> set[str]:
        """Reconstruct a keyword set from a stored label."""
        return set(label.split("_"))

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard similarity between two sets."""
        if not a and not b:
            return 1.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union else 0.0

    def get_current_topic(self, chat_id: str) -> str | None:
        return self._topics.get(chat_id)

    def get_metrics(self) -> dict[str, Any]:
        return dict(self._stats)
