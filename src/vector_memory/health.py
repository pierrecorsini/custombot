"""
Embedding API health monitoring and retry queue for VectorMemory.

Provides the EmbeddingHealthMixin which tracks embedding API reachability
and queues failed saves for automatic retry when the API recovers.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.core.errors import NonCriticalCategory, log_noncritical

log = logging.getLogger(__name__)

# TTL for the embedding API health cache.  When the embeddings endpoint is
# confirmed unreachable, subsequent calls short-circuit for this many seconds
# instead of waiting for the full API timeout on every attempt.
_EMBED_HEALTH_TTL = 60.0

# Maximum number of queued retry entries.  Oldest entries are dropped when
# the cap is exceeded to prevent unbounded memory growth during extended outages.
_MAX_RETRY_QUEUE_SIZE = 1000


class EmbeddingHealthMixin:
    """Mixin providing embedding API health checks and retry queue management.

    Expects the host class to provide:
        _cache_lock          — ThreadLock protecting _embed_api_* state
        _embed_api_healthy   — bool flag
        _embed_api_last_check — float timestamp
        _pending_retries     — list of (chat_id, text, category, queued_at)
    """

    def _check_embedding_api_health(self) -> None:
        """Raise immediately if the embedding API was recently confirmed down.

        When the API is unreachable every ``_embed()`` / ``_embed_batch()``
        call would block for the full HTTP timeout (potentially 120 s).  After
        a failure we record the timestamp and short-circuit subsequent calls
        for ``_EMBED_HEALTH_TTL`` seconds so the event loop is not blocked.
        """
        with self._cache_lock:
            if self._embed_api_healthy:
                return
            elapsed = time.monotonic() - self._embed_api_last_check
            if elapsed >= _EMBED_HEALTH_TTL:
                # TTL expired — allow a fresh probe
                return
        remaining = round(_EMBED_HEALTH_TTL - elapsed, 1)
        raise ConnectionError(
            f"Embedding API unreachable (last failure {elapsed:.0f}s ago, "
            f"retrying in ~{remaining}s)"
        )

    def _mark_embedding_api_healthy(self) -> None:
        """Record that the embedding API is reachable."""
        with self._cache_lock:
            self._embed_api_healthy = True
            self._embed_api_last_check = time.monotonic()

    def _mark_embedding_api_unhealthy(self) -> None:
        """Record that the embedding API is unreachable."""
        with self._cache_lock:
            self._embed_api_healthy = False
            self._embed_api_last_check = time.monotonic()

    def _queue_for_retry(
        self,
        chat_id: str,
        text: str,
        category: str,
    ) -> None:
        """Queue a failed save for later retry, capping queue size."""
        if len(self._pending_retries) >= _MAX_RETRY_QUEUE_SIZE:
            dropped = self._pending_retries.pop(0)
            log.warning(
                "Retry queue full (%d); dropping oldest save for chat=%s",
                _MAX_RETRY_QUEUE_SIZE,
                dropped[0],
            )
        self._pending_retries.append((chat_id, text, category, time.time()))
        log.debug(
            "Queued vector memory save for retry (chat=%s, queue=%d)",
            chat_id,
            len(self._pending_retries),
        )

    async def _retry_pending_saves(self) -> None:
        """Attempt to flush queued retry saves when the embedding API recovers.

        Called opportunistically after a successful save.  If the first retry
        fails, the remaining items are re-queued and we back off until the
        next successful operation.
        """
        if not self._pending_retries:
            return

        # Only attempt retries if the health TTL has expired or API is healthy
        with self._cache_lock:
            if not self._embed_api_healthy:
                elapsed = time.monotonic() - self._embed_api_last_check
                if elapsed < _EMBED_HEALTH_TTL:
                    return  # Still in cooldown

        items = self._pending_retries[:]
        self._pending_retries.clear()
        retried = 0
        for chat_id, text, category, queued_at in items:
            try:
                self._check_embedding_api_health()
                embedding = await self._batched_embed(text)
                now = time.time()
                await asyncio.to_thread(
                    self._insert_entry, chat_id, text, category, now, embedding,
                )
                retried += 1
                age = round(now - queued_at, 1)
                log.debug(
                    "Retry-saved vector memory chat=%s (queued %.1fs ago)",
                    chat_id,
                    age,
                )
            except Exception as exc:
                log_noncritical(
                    NonCriticalCategory.EMBEDDING,
                    "Retry still failing for vector memory save chat=%s: %s",
                    chat_id,
                    exc,
                    logger=log,
                )
                # Re-queue this item plus all remaining; API still down
                remaining = [(chat_id, text, category, queued_at)]
                idx = items.index((chat_id, text, category, queued_at))
                remaining.extend(items[idx + 1 :])
                self._pending_retries.extend(remaining)
                break

        if retried:
            log.info(
                "Flushed %d/%d queued vector memory saves",
                retried,
                len(items),
            )
