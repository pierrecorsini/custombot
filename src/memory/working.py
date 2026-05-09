"""
working.py — Bounded working memory with priority scoring.

Maintains a token-bounded priority queue of memory items, rebuilt each
turn from episodic + per-chat memory. Integrates with ContextAssembler
to stay within token budgets.

Priority = recency + relevance + importance.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.core.context_builder import estimate_tokens

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2000


@dataclass(slots=True)
class WorkingMemoryItem:
    """A single item in working memory."""

    content: str
    priority: float
    source: str  # "episodic" | "memory" | "shared"
    importance: float = 0.5
    timestamp: float = 0.0
    token_count: int = 0

    def __post_init__(self) -> None:
        if self.token_count == 0:
            self.token_count = estimate_tokens(self.content)


class WorkingMemory:
    """Token-bounded priority queue for active context items."""

    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        self._max_tokens = max_tokens
        self._items: list[WorkingMemoryItem] = []
        self._current_tokens = 0

    @property
    def current_tokens(self) -> int:
        return self._current_tokens

    @property
    def item_count(self) -> int:
        return len(self._items)

    # ── mutations ────────────────────────────────────────────────────────

    def add(self, item: WorkingMemoryItem) -> None:
        """Add an item, evicting lowest-priority if budget exceeded."""
        self._items.append(item)
        self._current_tokens += item.token_count
        if self._current_tokens > self._max_tokens:
            self.evict_low_priority()

    def clear(self) -> None:
        """Remove all items."""
        self._items.clear()
        self._current_tokens = 0

    # ── retrieval ────────────────────────────────────────────────────────

    def get_context(self, token_budget: int | None = None) -> list[WorkingMemoryItem]:
        """Return items sorted by priority within the token budget."""
        budget = token_budget or self._max_tokens
        sorted_items = sorted(self._items, key=lambda i: i.priority, reverse=True)
        result: list[WorkingMemoryItem] = []
        used = 0
        for item in sorted_items:
            if used + item.token_count > budget:
                continue
            result.append(item)
            used += item.token_count
        return result

    # ── eviction ─────────────────────────────────────────────────────────

    def evict_low_priority(self) -> int:
        """Remove lowest-priority items until within token budget.

        Returns the number of items evicted.
        """
        evicted = 0
        while self._current_tokens > self._max_tokens and self._items:
            lowest = min(self._items, key=lambda i: i.priority)
            self._items.remove(lowest)
            self._current_tokens -= lowest.token_count
            evicted += 1
        return evicted

    # ── scoring ──────────────────────────────────────────────────────────

    @staticmethod
    def relevance_score(item: WorkingMemoryItem, current_query: str) -> float:
        """Score relevance via keyword overlap between item and query."""
        if not current_query:
            return 0.0
        query_words = set(current_query.lower().split())
        content_words = set(item.content.lower().split())
        if not query_words:
            return 0.0
        overlap = len(query_words & content_words)
        return overlap / len(query_words)

    def compute_priority(
        self,
        item: WorkingMemoryItem,
        current_query: str = "",
    ) -> float:
        """Combine recency, relevance, and importance into a priority score."""
        now = time.time()
        age_hours = (now - item.timestamp) / 3600 if item.timestamp else 0.0
        recency = max(0.0, 1.0 - age_hours / 168.0)  # 1 week decay
        relevance = self.relevance_score(item, current_query)
        return recency * 0.4 + relevance * 0.3 + item.importance * 0.3

    # ── rebuild ──────────────────────────────────────────────────────────

    def rebuild(
        self,
        items: list[WorkingMemoryItem],
        current_query: str = "",
    ) -> None:
        """Rebuild working memory from a fresh set of items."""
        self.clear()
        for item in items:
            item.priority = self.compute_priority(item, current_query)
        sorted_items = sorted(items, key=lambda i: i.priority, reverse=True)
        for item in sorted_items:
            if self._current_tokens + item.token_count <= self._max_tokens:
                self._items.append(item)
                self._current_tokens += item.token_count
