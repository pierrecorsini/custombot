"""
src/db/file_pool.py — Bounded LRU pool of append-mode file handles.

Prevents OS file-descriptor exhaustion under extreme concurrency by reusing
open file handles across writes instead of open/close per operation.

Thread-safe via ``threading.Lock`` because file I/O runs inside
``asyncio.to_thread()`` workers (no event loop available in those threads).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import IO

from src.constants import MAX_FILE_HANDLES
from src.exceptions import DatabaseError, ErrorCode

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
        self._lock = threading.Lock()

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
                    path, exc, len(self._handles),
                )
                self._evict_all_locked()
                try:
                    handle = path.open("a", encoding="utf-8", buffering=1)
                except OSError as retry_exc:
                    raise DatabaseError(
                        f"Failed to open message file after handle eviction: "
                        f"{retry_exc}",
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
            pass  # Best-effort close during shutdown


# Backward-compatible alias so existing imports keep working.
_FileHandlePool = FileHandlePool
