"""
context_cache.py — Pre-computed context templates for faster assembly.

Caches the assembled context template per routing rule so that only the
variable portions (user message, recent history) are filled on each turn.
Invalidated automatically when routing rules change during hot-reload.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Cache TTL in seconds.
_CACHE_TTL: float = 300.0

# Maximum cached entries before LRU eviction.
_MAX_CACHE_SIZE: int = 50


@dataclass(slots=True, frozen=True)
class _CacheKey:
    """Hashable cache key: (rule_id, system_prompt_hash, tools_hash)."""

    rule_id: str
    system_prompt_hash: str
    tools_hash: str


@dataclass(slots=True)
class _CacheEntry:
    """A cached template with its assembled parts and creation time."""

    static_messages: list[dict[str, Any]]  # system + instruction messages
    created_at: float


class ContextTemplateCache:
    """LRU cache for pre-assembled context templates.

    Cache key combines the routing rule ID, system prompt content hash,
    and tool definitions hash.  On a cache hit, only variable slots
    (user message, recent history) are appended — the static portion
    (system prompt, instructions, channel prompt) is reused.

    Cache is invalidated when routing rules change (config hot-reload)
    or when entries exceed their TTL.
    """

    def __init__(
        self,
        ttl: float = _CACHE_TTL,
        max_size: int = _MAX_CACHE_SIZE,
    ) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._entries: OrderedDict[_CacheKey, _CacheEntry] = OrderedDict()
        # Incremented on routing rule changes; old entries are pruned lazily.
        self._generation: int = 0

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def _make_key(
        self,
        rule_id: str,
        system_prompt: str,
        tool_definitions: str,
    ) -> _CacheKey:
        return _CacheKey(
            rule_id=rule_id,
            system_prompt_hash=self._hash_content(system_prompt),
            tools_hash=self._hash_content(tool_definitions),
        )

    def get(
        self,
        rule_id: str,
        system_prompt: str,
        tool_definitions: str,
    ) -> list[dict[str, Any]] | None:
        """Return cached static messages if present and not expired."""
        key = self._make_key(rule_id, system_prompt, tool_definitions)
        entry = self._entries.get(key)
        if entry is None:
            return None

        if time.monotonic() - entry.created_at > self._ttl:
            self._entries.pop(key, None)
            return None

        self._entries.move_to_end(key)  # O(1) LRU refresh
        return list(entry.static_messages)

    def put(
        self,
        rule_id: str,
        system_prompt: str,
        tool_definitions: str,
        static_messages: list[dict[str, Any]],
    ) -> None:
        """Store assembled static messages in the cache."""
        key = self._make_key(rule_id, system_prompt, tool_definitions)

        if len(self._entries) >= self._max_size and key not in self._entries:
            # Evict oldest entry — O(1) with OrderedDict
            self._entries.popitem(last=False)

        self._entries[key] = _CacheEntry(
            static_messages=list(static_messages),
            created_at=time.monotonic(),
        )
        self._entries.move_to_end(key)  # O(1) LRU refresh

    def invalidate(self) -> None:
        """Clear all cache entries (called on routing rule changes)."""
        self._entries.clear()
        self._generation += 1
        log.debug("Context template cache invalidated (generation=%d)", self._generation)

    @property
    def size(self) -> int:
        return len(self._entries)


__all__ = ["ContextTemplateCache"]
