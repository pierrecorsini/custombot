"""
src/core/dedup.py — Unified deduplication service.

Consolidates inbound (message-id), outbound (content-hash), and request
(input content-hash within per-chat lock scope) dedup strategies behind a
single service with unified stats tracking.

- Inbound: checks message_id against the database's persistent index,
  with an in-memory LRU cache to short-circuit repeated async DB lookups.
- Outbound: xxHash (xxh64) content hash with TTL-based LRU cache.
- Request: per-chat content-hash of the input text within a short TTL
  window, catching double-sends and scheduled-vs-manual collisions inside
  the per-chat lock scope.

Usage::

    dedup = DeduplicationService(db=database)
    # Inbound check (called from Bot)
    if await dedup.is_inbound_duplicate("msg_123"):
        return
    # Batch inbound check (crash-recovery or burst processing)
    results = await dedup.batch_check_inbound(["msg_1", "msg_2", "msg_3"])
    # Single-pass outbound check + record (Scheduler — one hash computation)
    if dedup.check_and_record_outbound("chat_1", "response text"):
        return
    # Request dedup check (called inside per-chat lock scope)
    if dedup.check_and_record_request("chat_1", "user input text"):
        return  # skip duplicate LLM call
    # Two-phase keyed variant for when recording must happen after a
    # successful send — avoids redundant xxh64 computation:
    is_dup, key = dedup.check_outbound_with_key("chat_1", "response text")
    if is_dup:
        return
    # ... send message ...
    dedup.record_outbound_keyed(key)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import xxhash

from src.constants import (
    DEDUP_BATCH_SIZE,
    INBOUND_DEDUP_CACHE_MAX_SIZE,
    INBOUND_DEDUP_CACHE_TTL_SECONDS,
    MAX_OUTBOUND_HASH_TEXT_LENGTH,
    OUTBOUND_DEDUP_BUFFER_MAX_SIZE,
    OUTBOUND_DEDUP_MAX_SIZE,
    OUTBOUND_DEDUP_TTL_SECONDS,
    REQUEST_DEDUP_HASH_TEXT_LENGTH,
    REQUEST_DEDUP_MAX_SIZE,
    REQUEST_DEDUP_TTL_SECONDS,
)
from src.exceptions import DatabaseError
from src.utils import BoundedOrderedDict

if TYPE_CHECKING:
    from src.db.storage_protocol import StorageProvider as Storage

log = logging.getLogger(__name__)


def outbound_key(chat_id: str, text: str, *, hasher: xxhash.xxh64 | None = None) -> str:
    """Content-addressable key via xxHash (xxh64).

    Deterministic hash combining *chat_id* and *text* so identical
    outbound messages to the same chat produce the same key.
    xxHash is ~10× faster than SHA-256 and sufficient for a TTL-bounded
    LRU cache key (not a cryptographic use case).

    When *hasher* is provided, reuses it via ``reset()`` to avoid per-call
    object allocation (pooled hasher pattern from DeduplicationService).
    Text is truncated to ``MAX_OUTBOUND_HASH_TEXT_LENGTH`` characters before
    hashing to prevent slow hashing on huge LLM responses.
    """
    truncated = text[:MAX_OUTBOUND_HASH_TEXT_LENGTH]
    payload = f"{chat_id}\x00{truncated}".encode()
    if hasher is not None:
        hasher.reset()
        hasher.update(payload)
        return hasher.hexdigest()
    return xxhash.xxh64(payload).hexdigest()


@dataclass(slots=True)
class DedupStats:
    """Snapshot of dedup hit/miss counters for both strategies."""

    inbound_hits: int = 0
    inbound_misses: int = 0
    outbound_hits: int = 0
    outbound_misses: int = 0
    request_hits: int = 0
    request_misses: int = 0
    buffer_evictions: int = 0
    buffer_size: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "inbound_hits": self.inbound_hits,
            "inbound_misses": self.inbound_misses,
            "outbound_hits": self.outbound_hits,
            "outbound_misses": self.outbound_misses,
            "request_hits": self.request_hits,
            "request_misses": self.request_misses,
            "buffer_evictions": self.buffer_evictions,
            "buffer_size": self.buffer_size,
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
        "_hasher",
        "_inbound_cache",
        "_outbound_buffer",
        "_outbound_buffer_max",
        "_outbound_cache",
        "_request_cache",
        "_request_hash_text_length",
        "_stats",
        "_buffer_evictions",
    )

    def __init__(
        self,
        db: Storage,
        outbound_max_size: int = OUTBOUND_DEDUP_MAX_SIZE,
        outbound_ttl: float = OUTBOUND_DEDUP_TTL_SECONDS,
        inbound_cache_max_size: int = INBOUND_DEDUP_CACHE_MAX_SIZE,
        inbound_cache_ttl: float = INBOUND_DEDUP_CACHE_TTL_SECONDS,
        outbound_buffer_max: int = OUTBOUND_DEDUP_BUFFER_MAX_SIZE,
        request_max_size: int = REQUEST_DEDUP_MAX_SIZE,
        request_ttl: float = REQUEST_DEDUP_TTL_SECONDS,
        request_hash_text_length: int = REQUEST_DEDUP_HASH_TEXT_LENGTH,
    ) -> None:
        self._db = db
        self._hasher = xxhash.xxh64()
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
        self._request_cache: BoundedOrderedDict[str, bool] = BoundedOrderedDict(
            max_size=request_max_size,
            ttl=request_ttl,
        )
        self._request_hash_text_length = request_hash_text_length
        self._stats = DedupStats()
        self._buffer_evictions: int = 0

    def _outbound_key(self, chat_id: str, text: str) -> str:
        """Content-addressable key via pooled xxHash (xxh64).

        Reuses a single hasher instance via ``reset()`` to avoid per-call
        object allocation during burst dedup operations.  Text is truncated
        to ``MAX_OUTBOUND_HASH_TEXT_LENGTH`` characters before hashing.
        """
        truncated = text[:MAX_OUTBOUND_HASH_TEXT_LENGTH]
        self._hasher.reset()
        self._hasher.update(f"{chat_id}\x00{truncated}".encode())
        return self._hasher.hexdigest()

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

    async def batch_check_inbound(self, message_ids: list[str]) -> dict[str, bool]:
        """Batch-check which message IDs are duplicates.

        Checks the in-memory LRU cache first for each ID, then batch-queries
        the database for all uncached IDs in a single call — reducing N
        sequential async DB lookups to one batch call when processing
        crash-recovery backlogs or message bursts.

        Large inputs are chunked into batches of ``DEDUP_BATCH_SIZE`` to cap
        per-batch memory and CPU overhead.

        On database failure, logs a warning and treats uncached IDs as
        non-duplicates (fail-open), consistent with ``is_inbound_duplicate``.

        Returns:
            Dict mapping each input *message_id* to ``True`` (duplicate)
            or ``False`` (new).
        """
        if not message_ids:
            return {}

        if len(message_ids) <= DEDUP_BATCH_SIZE:
            return await self._batch_check_inbound_chunk(message_ids)

        # Chunk large batches to cap per-batch overhead.
        results: dict[str, bool] = {}
        for start in range(0, len(message_ids), DEDUP_BATCH_SIZE):
            chunk = message_ids[start : start + DEDUP_BATCH_SIZE]
            results.update(await self._batch_check_inbound_chunk(chunk))
        return results

    async def _batch_check_inbound_chunk(self, message_ids: list[str]) -> dict[str, bool]:
        """Process a single chunk for :meth:`batch_check_inbound`."""
        results: dict[str, bool] = {}
        uncached: list[str] = []

        # Phase 1: resolve from LRU cache
        for mid in message_ids:
            cached = self._inbound_cache.get(mid)
            if cached is not None:
                results[mid] = cached
                if cached:
                    self._stats.inbound_hits += 1
                else:
                    self._stats.inbound_misses += 1
            else:
                uncached.append(mid)

        if not uncached:
            return results

        # Phase 2: batch-query DB for remaining IDs
        try:
            db_results = await self._db.batch_message_exists(uncached)
        except DatabaseError:
            log.warning(
                "Dedup batch DB lookup failed for %d IDs — allowing through",
                len(uncached),
                exc_info=True,
            )
            for mid in uncached:
                results[mid] = False
            return results

        for mid in uncached:
            exists = db_results.get(mid, False)
            self._inbound_cache[mid] = exists
            results[mid] = exists
            if exists:
                self._stats.inbound_hits += 1
            else:
                self._stats.inbound_misses += 1

        return results

    # ── Outbound dedup (content-hash based, TTL LRU cache) ──────────────────

    def check_outbound_with_key(
        self, chat_id: str, text: str,
    ) -> tuple[bool, str]:
        """Check for outbound duplicate and return the pre-computed hash key.

        Check for outbound duplicate and return the pre-computed hash key
        so callers can pass it to :meth:`record_outbound_keyed` after a
        successful send — avoiding redundant xxh64 computation.

        Returns:
            ``(is_duplicate, key)`` — *is_duplicate* is ``True`` if the
            text was recently sent to *chat_id*; *key* is the xxh64 hex
            digest for ``chat_id + text``.
        """
        self.flush_outbound_batch()
        key = self._outbound_key(chat_id, text)
        if self._outbound_cache.get(key) is not None:
            self._stats.outbound_hits += 1
            return True, key
        self._stats.outbound_misses += 1
        return False, key

    def record_outbound_keyed(self, key: str) -> None:
        """Record an outbound entry using a pre-computed hash key.

        Use after :meth:`check_outbound_with_key` to avoid computing the
        xxh64 hash a second time.  Records directly into the cache (no
        buffering) because the caller has already confirmed the send
        succeeded.
        """
        self._outbound_cache[key] = True

    def flush_outbound_batch(self) -> None:
        """Flush buffered outbound recordings to the dedup cache.

        Precomputes content hashes for all buffered ``(chat_id, text)`` pairs
        and deduplicates them by hash key — the same pair may be buffered
        multiple times during a burst.  Passes only unique entries to
        ``BoundedOrderedDict.batch_set``, avoiding redundant OrderedDict
        move-to-end + overwrite operations for duplicate keys.

        Tracks buffer evictions when the buffer exceeds capacity.
        """
        if not self._outbound_buffer:
            return
        if len(self._outbound_buffer) > self._outbound_buffer_max:
            evicted = len(self._outbound_buffer) - self._outbound_buffer_max
            self._buffer_evictions += evicted
            self._outbound_buffer = self._outbound_buffer[-self._outbound_buffer_max:]
        buffer = self._outbound_buffer
        self._outbound_buffer = []
        unique = {self._outbound_key(cid, txt): True for cid, txt in buffer}
        self._outbound_cache.batch_set(unique.items())

    def check_and_record_outbound(self, chat_id: str, text: str) -> bool:
        """Combined check + record: returns ``True`` if duplicate.

        Computes the content hash **once** and performs both the duplicate
        check and (on miss) the recording in a single pass.  Use this
        instead of separate check + record calls to avoid redundant
        xxh64 computation.

        Flushes any buffered outbound recordings before checking so
        that recently-recorded entries are visible.
        """
        self.flush_outbound_batch()
        key = self._outbound_key(chat_id, text)
        if self._outbound_cache.get(key) is not None:
            self._stats.outbound_hits += 1
            return True
        self._outbound_cache[key] = True
        self._stats.outbound_misses += 1
        return False

    # ── Request dedup (input content-hash, per-chat, within lock scope) ─────

    def check_and_record_request(self, chat_id: str, text: str) -> bool:
        """Return ``True`` if *text* was recently submitted to *chat_id*.

        Detects near-simultaneous duplicate requests within the per-chat
        lock scope — e.g. a user double-sending slightly different text,
        or a scheduled task firing while a manually-triggered message is
        already being processed.  The text is truncated to
        ``_request_hash_text_length`` characters before hashing so that
        very long messages with only trailing differences still match.

        Always records the entry on miss so subsequent calls detect it
        within the TTL window.  Returns ``True`` on hit (duplicate detected).
        """
        truncated = text[: self._request_hash_text_length]
        key = self._outbound_key(chat_id, truncated)
        if self._request_cache.get(key) is not None:
            self._stats.request_hits += 1
            return True
        self._request_cache[key] = True
        self._stats.request_misses += 1
        return False

    # ── Stats ───────────────────────────────────────────────────────────────

    @property
    def stats(self) -> DedupStats:
        """Return a lightweight copy of dedup counters.

        Uses ``dataclasses.replace`` which copies the integer
        fields — cheaper than the previous manual construction and
        avoids sharing a mutable reference with callers.
        """
        return replace(
            self._stats,
            buffer_evictions=self._buffer_evictions,
            buffer_size=len(self._outbound_buffer),
        )


class NullDedupService:
    """NullObject that satisfies the ``DeduplicationService`` interface.

    Used when dedup is disabled or unavailable, eliminating downstream
    ``None``-checks.  Every method is a safe no-op; check methods always
    return ``False`` (never a duplicate), record methods are no-ops, and
    ``stats`` returns zeroed counters.
    """

    __slots__ = ()

    async def is_inbound_duplicate(self, message_id: str) -> bool:
        return False

    async def batch_check_inbound(self, message_ids: list[str]) -> dict[str, bool]:
        return {mid: False for mid in message_ids}

    def check_outbound_with_key(
        self, chat_id: str, text: str,
    ) -> tuple[bool, str]:
        return False, outbound_key(chat_id, text, hasher=None)

    def record_outbound_keyed(self, key: str) -> None:
        pass

    def flush_outbound_batch(self) -> None:
        pass

    def check_and_record_outbound(self, chat_id: str, text: str) -> bool:
        return False

    def check_and_record_request(self, chat_id: str, text: str) -> bool:
        return False

    @property
    def stats(self) -> DedupStats:
        return DedupStats(buffer_size=0)
