"""
background_service.py — Base class for long-running background services.

Provides BaseBackgroundService which standardizes the lifecycle of
components that spawn ``asyncio.create_task`` loops:

  - ``_running`` flag and ``_task`` handle managed automatically
  - ``start()`` / ``stop()`` with idempotent guard and graceful cancellation
  - ``is_running`` property for introspection

Subclasses implement ``_run_loop()`` to define the service's behavior.

Usage::

    from src.utils.background_service import BaseBackgroundService

    class MyMonitor(BaseBackgroundService):
        _service_name = "My monitor"

        def __init__(self, interval: float = 60.0) -> None:
            super().__init__()
            self._interval = interval

        def start_monitoring(self) -> None:
            self.start()

        async def _run_loop(self) -> None:
            while self._running:
                await self._check()
                await asyncio.sleep(self._interval)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class BaseBackgroundService(ABC):
    """Base class for background services with managed asyncio tasks.

    Provides standardized start/stop lifecycle management. Subclasses
    implement ``_run_loop()`` to define the service's periodic behavior.

    The ``start()`` method is idempotent — calling it while already running
    is a no-op with a warning log. The ``stop()`` method gracefully cancels
    the background task and waits for it to finish.

    Subclasses should set ``_service_name`` for descriptive logging.
    """

    _service_name: str = ""

    def __init__(self) -> None:
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the service is actively running."""
        return self._running

    def start(self) -> None:
        """Start the background service.

        Creates an asyncio task running ``_run_loop()``. Idempotent —
        calling while already running logs a warning and returns.
        """
        name = self._service_name or type(self).__name__
        if self._running:
            log.warning("%s already running", name)
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the background service gracefully.

        Sets the ``_running`` flag to False and cancels the background
        task, then awaits its completion.
        """
        name = self._service_name or type(self).__name__
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("%s stopped", name)

    @abstractmethod
    async def _run_loop(self) -> None:
        """Implement the service's main background loop.

        Called once by ``start()``. The loop should check ``self._running``
        to know when to exit. When ``stop()`` is called, the task is
        cancelled, so ``CancelledError`` will be raised at the next
        ``await`` point.
        """
        ...


__all__ = ["BaseBackgroundService"]
