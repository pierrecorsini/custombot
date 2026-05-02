"""
src/core/dedup.py — Unified deduplication service.

Consolidates inbound (message-id) and outbound (content-hash) dedup
strategies behind a single service with unified stats tracking.

- Inbound: checks message_id against the database's persistent index.
- Outbound: xxHash (xxh64) content hash with TTL-based LRU cache.

Usage::

    dedup = DeduplicationService(db=database)
    # Inbound check (called from Bot)
    if await dedup.is_inbound_duplicate("msg_123"):
        return
    # Outbound check (called from Scheduler)
    if dedup.is_outbound_duplicate("chat_1", "response text"):
        return
    dedup.record_outbound("chat_1", "response text")
"""

from __future__ import annotations

import xxhash
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from src.constants import OUTBOUND_DEDUP_MAX_SIZE, OUTBOUND_DEDUP_TTL_SECONDS
from src.exceptions import DatabaseError

if TYPE_CHECKING:
    from src.utils.protocols import Storage

log = logging.getLogger(__name__)


def outbound_key(chat_id: str, text: str) -> str:
    """Content-addressable key via xxHash (xxh64).

    Deterministic hash combining *chat_id* and *text* so identical
    outbound messages to the same chat produce the same key.
    xxHash is ~10× faster than SHA-256 and sufficient for a TTL-bounded
    LRU cache key (not a cryptographic use case).
    """
    return xxhash.xxh64(f"{chat_id}\x00{text}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class DedupStats:
    """Snapshot of dedup hit/miss counters for both strategies."""

    inbound_hits: int = 0
    inbound_misses: int = 0
    outbound_hits: int = 0
    outbound_misses: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "inbound_hits": self.inbound_hits,
            "inbound_misses": self.inbound_misses,
            "outbound_hits": self.outbound_hits,
            "outbound_misses": self.outbound_misses,
        }


class DeduplicationService:
    """Unified deduplication for inbound and outbound messages.

    Thread-safety: all methods are either ``async`` (inbound check delegates
    to the async DB) or synchronous single-threaded (outbound cache).  Within
    the asyncio event loop, sequential execution between ``await`` points is
    guaranteed, so no additional locking is needed for the outbound cache.
    """

    __slots__ = ("_db", "_outbound_cache", "_outbound_ttl", "_stats")

    def __init__(
        self,
        db: Storage,
        outbound_max_size: int = OUTBOUND_DEDUP_MAX_SIZE,
        outbound_ttl: float = OUTBOUND_DEDUP_TTL_SECONDS,
    ) -> None:
        from src.utils import LRUDict

        self._db = db
        self._outbound_cache: LRUDict = LRUDict(max_size=outbound_max_size)
        self._outbound_ttl = outbound_ttl
        self._stats = DedupStats()

    # ── Inbound dedup (message-id based, persistent) ────────────────────────

    async def is_inbound_duplicate(self, message_id: str) -> bool:
        """Check if *message_id* was already processed.

        Delegates to the database's in-memory message-ID index (rebuilt from
        JSONL on startup).  Tracks hits/misses for metrics.

        On database failure, logs a warning and returns ``False`` so the
        message is allowed through — graceful degradation during transient
        DB outages.
        """
        try:
            exists = await self._db.message_exists(message_id)
        except DatabaseError:
            log.warning(
                "Dedup DB lookup failed for %r — allowing message through",
                message_id,
                exc_info=True,
            )
            return False
        if exists:
            self._stats.inbound_hits += 1
        else:
            self._stats.inbound_misses += 1
        return exists

    # ── Outbound dedup (content-hash based, TTL LRU cache) ──────────────────

    def check_outbound_duplicate(self, chat_id: str, text: str) -> bool:
        """Return ``True`` if *text* was recently sent to *chat_id*.

        Read-only check — does NOT record the timestamp.  Use
        :meth:`record_outbound` after the send succeeds to populate
        the cache.  This two-phase API prevents false positives when
        the send fails after the check passes.
        """
        key = outbound_key(chat_id, text)
        now = time.monotonic()
        sent_at = self._outbound_cache.get(key)
        if sent_at is not None and (now - sent_at) < self._outbound_ttl:
            self._stats.outbound_hits += 1
            return True
        self._stats.outbound_misses += 1
        return False

    def is_outbound_duplicate(self, chat_id: str, text: str) -> bool:
        """Return ``True`` if *text* was recently sent to *chat_id*.

        Records the current timestamp on miss so subsequent calls detect it
        within the TTL window.

        .. deprecated::
            Prefer :meth:`check_outbound_duplicate` + :meth:`record_outbound`
            for correct two-phase usage (check before send, record after
            successful send).  This method records on miss, which causes
            false positives if the send subsequently fails.
        """
        key = outbound_key(chat_id, text)
        now = time.monotonic()
        sent_at = self._outbound_cache.get(key)
        if sent_at is not None and (now - sent_at) < self._outbound_ttl:
            self._stats.outbound_hits += 1
            return True
        self._outbound_cache[key] = now
        self._stats.outbound_misses += 1
        return False

    def record_outbound(self, chat_id: str, text: str) -> None:
        """Explicitly record that *text* was sent to *chat_id*.

        Call this **after** the send succeeds to populate the dedup cache.
        Safe to call multiple times — overwrites the previous timestamp.
        """
        key = outbound_key(chat_id, text)
        self._outbound_cache[key] = time.monotonic()

    # ── Stats ───────────────────────────────────────────────────────────────

    @property
    def stats(self) -> DedupStats:
        """Return a lightweight copy of dedup counters.

        Uses ``dataclasses.replace`` which copies only the four integer
        fields — cheaper than the previous manual construction and
        avoids sharing a mutable reference with callers.
        """
        return replace(self._stats)
