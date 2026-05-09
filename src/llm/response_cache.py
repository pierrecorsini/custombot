"""
llm.response_cache — TTL-based LLM response cache.

Caches identical prompts using SHA-256 hashing to avoid redundant
LLM calls for repeated questions.  Uses LRU eviction when the
cache reaches max size.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300  # 5 minutes
DEFAULT_MAX_SIZE = 100


@dataclass(slots=True, frozen=True)
class CacheEntry:
    """A single cached LLM response."""

    response_text: str
    cached_at: float
    model: str


class LLMResponseCache:
    """TTL-based cache for LLM responses keyed by prompt hash.

    Cache key is the SHA-256 hash of the concatenation of system prompt,
    context, and user message.  Entries expire after ``ttl_seconds`` and
    the cache uses LRU eviction at ``max_size``.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_size: int = DEFAULT_MAX_SIZE,
    ) -> None:
        self._enabled = enabled
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _make_key(
        self,
        messages: list[ChatCompletionMessageParam],
    ) -> str:
        """Create a deterministic cache key from messages."""
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            parts.append(f"{role}:{content}")
        combined = "|".join(parts)
        return hashlib.sha256(combined.encode()).hexdigest()

    def get(
        self,
        messages: list[ChatCompletionMessageParam],
        model: str = "",
    ) -> str | None:
        """Return cached response text if present and not expired."""
        if not self._enabled:
            return None

        key = self._make_key(messages)
        entry = self._cache.get(key)

        if entry is None:
            self._misses += 1
            return None

        if time.monotonic() - entry.cached_at > self._ttl:
            self._cache.pop(key, None)
            self._misses += 1
            log.debug("Cache entry expired for key %s…", key[:12])
            return None

        # Promote to most-recently-used
        self._cache.move_to_end(key)
        self._hits += 1
        log.debug("Cache hit for key %s… (age=%.1fs)", key[:12], time.monotonic() - entry.cached_at)
        return entry.response_text

    def put(
        self,
        messages: list[ChatCompletionMessageParam],
        response_text: str,
        model: str = "",
    ) -> None:
        """Store a response in the cache."""
        if not self._enabled or not response_text:
            return

        key = self._make_key(messages)

        # Evict oldest if at capacity
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)

        self._cache[key] = CacheEntry(
            response_text=response_text,
            cached_at=time.monotonic(),
            model=model,
        )

    def clear(self) -> None:
        """Remove all entries from the cache."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0

    def stats(self) -> dict[str, int | float]:
        """Return cache statistics for observability."""
        total = self._hits + self._misses
        hit_rate = (self._hits / total) if total > 0 else 0.0
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 3),
            "ttl_seconds": self._ttl,
        }

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns count of evicted entries."""
        if not self._enabled:
            return 0

        now = time.monotonic()
        expired_keys = [
            k for k, v in self._cache.items()
            if now - v.cached_at > self._ttl
        ]
        for k in expired_keys:
            del self._cache[k]

        if expired_keys:
            log.debug("Evicted %d expired cache entries", len(expired_keys))
        return len(expired_keys)
