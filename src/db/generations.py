"""
generations.py — Per-chat generation counter for write-conflict detection.

Extracted from db.py to isolate generation tracking concerns and make
the counter independently testable.

Each chat gets a monotonically increasing generation counter that is
bumped after every successful write.  Callers can check whether a
stale generation matches before applying optimistic updates.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from src.constants import MAX_CHAT_GENERATIONS
from src.db.db_utils import _db_log_extra

log = logging.getLogger(__name__)

__all__ = ["GenerationCounter"]


class GenerationCounter:
    """Bounded LRU map of chat_id → generation counter.

    Automatically evicts the oldest 25 % of entries when the cap
    (``MAX_CHAT_GENERATIONS``) is exceeded, keeping the most-recently
    bumped chats resident.
    """

    def __init__(self, max_generations: int = MAX_CHAT_GENERATIONS) -> None:
        self._generations: OrderedDict[str, int] = OrderedDict()
        self._max = max_generations

    def get(self, chat_id: str) -> int:
        """Return the current generation counter for *chat_id*."""
        return self._generations.get(chat_id, 0)

    def check(self, chat_id: str, expected: int) -> bool:
        """Return ``True`` if *chat_id*'s generation still matches *expected*."""
        return self._generations.get(chat_id, 0) == expected

    def bump(self, chat_id: str) -> None:
        """Increment the generation counter after a successful write."""
        self._generations[chat_id] = self._generations.get(chat_id, 0) + 1
        self._generations.move_to_end(chat_id)

        if len(self._generations) > self._max:
            discard_count = self._max // 4
            for _ in range(discard_count):
                self._generations.popitem(last=False)
            log.debug(
                "Trimmed %d entries from generation counter (cap=%d)",
                discard_count,
                self._max,
                extra=_db_log_extra(),
            )
