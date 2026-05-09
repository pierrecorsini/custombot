"""
src/db/file_pool.py — Bounded LRU pools for file handles.

Provides two pools:

* ``FileHandlePool`` — append-mode handles for JSONL writes.
* ``ReadHandlePool`` — read-mode handles for JSONL reads.

Both prevent OS file-descriptor exhaustion under extreme concurrency by
reusing open file handles instead of open/close per operation.

Thread-safe via ``ThreadLock`` (see src.utils.locking) because file I/O runs
inside ``asyncio.to_thread()`` workers (no event loop available in those threads).
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from src.core.errors import NonCriticalCategory, log_noncritical
from src.utils.locking import ThreadLock
from typing import IO, TYPE_CHECKING

from src.constants import MAX_FILE_HANDLES, MAX_READ_FILE_HANDLES
from src.exceptions import DatabaseError, ErrorCode

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


class FileHandlePool:
    """Bounded LRU pool of append-mode file handles for message JSONL files.

    Handles are opened in line-buffered append mode (``buffering=1``) so every
    newline-terminated JSONL record is flushed to the OS immediately.
    """

    __slots__ = ("_handles", "_lock", "_max_size")

    def __init__(self, max_size: int = MAX_FILE_HANDLES) -> None:
        self._max_size = max_size
        self._handles: OrderedDict[str, IO[str]] = OrderedDict()
        self._lock = ThreadLock()

    def get_or_open(self, path: Path) -> IO[str]:
        """Return an open append-mode handle for *path*, creating one if needed.

        LRU-evicts the least-recently-used handle when the pool exceeds
        *max_size*, closing the evicted handle.

        Raises:
            DatabaseError: If the file cannot be opened after invalidating
                stale handles and retrying (e.g. EMFILE, permission denied).
        """
        key = str(path)
        with self._lock:
            if key in self._handles:
                handle = self._handles[key]
                if not handle.closed:
                    self._handles.move_to_end(key)
                    return handle
                # Stale handle — remove and reopen
                del self._handles[key]

            try:
                handle = path.open("a", encoding="utf-8", buffering=1)
            except OSError as exc:
                # First attempt failed — close all pooled handles to free file
                # descriptors, then retry once.
                log.warning(
                    "FileHandlePool: OSError opening %s (%s), "
                    "evicting all %d pooled handles and retrying",
                    path,
                    exc,
                    len(self._handles),
                )
                self._evict_all_locked()
                try:
                    handle = path.open("a", encoding="utf-8", buffering=1)
                except OSError as retry_exc:
                    raise DatabaseError(
                        f"Failed to open message file after handle eviction: {retry_exc}",
                        suggestion="Check file descriptor limits (ulimit -n) "
                        "and filesystem permissions.",
                        error_code=ErrorCode.DB_WRITE_FAILED,
                        path=str(path),
                        os_error=str(retry_exc),
                    ) from retry_exc

            self._handles[key] = handle

            # Evict LRU entries over capacity
            while len(self._handles) > self._max_size:
                _, evicted = self._handles.popitem(last=False)
                self._close_handle(evicted)

            return handle

    def _evict_all_locked(self) -> None:
        """Close and remove all pooled handles.  Caller must hold ``_lock``."""
        for handle in self._handles.values():
            self._close_handle(handle)
        self._handles.clear()

    def invalidate(self, path: Path) -> None:
        """Close and remove the handle for *path* (e.g. after file repair)."""
        key = str(path)
        with self._lock:
            handle = self._handles.pop(key, None)
            if handle is not None:
                self._close_handle(handle)

    def close_all(self) -> None:
        """Close every pooled handle.  Called during ``Database.close()``."""
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: IO[str]) -> None:
        try:
            if not handle.closed:
                handle.close()
        except Exception:
            log_noncritical(
                NonCriticalCategory.CONNECTION_CLEANUP,
                "Failed to close file handle during pool shutdown",
                logger=log,
            )


# Backward-compatible alias so existing imports keep working.
_FileHandlePool = FileHandlePool


class ReadHandlePool:
    """Bounded LRU pool of read-mode file handles for message JSONL files.

    Handles are opened in read-text mode (``"r"``) with default buffering.
    Callers use :meth:`get_reader` to obtain a handle, then seek/read as
    needed.  The handle is returned to the pool for reuse — callers should
    **not** close it.

    Staleness detection: read handles cache a ``st_size`` at open time.
    ``get_reader`` checks the current file size against the cached value; if
    the file has grown or shrunk, the handle is reopened so the OS reflects
    the new file length.  This avoids returning stale data after appends.

    Thread-safe via ``ThreadLock`` — same rationale as :class:`FileHandlePool`.
    """

    __slots__ = ("_handles", "_lock", "_max_size", "_st_sizes")

    def __init__(self, max_size: int = MAX_READ_FILE_HANDLES) -> None:
        self._max_size = max_size
        self._handles: OrderedDict[str, IO[str]] = OrderedDict()
        # Track the st_size at the time each handle was opened so we can
        # detect file growth and reopen stale handles.
        self._st_sizes: dict[str, int] = {}
        self._lock = ThreadLock()

    def get_reader(self, path: Path) -> IO[str]:
        """Return a read-mode handle for *path*, opening or reopening as needed.

        If a pooled handle exists but the file has changed size since it was
        opened (due to appends or compression), the stale handle is closed and
        a fresh one is opened.

        LRU-evicts the least-recently-used handle when the pool exceeds
        *max_size*.

        Raises:
            DatabaseError: If the file cannot be opened after evicting all
                pooled handles (e.g. EMFILE, permission denied).
        """
        key = str(path)

        with self._lock:
            if key in self._handles:
                handle = self._handles[key]
                if not handle.closed:
                    # Check if the file has changed size since open — if so,
                    # reopen so we see the latest content (handles opened in
                    # "r" mode may not reflect appends on all platforms).
                    try:
                        current_size = os.stat(handle.fileno()).st_size
                    except OSError:
                        current_size = -1
                    cached_size = self._st_sizes.get(key, -1)

                    if current_size == cached_size:
                        self._handles.move_to_end(key)
                        handle.seek(0)
                        return handle

                    # File changed — close stale handle and reopen below
                    self._close_handle(handle)
                    del self._handles[key]
                    self._st_sizes.pop(key, None)
                else:
                    # Stale closed handle — remove
                    del self._handles[key]
                    self._st_sizes.pop(key, None)

            try:
                st = path.stat()
                handle = path.open("r", encoding="utf-8")
            except OSError as exc:
                log.warning(
                    "ReadHandlePool: OSError opening %s (%s), "
                    "evicting all %d pooled handles and retrying",
                    path,
                    exc,
                    len(self._handles),
                )
                self._evict_all_locked()
                try:
                    st = path.stat()
                    handle = path.open("r", encoding="utf-8")
                except OSError as retry_exc:
                    raise DatabaseError(
                        f"Failed to open message file for reading after "
                        f"handle eviction: {retry_exc}",
                        suggestion="Check file descriptor limits (ulimit -n) "
                        "and filesystem permissions.",
                        error_code=ErrorCode.DB_READ_FAILED,
                        path=str(path),
                        os_error=str(retry_exc),
                    ) from retry_exc

            self._handles[key] = handle
            self._st_sizes[key] = st.st_size

            # Evict LRU entries over capacity
            while len(self._handles) > self._max_size:
                evict_key, evicted = self._handles.popitem(last=False)
                self._st_sizes.pop(evict_key, None)
                self._close_handle(evicted)

            return handle

    def invalidate(self, path: Path) -> None:
        """Close and remove the read handle for *path* (e.g. after file repair)."""
        key = str(path)
        with self._lock:
            handle = self._handles.pop(key, None)
            self._st_sizes.pop(key, None)
            if handle is not None:
                self._close_handle(handle)

    def _evict_all_locked(self) -> None:
        """Close and remove all pooled handles.  Caller must hold ``_lock``."""
        for handle in self._handles.values():
            self._close_handle(handle)
        self._handles.clear()
        self._st_sizes.clear()

    def close_all(self) -> None:
        """Close every pooled handle.  Called during ``Database.close()``."""
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
            self._st_sizes.clear()
        for handle in handles:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: IO[str]) -> None:
        try:
            if not handle.closed:
                handle.close()
        except Exception:
            log_noncritical(
                NonCriticalCategory.CONNECTION_CLEANUP,
                "Failed to close read file handle during pool shutdown",
                logger=log,
            )
