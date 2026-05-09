"""
src/core/skill_task_manager.py — Bounded lifecycle for skill background tasks.

Skills that spawn long-running coroutines (e.g. periodic web checks,
file watchers) register them here.  The manager enforces wall-clock
limits and provides a single ``cancel_all()`` entry point for shutdown.

Usage::

    manager = SkillTaskManager()
    task = asyncio.create_task(my_coroutine())
    manager.register("web_monitor", task, max_duration_seconds=600)

    # On shutdown:
    await manager.cancel_all()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_MAX_DURATION = 300  # 5 minutes


@dataclass(slots=True)
class _TrackedTask:
    """Internal bookkeeping for a registered background task."""

    task_id: str
    task: asyncio.Task[Any]
    start_time: float
    max_duration: float
    skill_name: str = ""


class SkillTaskManager:
    """Track and enforce wall-clock limits on skill background tasks.

    A single background checker task periodically scans registered tasks
    and cancels any that exceed their ``max_duration``.  The checker
    starts lazily on first registration and stops when all tasks complete
    or ``cancel_all()`` is called.
    """

    def __init__(self, check_interval: float = 5.0) -> None:
        self._tasks: dict[str, _TrackedTask] = {}
        self._check_interval = check_interval
        self._checker: asyncio.Task[None] | None = None

    def register(
        self,
        task_id: str,
        task: asyncio.Task[Any],
        max_duration_seconds: float = _DEFAULT_MAX_DURATION,
        *,
        skill_name: str = "",
    ) -> None:
        """Register a background task for bounded execution.

        Args:
            task_id: Unique identifier (deduped — existing ID is replaced).
            task: The ``asyncio.Task`` to track.
            max_duration_seconds: Wall-clock limit before cancellation.
            skill_name: Optional skill name for logging clarity.
        """
        now = time.monotonic()
        existing = self._tasks.get(task_id)
        if existing is not None:
            log.debug("Replacing tracked task %s", task_id)
            existing.task.cancel()

        self._tasks[task_id] = _TrackedTask(
            task_id=task_id,
            task=task,
            start_time=now,
            max_duration=max_duration_seconds,
            skill_name=skill_name,
        )
        task.add_done_callback(lambda _: self._on_task_done(task_id))
        self._ensure_checker()

    def get_active_tasks(self) -> list[dict[str, Any]]:
        """Snapshot of currently running tasks for monitoring."""
        now = time.monotonic()
        result: list[dict[str, Any]] = []
        for tracked in self._tasks.values():
            if tracked.task.done():
                continue
            elapsed = now - tracked.start_time
            result.append({
                "task_id": tracked.task_id,
                "skill_name": tracked.skill_name,
                "elapsed_seconds": round(elapsed, 1),
                "max_duration": tracked.max_duration,
                "remaining_seconds": round(max(0, tracked.max_duration - elapsed), 1),
            })
        return result

    async def cancel_all(self) -> None:
        """Cancel all running tasks (for shutdown)."""
        for tracked in self._tasks.values():
            if not tracked.task.done():
                tracked.task.cancel()

        if self._tasks:
            await asyncio.gather(
                *(t.task for t in self._tasks.values()),
                return_exceptions=True,
            )

        self._tasks.clear()
        self._stop_checker()
        log.info("All skill background tasks cancelled")

    # ── internal ──────────────────────────────────────────────────────────

    def _on_task_done(self, task_id: str) -> None:
        """Remove completed task from tracking."""
        self._tasks.pop(task_id, None)
        if not self._tasks:
            self._stop_checker()

    def _ensure_checker(self) -> None:
        """Start the background expiry checker if not already running."""
        if self._checker is not None and not self._checker.done():
            return
        self._checker = asyncio.create_task(self._check_loop(), name="skill-task-checker")

    def _stop_checker(self) -> None:
        if self._checker is not None and not self._checker.done():
            self._checker.cancel()
        self._checker = None

    async def _check_loop(self) -> None:
        """Periodically cancel tasks that exceed their max duration."""
        try:
            while self._tasks:
                await asyncio.sleep(self._check_interval)
                self._expire_overdue()
        except asyncio.CancelledError:
            pass

    def _expire_overdue(self) -> None:
        now = time.monotonic()
        expired: list[str] = []
        for tracked in self._tasks.values():
            if tracked.task.done():
                continue
            if now - tracked.start_time > tracked.max_duration:
                tracked.task.cancel()
                expired.append(tracked.task_id)

        for tid in expired:
            label = tid
            tracked = self._tasks.get(tid)
            if tracked and tracked.skill_name:
                label = f"{tracked.skill_name}:{tid}"
            log.warning("Cancelled skill task %s (exceeded max duration)", label)
