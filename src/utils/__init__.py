"""
Utility modules for custombot.

Provides:
- LRULockCache: Bounded LRU cache for asyncio.Lock objects
- async_file: Non-blocking file I/O utilities
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from src.constants import MAX_LRU_CACHE_SIZE
from src.utils.async_file import (
    async_read_text,
    async_write_text,
    async_append_text,
    async_read_bytes,
    async_exists,
)
from src.utils.disk import (
    check_disk_space,
    ensure_disk_space,
    DiskSpaceResult,
    DEFAULT_MIN_DISK_SPACE,
    DISK_SPACE_WARNING_THRESHOLD,
)
from src.utils.logging_utils import log_execution
from src.utils.timing import (
    OperationTimer,
    TimingResult,
    skill_timer,
    timed_operation,
    DEFAULT_SLOW_THRESHOLD_SECONDS,
)
from src.utils.json_utils import (
    safe_json_parse,
    safe_json_parse_line,
    safe_json_parse_with_error,
    JsonParseResult,
)
from src.utils.singleton import (
    singleton,
    SingletonMeta,
    get_or_create_singleton,
    reset_singleton,
    create_singleton_getter,
)
from src.utils.async_executor import (
    AsyncExecutor,
    ExecutorResult,
)


class LRULockCache:
    """
    Bounded LRU cache for asyncio.Lock objects.

    Prevents unbounded memory growth by evicting the least recently used
    lock when the cache reaches max_size.

    Uses __slots__ for memory efficiency since lock cache objects are
    frequently created. Reduces per-instance memory overhead.

    Thread-safe via internal async lock.

    Attributes:
        _cache: OrderedDict storing key -> asyncio.Lock mappings
        _max_size: Maximum number of locks to retain
        _lock: Async lock for thread-safe cache operations
    """

    __slots__ = ("_cache", "_max_size", "_lock")

    def __init__(self, max_size: int = MAX_LRU_CACHE_SIZE) -> None:
        """
        Initialize the LRU lock cache.

        Args:
            max_size: Maximum number of locks to retain. Default is MAX_LRU_CACHE_SIZE.
        """
        self._cache: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def get_or_create(self, key: str) -> asyncio.Lock:
        """
        Get an existing lock or create a new one.

        If the key exists, moves it to the end (most recently used).
        If cache is full, evicts the oldest (least recently used) entry.

        Args:
            key: Unique identifier for the lock (e.g., chat_id).

        Returns:
            The asyncio.Lock for the given key.
        """
        async with self._lock:
            if key in self._cache:
                # Move to end (most recently used)
                self._cache.move_to_end(key)
                return self._cache[key]

            # Evict oldest if at capacity
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            # Create new lock
            new_lock = asyncio.Lock()
            self._cache[key] = new_lock
            return new_lock

    def __len__(self) -> int:
        """Return current number of cached locks."""
        return len(self._cache)

    @property
    def max_size(self) -> int:
        """Return the maximum cache size."""
        return self._max_size


__all__ = [
    "LRULockCache",
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
    "safe_json_parse",
    "safe_json_parse_line",
    "safe_json_parse_with_error",
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
