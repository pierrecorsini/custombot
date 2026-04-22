"""
src/scheduler.py — Async task scheduler engine.

Runs as an asyncio background task. Checks scheduled jobs periodically,
and when a job is due, injects a synthetic message into the bot's
handle_message pipeline so the LLM can use all existing skills.

Schedule types:
  - daily:   {hour, minute} — runs once per day at that time
  - interval: {seconds}     — runs every N seconds
  - cron:    {hour, minute, weekdays} — runs on specific days

Persistence:
  workspace/.scheduler/tasks.json — one array of task objects per chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from src.constants import (
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_RETRY_INITIAL_DELAY,
)
from src.utils.retry import is_transient_error

if TYPE_CHECKING:
    from src.core.dedup import DeduplicationService

log = logging.getLogger(__name__)

SCHEDULER_DIR = ".scheduler"
TASKS_FILE = "tasks.json"
TICK_SECONDS = 30  # check every 30s


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_offset_hours() -> float:
    """Local UTC offset in hours (e.g. +1 for CET).

    .astimezone() converts a naive datetime to the local timezone,
    which makes .utcoffset() work reliably on all platforms (including Windows).
    """
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset else 0


class TaskScheduler:
    """Background scheduler that triggers bot actions on schedule."""

    # Cache UTC offset — refreshes every hour (only changes on DST transition)
    _UTC_OFFSET_CACHE_SECONDS = 3600

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        # chat_id -> list of task dicts
        self._tasks: dict[str, list[dict[str, Any]]] = {}
        # callback: (chat_id, prompt_text) -> str response
        self._on_trigger: Callable[[str, str], Awaitable[str]] | None = None
        # send callback: (chat_id, text) -> None
        self._on_send: Callable[[str, str], Awaitable[None]] | None = None
        # workspace root
        self._workspace: Path | None = None
        # Cached UTC offset
        self._cached_utc_offset: float | None = None
        self._utc_offset_updated_at: float = 0.0
        # Execution tracking for health checks
        self._failure_count: int = 0
        self._success_count: int = 0
        self._recent_executions: deque[dict[str, Any]] = deque(maxlen=10)
        # Unified dedup service — set via set_dedup_service()
        self._dedup: DeduplicationService | None = None

    def set_dedup_service(self, dedup: DeduplicationService) -> None:
        """Set the unified dedup service for outbound message dedup."""
        self._dedup = dedup

    def configure(
        self,
        workspace: Path,
        on_trigger: Callable[[str, str], Awaitable[str]] | None = None,
        on_send: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._workspace = workspace
        if on_trigger is not None:
            self._on_trigger = on_trigger
        if on_send is not None:
            self._on_send = on_send

    def set_on_send(self, callback: Callable[[str, str], Awaitable[None]]) -> None:
        """Set the callback for delivering scheduled task results."""
        self._on_send = callback

    def set_on_trigger(self, callback: Callable[[str, str], Awaitable[str | None]]) -> None:
        """Set the callback for triggering scheduled task processing."""
        self._on_trigger = callback

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Scheduler started")

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Scheduler stopped")

    def get_status(self) -> dict[str, Any]:
        """Return scheduler status for health checks."""
        total_tasks = sum(len(tasks) for tasks in self._tasks.values())
        enabled_tasks = sum(
            1 for tasks in self._tasks.values() for t in tasks if t.get("enabled", True)
        )
        chats_with_tasks = len(self._tasks)
        return {
            "running": self._running,
            "total_tasks": total_tasks,
            "enabled_tasks": enabled_tasks,
            "chats_with_tasks": chats_with_tasks,
            "success_count": self._success_count,
            "failure_count": self._failure_count,
            "recent_executions": list(self._recent_executions),
        }

    # ── task validation ────────────────────────────────────────────────────

    _VALID_SCHEDULE_TYPES = frozenset({"daily", "interval", "cron"})

    def _validate_task(self, task: dict[str, Any]) -> None:
        """Validate a task dict before persistence. Raises ``ValueError``."""
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Task 'prompt' must be a non-empty string")

        schedule = task.get("schedule")
        if not isinstance(schedule, dict):
            raise ValueError("Task 'schedule' must be a dict")

        stype = schedule.get("type")
        if stype not in self._VALID_SCHEDULE_TYPES:
            raise ValueError(
                f"Task schedule 'type' must be one of {sorted(self._VALID_SCHEDULE_TYPES)}, "
                f"got {stype!r}"
            )

        if stype == "daily":
            if "hour" not in schedule or "minute" not in schedule:
                raise ValueError("Daily schedule requires 'hour' and 'minute' fields")
        elif stype == "interval":
            seconds = schedule.get("seconds")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValueError("Interval schedule requires a positive 'seconds' field")
        elif stype == "cron":
            if "hour" not in schedule or "minute" not in schedule:
                raise ValueError("Cron schedule requires 'hour' and 'minute' fields")

    # ── task CRUD ──────────────────────────────────────────────────────────

    def _prepare_task(self, chat_id: str, task: dict[str, Any]) -> str:
        """Prepare a scheduled task with ID and metadata. Returns the task_id."""
        tasks = self._tasks.setdefault(chat_id, [])
        existing_ids = {t["task_id"] for t in tasks if "task_id" in t}
        counter = len(tasks) + 1
        task_id = f"task_{counter:03d}"
        while task_id in existing_ids:
            counter += 1
            task_id = f"task_{counter:03d}"
        task.setdefault("task_id", task_id)
        task["task_id"] = task_id
        task["created"] = _now().isoformat()
        task["last_run"] = None
        task["last_result"] = None
        task["enabled"] = True
        tasks.append(task)
        return task_id

    async def add_task(self, chat_id: str, task: dict[str, Any]) -> str:
        """Add a scheduled task with async persistence. Returns the task_id.

        Raises:
            ValueError: If the task dict is missing required fields or has
                invalid values.
        """
        self._validate_task(task)
        task_id = self._prepare_task(chat_id, task)
        await self._persist(chat_id)
        return task_id

    async def remove_task_async(self, chat_id: str, task_id: str) -> bool:
        """Remove a task by ID with async persistence. Returns True if found."""
        tasks = self._tasks.get(chat_id, [])
        before = len(tasks)
        self._tasks[chat_id] = [t for t in tasks if t["task_id"] != task_id]
        if len(self._tasks[chat_id]) < before:
            await self._persist(chat_id)
            return True
        return False

    def list_tasks(self, chat_id: str) -> list[dict[str, Any]]:
        return self._tasks.get(chat_id, [])

    # ── persistence ────────────────────────────────────────────────────────

    async def _persist(self, chat_id: str) -> None:
        """Persist tasks to disk via thread pool to avoid blocking the event loop."""
        if not self._workspace:
            return
        data = self._tasks.get(chat_id, [])
        dest = self._workspace / chat_id / SCHEDULER_DIR / TASKS_FILE
        await asyncio.to_thread(self._write_tasks_file, dest, data)

    @staticmethod
    def _write_tasks_file(path: Path, data: list[dict[str, Any]]) -> None:
        """Synchronous helper: mkdir + serialize + write (runs in thread pool)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    async def _load(self, chat_id: str) -> None:
        """Load tasks for a chat from disk via thread pool to avoid blocking."""
        if not self._workspace:
            return
        path = self._workspace / chat_id / SCHEDULER_DIR / TASKS_FILE
        try:
            raw = await asyncio.to_thread(self._read_tasks_file, path)
            if raw is not None:
                self._tasks[chat_id] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to load scheduler tasks for %s: %s", chat_id, exc)

    @staticmethod
    def _read_tasks_file(path: Path) -> str | None:
        """Synchronous helper: check exists + read (runs in thread pool)."""
        if path.exists():
            return path.read_text()
        return None

    async def load_all(self) -> None:
        """Load tasks for all chats from workspace asynchronously."""
        if not self._workspace or not self._workspace.exists():
            return
        for chat_dir in self._workspace.iterdir():
            if chat_dir.is_dir():
                task_file = chat_dir / SCHEDULER_DIR / TASKS_FILE
                if task_file.exists():
                    await self._load(chat_dir.name)

    # ── scheduling logic ───────────────────────────────────────────────────

    def _get_cached_utc_offset(self) -> float:
        """Get UTC offset with hourly caching to avoid repeated syscalls."""
        now = time.monotonic()
        if (
            self._cached_utc_offset is None
            or (now - self._utc_offset_updated_at) > self._UTC_OFFSET_CACHE_SECONDS
        ):
            self._cached_utc_offset = _utc_offset_hours()
            self._utc_offset_updated_at = now
        return self._cached_utc_offset

    def _is_due(self, task: dict[str, Any]) -> bool:
        """Check if a task is due to run now."""
        if not task.get("enabled", True):
            return False

        schedule = task.get("schedule", {})
        stype = schedule.get("type", "")
        last_run = task.get("last_run")

        now = _now()
        local_offset = self._get_cached_utc_offset()

        if stype == "daily":
            target_hour = schedule.get("hour", 0)
            target_min = schedule.get("minute", 0)
            # Convert target local time to UTC using minutes to handle fractional offsets
            local_total_min = target_hour * 60 + target_min
            utc_total_min = (local_total_min - int(local_offset * 60)) % (24 * 60)
            utc_hour = utc_total_min // 60
            utc_minute = utc_total_min % 60
            if now.hour == utc_hour and now.minute == utc_minute:
                if not last_run or not _same_day(last_run, now.isoformat()):
                    return True

        elif stype == "interval":
            interval_sec = schedule.get("seconds", 3600)
            if not last_run:
                return True
            elapsed = (now - datetime.fromisoformat(last_run)).total_seconds()
            if elapsed >= interval_sec:
                return True

        elif stype == "cron":
            target_hour = schedule.get("hour", 0)
            target_min = schedule.get("minute", 0)
            weekdays = schedule.get("weekdays", list(range(7)))
            local_total_min = target_hour * 60 + target_min
            utc_total_min = (local_total_min - int(local_offset * 60)) % (24 * 60)
            utc_hour = utc_total_min // 60
            utc_minute = utc_total_min % 60
            # Python weekday: Mon=0..Sun=6
            if now.weekday() in weekdays and now.hour == utc_hour and now.minute == utc_minute:
                if not last_run or not _same_day(last_run, now.isoformat()):
                    return True

        return False

    async def _trigger_with_retry(
        self, chat_id: str, prompt: str, task_id: str,
    ) -> str:
        """Trigger the bot callback with retry for transient failures.

        Uses exponential backoff with jitter. Only retries on transient
        errors (timeouts, connection failures, rate limits). Permanent
        failures (authentication, invalid prompt) fail immediately.
        """
        delay = SCHEDULER_RETRY_INITIAL_DELAY
        for attempt in range(SCHEDULER_MAX_RETRIES + 1):
            try:
                return await self._on_trigger(chat_id, prompt)  # type: ignore[misc]
            except Exception as exc:
                if not is_transient_error(exc):
                    raise
                if attempt >= SCHEDULER_MAX_RETRIES:
                    log.warning(
                        "Retry exhausted for task %s after %d attempts",
                        task_id,
                        SCHEDULER_MAX_RETRIES + 1,
                    )
                    raise
                from src.utils.retry import calculate_delay_with_jitter

                actual_delay = calculate_delay_with_jitter(delay)
                log.info(
                    "Retrying task %s, attempt %d/%d after %.1fs (error: %s)",
                    task_id,
                    attempt + 1,
                    SCHEDULER_MAX_RETRIES,
                    actual_delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(actual_delay)
                delay *= 2
        raise RuntimeError("Unreachable")  # pragma: no cover

    async def _execute_task(self, chat_id: str, task: dict[str, Any]) -> None:
        """Execute a due task by invoking the bot."""
        prompt = task.get("prompt", "")
        compare = task.get("compare", False)
        last_result = task.get("last_result")

        if compare and last_result:
            prompt = (
                f"{prompt}\n\n"
                f"[PREVIOUS RESULT]:\n{last_result}\n\n"
                f"Compare with the current result and report any changes."
            )

        try:
            if not self._on_trigger:
                log.warning("No on_trigger callback — skipping task %s", task.get("task_id"))
                return

            result = await self._trigger_with_retry(chat_id, prompt, task.get("task_id", ""))
            task["last_result"] = (result or "")[:2000]
            task["last_run"] = _now().isoformat()
            await self._persist(chat_id)

            # Deliver result — transport layer handles reconnection
            if not result:
                log.warning(
                    "Scheduled task %s produced empty result — nothing to send",
                    task["task_id"],
                )
            elif not self._on_send:
                log.warning(
                    "No on_send callback — result for task %s not delivered",
                    task["task_id"],
                )
            else:
                formatted = f"⏰ **{task.get('label', 'Tâche planifiée')}**\n\n{result}"
                # Outbound dedup: skip sending if the same content was
                # already delivered to this chat within the TTL window.
                # Uses the unified DeduplicationService.
                if self._dedup and self._dedup.is_outbound_duplicate(chat_id, formatted):
                    log.info(
                        "Scheduled task %s response suppressed (duplicate outbound) for chat %s",
                        task["task_id"],
                        chat_id,
                    )
                else:
                    await self._on_send(chat_id, formatted)
                    # Record successful delivery for future dedup checks
                    if self._dedup:
                        self._dedup.record_outbound(chat_id, formatted)
                    log.info(
                        "Delivered scheduled task %s to chat %s",
                        task["task_id"],
                        chat_id,
                    )

            log.info("Executed scheduled task %s for chat %s", task["task_id"], chat_id)
            self._success_count += 1
            self._recent_executions.append({
                "task_id": task["task_id"],
                "chat_id": chat_id,
                "status": "success",
                "timestamp": _now().isoformat(),
                "error_summary": None,
            })
        except Exception as exc:
            self._failure_count += 1
            self._recent_executions.append({
                "task_id": task.get("task_id", "unknown"),
                "chat_id": chat_id,
                "status": "failure",
                "timestamp": _now().isoformat(),
                "error_summary": f"{type(exc).__name__}: {exc}"[:200],
            })
            log.error("Error executing task %s: %s", task.get("task_id"), exc, exc_info=True)

    # ── main loop ──────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Background loop: check tasks every TICK_SECONDS.

        All due tasks are collected first (snapshot), then executed
        concurrently so that long-running tasks don't block co-scheduled ones.
        """
        while self._running:
            try:
                due_tasks: list[tuple[str, dict[str, Any]]] = []
                for chat_id, tasks in list(self._tasks.items()):
                    for task in tasks:
                        if self._is_due(task):
                            due_tasks.append((chat_id, task))

                if due_tasks:
                    results = await asyncio.gather(
                        *[self._execute_task(cid, t) for cid, t in due_tasks],
                        return_exceptions=True,
                    )
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            cid, t = due_tasks[i]
                            log.error(
                                "Scheduled task %s raised: %s",
                                t.get("task_id"),
                                result,
                                exc_info=(type(result), result, result.__traceback__),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Scheduler loop error: %s", exc, exc_info=True)

            await asyncio.sleep(TICK_SECONDS)


def _same_day(iso_a: str, iso_b: str) -> bool:
    """Check if two ISO timestamps are on the same calendar day (UTC)."""
    try:
        da = datetime.fromisoformat(iso_a)
        db = datetime.fromisoformat(iso_b)
        return da.date() == db.date()
    except (ValueError, TypeError):
        return False
