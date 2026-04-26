"""
locking.py — Centralised locking policy, helpers, and mixins.

Provides two lock types with a documented decision guide so that every
module uses the *correct* strategy and no ad-hoc lazy-initialisation
leaks into production code.

┌─────────────────────────────────────────────────────────────────────┐
│  LOCKING POLICY                                                     │
│                                                                     │
│  ThreadLock (wraps threading.Lock):                                 │
│    • For code called from BOTH sync and async contexts.             │
│    • Safe to acquire from any thread.                               │
│    • MUST NOT be held across ``await`` — it blocks the event loop.  │
│    • Examples: rate_limiter, vector_memory, SQLite writes.          │
│                                                                     │
│  AsyncLock (wraps asyncio.Lock):                                    │
│    • For async-only code.                                           │
│    • Does NOT block the event loop while waiting.                   │
│    • Safe to hold across ``await`` points.                          │
│    • Lazy-initialised — safe to create before the event loop runs.  │
│    • Examples: db.py, message_queue, event_bus, shutdown.           │
│                                                                     │
│  Decision guide:                                                    │
│    1. All call sites are async methods  → AsyncLock                 │
│    2. Some call sites are synchronous   → ThreadLock                │
│    3. NEVER use asyncio.Lock in a synchronous context               │
│    4. NEVER hold ThreadLock across an await                         │
│                                                                     │
│  Helper mixins:                                                     │
│    Both classes support ``async with`` / ``with`` context managers, │
│    ``acquire()`` / ``release()`` explicit calls, and a ``locked``   │
│    property for introspection.                                      │
│                                                                     │
│  Mixin classes:                                                     │
│    ``ThreadLockMixin`` — provides ``self._lock`` (ThreadLock) +     │
│    convenience ``with self._lock:`` support for sync code.          │
│    ``AsyncLockMixin`` — provides ``self._lock`` (AsyncLock) +       │
│    convenience ``async with self._lock:`` support for async code.   │
└─────────────────────────────────────────────────────────────────────┘

Usage::

    # Async-only code (safe across await):
    from src.utils.locking import AsyncLock

    class MyService:
        def __init__(self) -> None:
            self._lock = AsyncLock()

        async def do_work(self) -> None:
            async with self._lock:
                await some_async_op()

    # Mixed sync/async code (never across await):
    from src.utils.locking import ThreadLock

    class MyCache:
        def __init__(self) -> None:
            self._lock = ThreadLock()

        def get(self, key: str) -> Any:
            with self._lock:
                return self._data[key]

    # Using mixins for less boilerplate:
    from src.utils.locking import ThreadLockMixin, AsyncLockMixin

    class MyStore(ThreadLockMixin):
        def save(self, data: str) -> None:
            with self._lock:
                self._data = data

    class MyAsyncService(AsyncLockMixin):
        async def process(self) -> None:
            async with self._lock:
                await self._do_work()
"""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType
from typing import Optional

__all__ = ["AsyncLock", "ThreadLock", "AsyncLockMixin", "ThreadLockMixin"]


# ── ThreadLock ────────────────────────────────────────────────────────


class ThreadLock:
    """Thread-safe lock for mixed sync/async contexts.

    Wraps :class:`threading.Lock` with documented constraints.  Use when
    the lock *may* be acquired from synchronous call-sites (e.g., SQLite
    writes, rate-limit tracking, cache access).

    CONSTRAINTS:
      * **NEVER** hold this lock across ``await`` — it blocks the entire
        event loop thread.
      * For async-only code, use :class:`AsyncLock` instead.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── sync context-manager protocol ──────────────────────────────

    def __enter__(self) -> "ThreadLock":
        self._lock.__enter__()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> bool:
        return self._lock.__exit__(exc_type, exc_val, exc_tb)

    # ── explicit acquire / release ─────────────────────────────────

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock (blocking or non-blocking).

        Args:
            blocking: If True, block until acquired.
            timeout: Max seconds to wait (-1 = infinite).

        Returns:
            True if the lock was acquired.
        """
        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        """Release the lock."""
        self._lock.release()

    # ── introspection ──────────────────────────────────────────────

    @property
    def locked(self) -> bool:
        """Return ``True`` if the lock is currently held."""
        return self._lock.locked()


# ── AsyncLock ────────────────────────────────────────────────────────


class AsyncLock:
    """Lazy-initialised ``asyncio.Lock`` safe to create before the event loop starts.

    On Python 3.10+ (especially Windows with ``ProactorEventLoop``),
    ``asyncio.Lock()`` binds to the current event loop at creation time.
    If the lock is created in ``__init__`` before the loop is running, it
    can bind to the wrong loop or raise ``RuntimeError``.  This wrapper
    defers lock creation until the first ``acquire()`` call, which always
    happens inside a running event loop.

    Drop-in replacement for the common pattern::

        # Before (repeated in 6+ files):
        self._lock: asyncio.Lock | None = None

        def _get_lock(self) -> asyncio.Lock:
            if self._lock is None:
                self._lock = asyncio.Lock()
            return self._lock

        async with self._get_lock():
            ...

        # After:
        self._lock = AsyncLock()

        async with self._lock:
            ...
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None

    # ── context-manager protocol ──────────────────────────────────────

    async def __aenter__(self) -> "AsyncLock":
        if self._lock is None:
            self._lock = asyncio.Lock()
        await self._lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self._lock is not None:  # pragma: no cover — always set after __aenter__
            self._lock.release()

    # ── explicit acquire / release ────────────────────────────────────

    async def acquire(self) -> None:
        """Acquire the lock (async, non-blocking to the event loop)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        await self._lock.acquire()

    def release(self) -> None:
        """Release the lock."""
        if self._lock is not None:
            self._lock.release()

    # ── introspection ─────────────────────────────────────────────────

    @property
    def locked(self) -> bool:
        """Return ``True`` if the lock is currently held."""
        return self._lock is not None and self._lock.locked()


# ── Mixins ────────────────────────────────────────────────────────────


class ThreadLockMixin:
    """Mixin providing a ``ThreadLock`` at ``self._lock``.

    Eliminates the repeated ``self._lock = ThreadLock()`` boilerplate in
    classes that use sync locking (rate limiters, caches, SQLite helpers).

    Usage::

        class MyStore(ThreadLockMixin):
            def save(self, key: str, value: str) -> None:
                with self._lock:
                    self._data[key] = value
    """

    __slots__ = ("_lock",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._lock = ThreadLock()
        super().__init__(*args, **kwargs)


class AsyncLockMixin:
    """Mixin providing a lazy-initialised ``AsyncLock`` at ``self._lock``.

    Eliminates the repeated ``self._lock = AsyncLock()`` boilerplate in
    async classes (message stores, queues, event buses).  The underlying
    ``asyncio.Lock`` is only created on first ``acquire()``, so the mixin
    is safe to use in ``__init__`` before the event loop starts.

    Usage::

        class MyService(AsyncLockMixin):
            async def process(self) -> None:
                async with self._lock:
                    await self._do_work()
    """

    __slots__ = ("_lock",)

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._lock = AsyncLock()
        super().__init__(*args, **kwargs)
