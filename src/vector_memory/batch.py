"""
Batch coalescing for VectorMemory embedding API calls.

Provides the BatchEmbedMixin which groups rapid save() calls into
batched embedding API requests, reducing per-request overhead.
"""

from __future__ import annotations

import asyncio
import logging

from src.utils.retry import retry_with_backoff
from src.vector_memory._utils import _cache_key, _track_embed_cache_event

log = logging.getLogger(__name__)

# Debounce window for coalescing individual save() embedding requests into a
# single batched API call.  Saves that arrive within this window are grouped
# together, reducing per-request API overhead.
_BATCH_DEBOUNCE = 0.05  # 50 ms

# Safety cap on the number of texts sent in a single batched API call.
_MAX_BATCH_SIZE = 64


class BatchEmbedMixin:
    """Mixin providing batch embedding coalescing for VectorMemory.

    Expects the host class to provide:
        _client          — AsyncOpenAI instance
        _embedding_model — str model name
        _cache_lock      — ThreadLock protecting _embed_cache
        _embed_cache     — BoundedOrderedDict LRU cache
        _inflight        — dict of in-flight embedding futures
        _pending_saves   — list of (text, Future) pairs
        _flush_handle    — asyncio.TimerHandle | None
    """

    async def _batched_embed(self, text: str) -> list[float]:
        """Queue *text* for a batched embedding call with debounce coalescing.

        If other ``save()`` calls arrive within ``_BATCH_DEBOUNCE`` seconds
        they are grouped into a single ``_embed_batch()`` call, reducing
        per-request API overhead.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[float]] = loop.create_future()
        self._pending_saves.append((text, future))

        if self._flush_handle is None:
            self._flush_handle = loop.call_later(
                _BATCH_DEBOUNCE,
                lambda: asyncio.ensure_future(self._flush_pending()),
            )

        return await future

    async def _flush_pending(self) -> None:
        """Drain the pending-save queue through ``_embed_batch()``.

        Large batches are chunked to respect ``_MAX_BATCH_SIZE`` so that a
        single oversized API call doesn't exceed provider limits.
        """
        self._flush_handle = None

        if not self._pending_saves:
            return

        # Atomically drain — new saves arriving during flush go into a fresh list
        pending = self._pending_saves
        self._pending_saves = []

        # Process in chunks capped at _MAX_BATCH_SIZE to avoid oversized API calls
        for start in range(0, len(pending), _MAX_BATCH_SIZE):
            chunk = pending[start : start + _MAX_BATCH_SIZE]
            texts = [text for text, _ in chunk]
            try:
                embeddings = await self._embed_batch(texts)
                for (_, future), embedding in zip(chunk, embeddings):
                    if not future.done():
                        future.set_result(embedding)
            except Exception as exc:
                for _, future in chunk:
                    if not future.done():
                        future.set_exception(exc)

    @retry_with_backoff(max_retries=2, initial_delay=0.5)
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a single API call.

        Checks the LRU cache and in-flight deduplication for each text first.
        Only texts that miss both caches are sent to the API in one batched
        ``embeddings.create(input=[...])`` call, significantly reducing API
        overhead when multiple memories are saved in quick succession.

        Returns embeddings in the same order as *texts*.
        """
        results: list[list[float] | None] = [None] * len(texts)
        # Map: position index → cache_key for texts that need an API call
        pending: dict[int, str] = {}
        # Map: cache_key → position indices (a single text may appear multiple times)
        key_to_indices: dict[str, list[int]] = {}

        for i, text in enumerate(texts):
            cache_key = _cache_key(text)

            # 1. Check LRU cache
            with self._cache_lock:
                if cache_key in self._embed_cache:
                    results[i] = self._embed_cache[cache_key]
                    _track_embed_cache_event(hit=True)
                    continue

            # 2. Check in-flight dedup
            if cache_key in self._inflight:
                results[i] = await self._inflight[cache_key]
                continue

            # 3. Needs API call — track by cache key
            _track_embed_cache_event(hit=False)
            pending[i] = cache_key
            key_to_indices.setdefault(cache_key, []).append(i)

        if not pending:
            # All embeddings resolved from cache / in-flight
            return results  # type: ignore[return-value]

        # 4. Build the batch input: one entry per unique cache key
        unique_keys = list(key_to_indices.keys())
        # Use the first position index for each unique key to get the text
        unique_texts = [texts[key_to_indices[k][0]] for k in unique_keys]
        # Map position in batch request → cache_key
        key_for_batch_pos: dict[int, str] = {
            batch_pos: key for batch_pos, key in enumerate(unique_keys)
        }

        # Register in-flight futures for deduplication of concurrent callers
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[list[float]]] = {}
        for key in unique_keys:
            future: asyncio.Future[list[float]] = loop.create_future()
            self._inflight[key] = future
            futures[key] = future

        try:
            resp = await self._client.embeddings.create(
                model=self._embedding_model,
                input=unique_texts,
                encoding_format="float",
            )

            if len(resp.data) != len(unique_texts):
                raise ValueError(
                    f"Embeddings API returned {len(resp.data)} results "
                    f"for {len(unique_texts)} inputs — possible content "
                    f"filtering or API error"
                )

            self._mark_embedding_api_healthy()

            # Populate cache and resolve futures
            for batch_pos, item in enumerate(resp.data):
                embedding = item.embedding
                cache_key = key_for_batch_pos[batch_pos]

                with self._cache_lock:
                    self._embed_cache[cache_key] = embedding

                futures[cache_key].set_result(embedding)

            # Fill results for all pending positions
            for idx, cache_key in pending.items():
                results[idx] = self._embed_cache[cache_key]  # already populated above

            return results  # type: ignore[return-value]
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            # Propagate error to all waiters
            for key, future in futures.items():
                if not future.done():
                    future.set_exception(exc)
            raise
        finally:
            for key in unique_keys:
                self._inflight.pop(key, None)
