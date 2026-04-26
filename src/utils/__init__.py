"""
Utility modules for custombot.

Provides:
- LRULockCache: Bounded LRU cache for asyncio.Lock objects
- LRUDict: Generic bounded LRU dictionary for arbitrary key-value data
- async_file: Non-blocking file I/O utilities
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from src.constants import MAX_LRU_CACHE_SIZE

log = logging.getLogger(__name__)
from src.utils.async_executor import (
    AsyncExecutor,
    ExecutorResult,
)
from src.utils.async_file import (
    async_append_text,
    async_exists,
    async_read_bytes,
    async_read_text,
    async_write_text,
)
from src.utils.disk import (
    DEFAULT_MIN_DISK_SPACE,
    DISK_SPACE_WARNING_THRESHOLD,
    DiskSpaceResult,
    check_disk_space,
    ensure_disk_space,
)
from src.utils.json_utils import (
    JSONDecodeError,
    JsonParseMode,
    JsonParseResult,
    _HAS_MSGPACK,
    _HAS_ORJSON,
    json_dumps,
    json_loads,
    msgpack_dumps,
    msgpack_loads,
    safe_json_parse,
)
from src.utils.logging_utils import log_execution
from src.utils.singleton import (
    SingletonMeta,
    create_singleton_getter,
    get_or_create_singleton,
    reset_singleton,
    singleton,
)
from src.utils.timing import (
    DEFAULT_SLOW_THRESHOLD_SECONDS,
    OperationTimer,
    TimingResult,
    skill_timer,
    timed_operation,
)


class LRULockCache:
    """
    Bounded LRU cache for asyncio.Lock objects with reference-tracked eviction.

    Prevents unbounded memory growth by evicting the least recently used
    lock when the cache reaches max_size. Locks with active references
    (handed out via ``get_or_create`` but not yet released) are never evicted,
    preventing the race condition where a coroutine holds a lock that was
    evicted between ``get_or_create()`` and ``async with lock:``.

    Use ``acquire(key)`` (async context manager) for the simplest safe usage,
    or pair ``get_or_create()`` with ``release()`` in a try/finally.

    Attributes:
        _cache: OrderedDict storing key -> asyncio.Lock mappings
        _ref_counts: Dict tracking how many references are outstanding per key
        _max_size: Maximum number of locks to retain
        _lock: AsyncLock for thread-safe cache operations (see src.utils.locking)
    """

    __slots__ = ("_cache", "_max_size", "_lock", "_ref_counts")

    def __init__(self, max_size: int = MAX_LRU_CACHE_SIZE) -> None:
        """
        Initialize the LRU lock cache.

        Args:
            max_size: Maximum number of locks to retain. Default is MAX_LRU_CACHE_SIZE.
        """
        self._cache: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_size = max_size
        # AsyncLock defers asyncio.Lock creation until first use — see
        # src.utils.locking policy.
        from src.utils.locking import AsyncLock

        self._lock = AsyncLock()
        self._ref_counts: dict[str, int] = {}

    async def get_or_create(self, key: str) -> asyncio.Lock:
        """
        Get an existing lock or create a new one, incrementing its reference count.

        If the key exists, moves it to the end (most recently used).
        If cache is full, evicts the oldest entry with zero references and
        not currently locked. If all entries are in use, the cache grows
        beyond max_size temporarily.

        Callers **must** call ``release(key)`` when done with the lock,
        or use the ``acquire()`` context manager instead.

        Args:
            key: Unique identifier for the lock (e.g., chat_id).

        Returns:
            The asyncio.Lock for the given key.
        """
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._ref_counts[key] = self._ref_counts.get(key, 0) + 1
                return self._cache[key]

            self._evict_one()
            new_lock = asyncio.Lock()
            self._cache[key] = new_lock
            self._ref_counts[key] = 1
            return new_lock

    def release(self, key: str) -> None:
        """
        Decrement the reference count for *key*.

        After the caller has finished using the lock (typically after
        the ``async with lock:`` block exits), call this to allow
        the entry to become eligible for eviction again.

        Args:
            key: The lock key previously passed to ``get_or_create()``.
        """
        if key in self._ref_counts:
            self._ref_counts[key] -= 1
            if self._ref_counts[key] <= 0:
                del self._ref_counts[key]

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        """
        Context manager that gets, ref-tracks, acquires, and releases a lock.

        Combines ``get_or_create()``, ``async with lock:``, and ``release()``
        into a single call so callers never miss the release step.

        Args:
            key: Unique identifier for the lock (e.g., chat_id).
        """
        lock = await self.get_or_create(key)
        try:
            async with lock:
                yield
        finally:
            self.release(key)

    def _evict_one(self) -> None:
        """Evict the oldest entry with zero references and not currently locked."""
        if len(self._cache) < self._max_size:
            return
        for k in list(self._cache.keys()):
            if self._ref_counts.get(k, 0) == 0 and not self._cache[k].locked():
                self._cache.pop(k)
                self._ref_counts.pop(k, None)
                return
        log.warning(
            "All %d cached locks are in-use; growing cache beyond max_size",
            len(self._cache),
        )

    def __len__(self) -> int:
        """Return current number of cached locks."""
        return len(self._cache)

    @property
    def max_size(self) -> int:
        """Return the maximum cache size."""
        return self._max_size


class LRUDict:
    """Generic bounded LRU dictionary for arbitrary key-value data.

    Drop-in replacement for manual OrderedDict + while-loop eviction.
    Evicts the least recently used entry when the cache reaches max_size.

    Accepts any value type (int, str, list, tuple, etc.).

    Example:
        cache = LRUDict(max_size=500)
        cache["chat_123"] = 42
        value = cache.get("chat_123", 0)  # 42
        cache.pop("chat_123")              # remove explicitly
    """

    __slots__ = ("_cache", "_max_size")

    def __init__(self, max_size: int = MAX_LRU_CACHE_SIZE) -> None:
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max_size = max_size

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value, moving the key to most-recently-used."""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def __getitem__(self, key: str) -> Any:
        """Get a value, moving the key to most-recently-used."""
        self._cache.move_to_end(key)
        return self._cache[key]

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value with default, moving to end if found."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return default

    def pop(self, key: str, default: Any = None) -> Any:
        """Remove and return a value, or default if not found."""
        return self._cache.pop(key, default)


__all__ = [
    "LRULockCache",
    "LRUDict",
    "async_read_text",
    "async_write_text",
    "async_append_text",
    "async_read_bytes",
    "async_exists",
    "check_disk_space",
    "ensure_disk_space",
    "DiskSpaceResult",
    "DEFAULT_MIN_DISK_SPACE",
    "DISK_SPACE_WARNING_THRESHOLD",
    # Logging utilities
    "log_execution",
    # Timing utilities
    "OperationTimer",
    "TimingResult",
    "skill_timer",
    "timed_operation",
    "DEFAULT_SLOW_THRESHOLD_SECONDS",
    # JSON utilities
    "json_dumps",
    "json_loads",
    "JSONDecodeError",
    "_HAS_ORJSON",
    "_HAS_MSGPACK",
    "msgpack_dumps",
    "msgpack_loads",
    "JsonParseMode",
    "safe_json_parse",
    "JsonParseResult",
    # Singleton utilities
    "singleton",
    "SingletonMeta",
    "get_or_create_singleton",
    "reset_singleton",
    "create_singleton_getter",
    # Async executor
    "AsyncExecutor",
    "ExecutorResult",
]
