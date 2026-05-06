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
from src.utils.circuit_breaker import CircuitBreaker

log = logging.getLogger(__name__)

# Maximum number of queued retry entries.  Oldest entries are dropped when
# the cap is exceeded to prevent unbounded memory growth during extended outages.
_MAX_RETRY_QUEUE_SIZE = 1000


class EmbeddingHealthMixin:
    """Mixin providing embedding API health checks and retry queue management.

    Expects the host class to provide:
        _circuit_breaker     — CircuitBreaker instance tracking API health
        _pending_retries     — list of (chat_id, text, category, queued_at)
    """

    async def _check_embedding_api_health(self) -> None:
        """Raise immediately if the embedding API circuit breaker is open."""
        if await self._circuit_breaker.is_open():
            raise ConnectionError("Embedding API unreachable (circuit breaker open)")

    async def _mark_embedding_api_healthy(self) -> None:
        """Record that the embedding API is reachable."""
        await self._circuit_breaker.record_success()

    async def _mark_embedding_api_unhealthy(self) -> None:
        """Record that the embedding API is unreachable."""
        await self._circuit_breaker.record_failure()

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

        # Only attempt retries if the circuit breaker allows it
        if await self._circuit_breaker.is_open():
            return

        items = self._pending_retries[:]
        self._pending_retries.clear()
        retried = 0
        for chat_id, text, category, queued_at in items:
            try:
                await self._check_embedding_api_health()
                embedding = await self._batched_embed(text)
                now = time.time()
                await asyncio.to_thread(
                    self._insert_entry,
                    chat_id,
                    text,
                    category,
                    now,
                    embedding,
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
