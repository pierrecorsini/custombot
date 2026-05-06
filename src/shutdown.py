"""
src/shutdown.py — Graceful shutdown manager.

Coordinates shutdown across all bot components:
- Stops accepting new messages
- Waits for in-flight operations to complete
- Provides signal handler registration
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from src.constants import DEFAULT_SHUTDOWN_TIMEOUT, SHUTDOWN_LOG_INTERVAL
from src.utils.locking import AsyncLock


class GracefulShutdown:
    """
    Manages graceful shutdown on SIGTERM/SIGINT.

    Coordinates cleanup across all components:
    - Stops accepting new messages
    - Waits for in-flight operations to complete
    - Closes database connections
    - Stops bridge subprocess
    - Closes HTTP clients

    Works on both Unix (SIGTERM, SIGINT) and Windows (SIGINT, SIGBREAK).
    """

    def __init__(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        self._timeout = timeout
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight_count = 0
        self._in_flight_ops: dict[int, str] = {}
        self._next_op_id = 0
        # Lazy-initialised via AsyncLock — see src.utils.locking policy
        self._in_flight_lock = AsyncLock()
        self._accepting_messages = True
        self._log = logging.getLogger(__name__)

    @property
    def is_shutting_down(self) -> bool:
        """Check if shutdown has been initiated."""
        return self._shutdown_event.is_set()

    @property
    def accepting_messages(self) -> bool:
        """Check if new messages should be accepted."""
        return self._accepting_messages and not self.is_shutting_down

    def request_shutdown(self) -> None:
        """Signal that shutdown has been requested.

        Thread-safe: uses ``loop.call_soon_threadsafe`` to set the
        asyncio Event from signal handlers that run outside the event loop.
        """
        self._accepting_messages = False
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
        else:
            self._shutdown_event.set()
        self._log.info("Shutdown requested - stopping new message acceptance")

    async def enter_operation(self, description: str = "unknown") -> int | None:
        """
        Register start of an in-flight operation.

        Returns an int op_id on success, or None if shutdown is in progress.
        """
        if self.is_shutting_down:
            return None
        async with self._in_flight_lock:
            if self.is_shutting_down:
                return None
            self._in_flight_count += 1
            op_id = self._next_op_id
            self._next_op_id += 1
            self._in_flight_ops[op_id] = description
            return op_id

    async def exit_operation(self, op_id: int | None = None) -> None:
        """Register completion of an in-flight operation."""
        async with self._in_flight_lock:
            self._in_flight_count = max(0, self._in_flight_count - 1)
            if op_id is not None:
                self._in_flight_ops.pop(op_id, None)

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()

    async def wait_for_in_flight(self) -> bool:
        """
        Wait for all in-flight operations to complete.

        Returns True if all completed, False if timeout reached.
        """
        start = time.time()
        last_log = start
        logged_initial = False

        while True:
            async with self._in_flight_lock:
                count = self._in_flight_count
                ops_snapshot = dict(self._in_flight_ops)

            if count == 0:
                self._log.info("All in-flight operations completed")
                return True

            if not logged_initial:
                self._log.info(
                    "Waiting for %d in-flight operations to complete (timeout: %.1fs)",
                    count,
                    self._timeout,
                )
                for op_id, desc in ops_snapshot.items():
                    self._log.info("  [%d] %s", op_id, desc)
                logged_initial = True

            elapsed = time.time() - start
            if elapsed >= self._timeout:
                self._log.warning(
                    "Shutdown timeout reached with %d operations still in-flight:",
                    count,
                )
                for op_id, desc in ops_snapshot.items():
                    self._log.warning("  [%d] %s", op_id, desc)
                return False

            # Log progress periodically
            if time.time() - last_log >= SHUTDOWN_LOG_INTERVAL:
                remaining = self._timeout - elapsed
                if len(ops_snapshot) != count:
                    self._log.info(
                        "Waiting for %d in-flight operations (%.1fs remaining)",
                        count,
                        remaining,
                    )
                else:
                    self._log.info(
                        "Waiting for %d in-flight operations (%.1fs remaining):",
                        count,
                        remaining,
                    )
                    for op_id, desc in ops_snapshot.items():
                        self._log.info("  [%d] %s", op_id, desc)
                last_log = time.time()

            await asyncio.sleep(0.1)

    def register_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Register signal handlers for graceful shutdown.

        Handles SIGINT (Ctrl+C) and SIGTERM on Unix.
        Handles SIGINT and SIGBREAK on Windows.
        """
        self._loop = loop
        shutdown_manager = self

        def handle_signal(signum: int, frame) -> None:
            signal_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
            shutdown_manager._log.info(
                "Received signal %s, initiating graceful shutdown", signal_name
            )
            shutdown_manager.request_shutdown()

        signals_to_handle = []

        if sys.platform == "win32":
            signals_to_handle = [signal.SIGINT, signal.SIGBREAK]
        else:
            signals_to_handle = [signal.SIGTERM, signal.SIGINT]

        for sig in signals_to_handle:
            try:
                signal.signal(sig, handle_signal)
                shutdown_manager._log.debug(
                    "Registered handler for %s",
                    sig.name if hasattr(sig, "name") else sig,
                )
            except (ValueError, OSError) as exc:
                shutdown_manager._log.debug(
                    "Could not register handler for signal %s: %s", sig, exc
                )
