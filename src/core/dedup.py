"""
src/core/dedup.py — Unified deduplication service.

Consolidates inbound (message-id) and outbound (content-hash) dedup
strategies behind a single service with unified stats tracking.

- Inbound: checks message_id against the database's persistent index,
  with an in-memory LRU cache to short-circuit repeated async DB lookups.
- Outbound: xxHash (xxh64) content hash with TTL-based LRU cache.

Usage::

    dedup = DeduplicationService(db=database)
    # Inbound check (called from Bot)
    if await dedup.is_inbound_duplicate("msg_123"):
        return
    # Outbound check + record (called from Scheduler — single hash computation)
    if dedup.check_and_record_outbound("chat_1", "response text"):
        return
    # Or two-phase for when recording must happen after a successful send:
    if dedup.check_outbound_duplicate("chat_1", "response text"):
        return
    dedup.record_outbound("chat_1", "response text")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import xxhash

from src.constants import (
    INBOUND_DEDUP_CACHE_MAX_SIZE,
    INBOUND_DEDUP_CACHE_TTL_SECONDS,
    OUTBOUND_DEDUP_BUFFER_MAX_SIZE,
    OUTBOUND_DEDUP_MAX_SIZE,
    OUTBOUND_DEDUP_TTL_SECONDS,
)
from src.exceptions import DatabaseError
from src.utils import BoundedOrderedDict

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
    return xxhash.xxh64(f"{chat_id}\x00{text}".encode()).hexdigest()


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

    __slots__ = (
        "_db",
        "_inbound_cache",
        "_outbound_buffer",
        "_outbound_buffer_max",
        "_outbound_cache",
        "_stats",
    )

    def __init__(
        self,
        db: Storage,
        outbound_max_size: int = OUTBOUND_DEDUP_MAX_SIZE,
        outbound_ttl: float = OUTBOUND_DEDUP_TTL_SECONDS,
        inbound_cache_max_size: int = INBOUND_DEDUP_CACHE_MAX_SIZE,
        inbound_cache_ttl: float = INBOUND_DEDUP_CACHE_TTL_SECONDS,
        outbound_buffer_max: int = OUTBOUND_DEDUP_BUFFER_MAX_SIZE,
    ) -> None:
        self._db = db
        self._inbound_cache: BoundedOrderedDict[str, bool] = BoundedOrderedDict(
            max_size=inbound_cache_max_size,
            ttl=inbound_cache_ttl,
        )
        self._outbound_buffer: list[tuple[str, str]] = []
        self._outbound_buffer_max = outbound_buffer_max
        self._outbound_cache: BoundedOrderedDict[str, bool] = BoundedOrderedDict(
            max_size=outbound_max_size,
            ttl=outbound_ttl,
        )
        self._stats = DedupStats()

    # ── Inbound dedup (message-id based, persistent) ────────────────────────

    async def is_inbound_duplicate(self, message_id: str) -> bool:
        """Check if *message_id* was already processed.

        An in-memory LRU cache is checked first to short-circuit the
        async DB call.  Cache entries store the boolean result from the
        DB query and expire after the configured TTL.  True duplicates
        arrive within seconds, and unique IDs never need re-checking
        after the first miss ages out.

        On database failure, logs a warning and returns ``False`` so the
        message is allowed through — graceful degradation during transient
        DB outages.  The failure is NOT cached to avoid stale negative
        entries masking a recoverable DB.
        """
        # Fast path: check in-memory LRU cache before the async DB call.
        cached = self._inbound_cache.get(message_id)
        if cached is not None:
            if cached:
                self._stats.inbound_hits += 1
            else:
                self._stats.inbound_misses += 1
            return cached

        try:
            exists = await self._db.message_exists(message_id)
        except DatabaseError:
            log.warning(
                "Dedup DB lookup failed for %r — allowing message through",
                message_id,
                exc_info=True,
            )
            return False

        # Persist the DB result in the LRU cache.
        self._inbound_cache[message_id] = exists

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

        Flushes any buffered outbound recordings before checking so
        that recently-recorded entries are visible.

        TTL expiry is handled lazily by ``BoundedOrderedDict``.
        """
        self.flush_outbound_batch()
        key = outbound_key(chat_id, text)
        if self._outbound_cache.get(key) is not None:
            self._stats.outbound_hits += 1
            return True
        self._stats.outbound_misses += 1
        return False

    def is_outbound_duplicate(self, chat_id: str, text: str) -> bool:
        """Return ``True`` if *text* was recently sent to *chat_id*.

        Records the entry on miss so subsequent calls detect it
        within the TTL window.

        .. deprecated::
            Prefer :meth:`check_outbound_duplicate` + :meth:`record_outbound`
            for correct two-phase usage (check before send, record after
            successful send).  This method records on miss, which causes
            false positives if the send subsequently fails.
        """
        self.flush_outbound_batch()
        key = outbound_key(chat_id, text)
        if self._outbound_cache.get(key) is not None:
            self._stats.outbound_hits += 1
            return True
        self._outbound_cache[key] = True
        self._stats.outbound_misses += 1
        return False

    def record_outbound(self, chat_id: str, text: str) -> None:
        """Buffer an outbound recording for batch insertion.

        Call this **after** the send succeeds.  Entries are accumulated in
        an internal buffer and flushed to the dedup cache before the next
        outbound check or on explicit :meth:`flush_outbound_batch`.  During
        burst delivery this defers hash computation and cache eviction,
        reducing per-message overhead.

        If the buffer reaches ``_outbound_buffer_max``, it is flushed
        immediately to prevent unbounded memory growth during sustained
        bursts.
        """
        if len(self._outbound_buffer) >= self._outbound_buffer_max:
            log.warning(
                "Outbound dedup buffer reached cap (%d) — flushing early",
                self._outbound_buffer_max,
            )
            self.flush_outbound_batch()
        self._outbound_buffer.append((chat_id, text))

    def flush_outbound_batch(self) -> None:
        """Flush buffered outbound recordings to the dedup cache.

        Computes content hashes for all buffered ``(chat_id, text)`` pairs
        and inserts them into the ``BoundedOrderedDict`` in a single batch,
        amortising eviction overhead.  Called automatically before outbound
        checks; can also be called explicitly after a burst of sends.
        """
        if not self._outbound_buffer:
            return
        buffer = self._outbound_buffer
        self._outbound_buffer = []
        now_entries = [(outbound_key(cid, txt), True) for cid, txt in buffer]
        self._outbound_cache.batch_set(now_entries)

    def check_and_record_outbound(self, chat_id: str, text: str) -> bool:
        """Combined check + record: returns ``True`` if duplicate.

        Computes the content hash **once** and performs both the duplicate
        check and (on miss) the recording in a single pass.  Use this
        instead of calling :meth:`check_outbound_duplicate` followed by
        :meth:`record_outbound` to avoid redundant xxh64 computation.

        Flushes any buffered outbound recordings before checking so
        that recently-recorded entries are visible.
        """
        self.flush_outbound_batch()
        key = outbound_key(chat_id, text)
        if self._outbound_cache.get(key) is not None:
            self._stats.outbound_hits += 1
            return True
        self._outbound_cache[key] = True
        self._stats.outbound_misses += 1
        return False

    # ── Stats ───────────────────────────────────────────────────────────────

    @property
    def stats(self) -> DedupStats:
        """Return a lightweight copy of dedup counters.

        Uses ``dataclasses.replace`` which copies only the four integer
        fields — cheaper than the previous manual construction and
        avoids sharing a mutable reference with callers.
        """
        return replace(self._stats)
