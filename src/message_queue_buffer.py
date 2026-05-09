"""
message_queue_buffer.py — Write-buffer management and background flush for MessageQueue.

Encapsulates the swap-buffers pattern for non-blocking disk writes: the write
buffer is detached under the lock (O(1) pointer swap), then flushed to disk
*without* holding the lock so that ``enqueue``/``complete`` calls are never
blocked by an in-progress fsync.

Extracted from ``message_queue.py`` to isolate buffer and flush concerns,
following the same decomposition pattern as ``message_queue_persistence.py``
(persistence I/O) and ``scheduler/`` (engine / persistence / cron split).

Lock model:
    - ``append_to_queue``, ``append_completion``, ``maybe_flush_buffer``,
      ``flush_write_buffer``, and ``persist_pending`` are called while the
      caller already holds the ``AsyncLock``.  They do **not** acquire it.
    - ``_flush_loop`` acquires the lock internally for the buffer-swap step,
      then releases it before the (potentially slow) fsync.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.core.errors import NonCriticalCategory, log_noncritical
from src.utils import json_dumps

if TYPE_CHECKING:
    from src.message_queue import QueuedMessage
    from src.message_queue_persistence import QueuePersistence
    from src.utils.locking import AsyncLock

log = logging.getLogger(__name__)

__all__ = ["FlushManager"]


class FlushManager:
    """Manages the write buffer and background flush loop for :class:`MessageQueue`.

    Responsibilities:
      * Buffering message lines and completion markers for batched writes.
      * Threshold-based and timer-based buffer flushing.
      * The swap-buffers background coroutine that drains the buffer on a timer.
      * Full-file atomic persistence (``persist_pending``).

    Args:
        lock: The ``AsyncLock`` shared with the owning ``MessageQueue``.
        persistence: The ``QueuePersistence`` instance that owns the raw buffer
            and file I/O.
        pending: Reference to the ``MessageQueue._pending`` dict.  Shared by
            reference so mutations by the owner are visible here.
    """

    def __init__(
        self,
        lock: AsyncLock,
        persistence: QueuePersistence,
        pending: Dict[str, QueuedMessage],
    ) -> None:
        self._lock = lock
        self._persistence = persistence
        self._pending = pending
        self._flush_task: Optional[asyncio.Task[None]] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background flush-loop coroutine."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Cancel the background flush loop and await cleanup."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

    # ── buffer operations (caller must hold lock) ──────────────────────────

    async def append_to_queue(self, msg: QueuedMessage) -> None:
        """Buffer a message line for batched write via persistence layer."""
        try:
            line = json_dumps(msg.to_dict(), ensure_ascii=False) + "\n"
            self._persistence.buffer_line(line)
            await self.maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append to queue file: %s", exc)
            raise

    async def append_completion(self, message_id: str) -> None:
        """Buffer a completion marker for batched write via persistence layer."""
        try:
            entry = (
                json_dumps(
                    {
                        "message_id": message_id,
                        "status": "completed",
                        "completed_at": time.time(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._persistence.buffer_line(entry)
            await self.maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append completion to queue file: %s", exc)
            # Fall back to full persist on error
            self._persistence.clear_buffer()
            await self.persist_pending()

    async def maybe_flush_buffer(self) -> None:
        """Flush write buffer if a threshold is met."""
        if self._persistence.should_flush():
            await self.flush_write_buffer()

    async def flush_write_buffer(self) -> None:
        """Flush all buffered lines to disk via persistence layer.

        On I/O failure (e.g. disk full), the persistence layer re-buffers
        the failed lines.  We log a warning and suppress the error so that
        ``enqueue`` / ``complete`` callers don't lose messages — the lines
        will be retried on the next flush cycle.
        """
        if not self._persistence.write_buffer:
            return
        try:
            t0 = time.perf_counter()
            await asyncio.to_thread(self._persistence.flush_buffer)
            self._report_flush_duration(time.perf_counter() - t0)
        except Exception as exc:
            log.warning(
                "Flush failed, %d lines re-buffered for retry: %s",
                len(self._persistence.write_buffer),
                exc,
            )

    async def persist_pending(self) -> None:
        """Atomically persist all pending messages to queue file."""
        try:
            await asyncio.to_thread(self._persistence.persist_messages, self._pending.values())
            log.debug("Persisted %d pending messages", len(self._pending))
        except Exception as exc:
            log.error("Failed to persist queue: %s", exc)
            try:
                await asyncio.to_thread(self._persistence.cleanup_temp_file)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.QUEUE_OPERATION,
                    "Failed to clean up temp queue file",
                    logger=log,
                )
            raise

    # ── metrics reporting ─────────────────────────────────────────────────

    @staticmethod
    def _report_flush_duration(duration_seconds: float) -> None:
        """Report flush latency to the global PerformanceMetrics collector."""
        try:
            from src.monitoring.performance import get_metrics_collector

            get_metrics_collector().track_flush_duration(duration_seconds)
        except Exception:
            # Metrics collection must never interfere with flush path.
            pass

    # ── background flush loop ─────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Background coroutine that drains the write buffer on a timer.

        Ensures buffered writes are persisted within the fsync interval even
        when no new enqueue calls arrive (e.g. during idle periods).

        Uses the swap-buffers pattern: atomically detaches the write buffer
        under the lock (fast pointer swap), then flushes the detached buffer
        to disk *without* holding the lock.  This prevents enqueue/complete
        calls from blocking behind an fsync syscall under burst traffic.
        """
        try:
            while True:
                await asyncio.sleep(self._persistence.fsync_interval)
                if self._persistence.write_buffer:
                    # Swap: detach the buffer under lock (O(1) pointer swap)
                    async with self._lock:
                        lines = self._persistence.swap_buffer()
                    # Flush the detached buffer without the lock — enqueue/
                    # complete can proceed against the fresh buffer immediately.
                    if lines:
                        try:
                            t0 = time.perf_counter()
                            await asyncio.to_thread(self._persistence.flush_lines, lines)
                            self._report_flush_duration(time.perf_counter() - t0)
                        except Exception as exc:
                            log.warning(
                                "Background flush failed, re-buffering %d lines: %s",
                                len(lines),
                                exc,
                            )
                            async with self._lock:
                                self._persistence.rebuffer_lines(lines)
        except asyncio.CancelledError:
            # Expected during stop() — suppress gracefully.
            return
