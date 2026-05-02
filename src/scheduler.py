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
import heapq
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from src.constants import (
    DEFAULT_SCHEDULER_TASK_TIMEOUT,
    MAX_SCHEDULED_PROMPT_LENGTH,
    SCHEDULER_HMAC_SECRET_ENV,
    SCHEDULER_HMAC_SIG_EXT,
    SCHEDULER_LOOP_BACKOFF_CAP,
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_MAX_SLEEP_SECONDS,
    SCHEDULER_MIN_SLEEP_SECONDS,
    SCHEDULER_RETRY_INITIAL_DELAY,
)
from src.db.db import _validate_chat_id
from src.security.signing import (
    get_scheduler_secret,
    read_signature_file,
    sign_payload,
    verify_payload,
    write_signature_file,
)
from src.utils import JSONDecodeError
from src.utils.background_service import BaseBackgroundService
from src.utils.path import sanitize_path_component
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


class TaskScheduler(BaseBackgroundService):
    """Background scheduler that triggers bot actions on schedule."""

    # Cache UTC offset — refreshes every hour (only changes on DST transition)
    _UTC_OFFSET_CACHE_SECONDS = 3600
    _service_name = "Scheduler"

    def __init__(self) -> None:
        super().__init__()
        # chat_id -> list of task dicts
        self._tasks: dict[str, list[dict[str, Any]]] = {}
        # callback: (chat_id, prompt_text, prompt_hmac | None) -> str | None response
        self._on_trigger: Callable[[str, str, str | None], Awaitable[str | None]] | None = None
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
        # Consecutive-failure backoff for the main loop tick
        self._consecutive_failures: int = 0
        # Unified dedup service — set via set_dedup_service()
        self._dedup: DeduplicationService | None = None

    def set_dedup_service(self, dedup: DeduplicationService) -> None:
        """Set the unified dedup service for outbound message dedup."""
        self._dedup = dedup

    def configure(
        self,
        workspace: Path,
        on_trigger: Callable[[str, str, str | None], Awaitable[str | None]] | None = None,
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

    def set_on_trigger(
        self,
        callback: Callable[[str, str, str | None], Awaitable[str | None]],
    ) -> None:
        """Set the callback for triggering scheduled task processing."""
        self._on_trigger = callback

    def start(self) -> None:
        super().start()
        log.info("Scheduler started")

    async def stop(self) -> None:
        await super().stop()
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

        if len(prompt) > MAX_SCHEDULED_PROMPT_LENGTH:
            raise ValueError(
                f"Task 'prompt' exceeds maximum length of "
                f"{MAX_SCHEDULED_PROMPT_LENGTH} characters "
                f"({len(prompt)} chars)"
            )

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
                invalid values, or if chat_id contains unsafe characters.
        """
        _validate_chat_id(chat_id)
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

    def _resolve_tasks_path(self, chat_id: str) -> Path | None:
        """Build and validate the tasks.json path for a chat.

        Sanitizes ``chat_id`` and verifies the resolved path stays within
        the workspace root to prevent path-traversal attacks.

        Returns:
            Validated ``Path`` or ``None`` if workspace is unset / path is
            outside the workspace tree.
        """
        if not self._workspace:
            return None
        safe_id = sanitize_path_component(chat_id)
        dest = (self._workspace / safe_id / SCHEDULER_DIR / TASKS_FILE).resolve()
        workspace_root = self._workspace.resolve()
        if not dest.is_relative_to(workspace_root):
            log.warning(
                "Scheduler path traversal blocked for chat_id=%r (resolved: %s)",
                chat_id,
                dest,
            )
            return None
        return dest

    async def _persist(self, chat_id: str) -> None:
        """Persist tasks to disk via thread pool to avoid blocking the event loop."""
        dest = self._resolve_tasks_path(chat_id)
        if dest is None:
            return
        data = self._tasks.get(chat_id, [])
        await asyncio.to_thread(self._write_tasks_file, dest, data)

    @staticmethod
    def _write_tasks_file(path: Path, data: list[dict[str, Any]]) -> None:
        """Synchronous helper: mkdir + serialize + write (runs in thread pool).

        When ``SCHEDULER_HMAC_SECRET`` is configured, an HMAC-SHA256
        signature is written to a sidecar ``.hmac`` file alongside the
        tasks data.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2)
        path.write_text(content)

        secret = get_scheduler_secret()
        if secret is not None:
            signature = sign_payload(secret, content.encode("utf-8"))
            write_signature_file(path.with_suffix(path.suffix + SCHEDULER_HMAC_SIG_EXT), signature)

    async def _load(self, chat_id: str) -> None:
        """Load tasks for a chat from disk via thread pool to avoid blocking.

        When ``SCHEDULER_HMAC_SECRET`` is configured, the HMAC signature is
        verified before parsing.  If verification fails, tasks are **not**
        loaded and a security audit warning is logged.
        """
        dest = self._resolve_tasks_path(chat_id)
        if dest is None:
            return
        try:
            raw = await asyncio.to_thread(self._read_tasks_file, dest)
            if raw is not None:
                # Verify HMAC when a secret is configured.
                secret = get_scheduler_secret()
                if secret is not None:
                    sig_path = dest.with_suffix(dest.suffix + SCHEDULER_HMAC_SIG_EXT)
                    stored_sig = await asyncio.to_thread(read_signature_file, sig_path)
                    if stored_sig is None:
                        log.error(
                            "Scheduler HMAC verification failed for %s: "
                            "signature file missing or unreadable",
                            chat_id,
                        )
                        log.warning(
                            "AUDIT: scheduler_integrity_fail chat_id=%s "
                            "reason=missing_signature_file",
                            chat_id,
                        )
                        return
                    if not verify_payload(secret, raw.encode("utf-8"), stored_sig):
                        log.error(
                            "Scheduler HMAC verification failed for %s: "
                            "signature mismatch — possible tampering",
                            chat_id,
                        )
                        log.warning(
                            "AUDIT: scheduler_integrity_fail chat_id=%s "
                            "reason=signature_mismatch",
                            chat_id,
                        )
                        return
                self._tasks[chat_id] = json.loads(raw)
        except (JSONDecodeError, OSError) as exc:
            log.error("Failed to load scheduler tasks for %s: %s", chat_id, exc)

    @staticmethod
    def _read_tasks_file(path: Path) -> str | None:
        """Synchronous helper: check exists + read (runs in thread pool)."""
        if path.exists():
            return path.read_text()
        return None

    async def load_all(self) -> None:
        """Load tasks for all chats from workspace concurrently.

        Scans workspace directories for task files and loads them in
        parallel using asyncio.gather for faster startup with many chats.
        """
        if not self._workspace or not self._workspace.exists():
            return

        # Collect valid chat directories first (synchronous scan is fast)
        valid_dirs: list[str] = []
        for chat_dir in self._workspace.iterdir():
            if not chat_dir.is_dir():
                continue
            task_file = chat_dir / SCHEDULER_DIR / TASKS_FILE
            if not task_file.exists():
                continue
            try:
                _validate_chat_id(chat_dir.name)
            except ValueError:
                log.warning(
                    "Skipping scheduler tasks for invalid chat_id=%r",
                    chat_dir.name,
                )
                continue
            valid_dirs.append(chat_dir.name)

        if not valid_dirs:
            return

        # Load all chat tasks concurrently
        await asyncio.gather(*(self._load(chat_id) for chat_id in valid_dirs))

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

    def _is_due(self, task: dict[str, Any], now: datetime | None = None) -> bool:
        """Check if a task is due to run now.

        Uses a tolerance window (1.5 × TICK_SECONDS) instead of an exact
        minute match so that tasks are not silently skipped when the
        scheduler tick overshoots the target minute due to backoff or
        long-running task execution.
        """
        if not task.get("enabled", True):
            return False

        schedule = task.get("schedule", {})
        stype = schedule.get("type", "")
        last_run = task.get("last_run")

        now = now or _now()
        local_offset = self._get_cached_utc_offset()
        due_window = TICK_SECONDS * 1.5

        if stype == "daily":
            target_hour = schedule.get("hour", 0)
            target_min = schedule.get("minute", 0)
            local_total_min = target_hour * 60 + target_min
            utc_total_min = (local_total_min - int(local_offset * 60)) % (24 * 60)
            utc_hour = utc_total_min // 60
            utc_minute = utc_total_min % 60
            target = now.replace(hour=utc_hour, minute=utc_minute, second=0, microsecond=0)
            if abs((now - target).total_seconds()) < due_window:
                if not last_run or not _same_day(last_run, now.isoformat()):
                    return True

        elif stype == "interval":
            interval_sec = schedule.get("seconds", 3600)
            if not last_run:
                return True
            last_run_epoch = task.get("last_run_epoch")
            if last_run_epoch is not None:
                elapsed = now.timestamp() - last_run_epoch
            else:
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
            target = now.replace(hour=utc_hour, minute=utc_minute, second=0, microsecond=0)
            if now.weekday() in weekdays and abs((now - target).total_seconds()) < due_window:
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

        The prompt is signed with HMAC-SHA256 (when a secret is configured)
        and the signature is passed to the callback for integrity verification.
        """
        # Sign the prompt so the callback can verify it wasn't tampered with.
        prompt_hmac: str | None = None
        secret = get_scheduler_secret()
        if secret:
            prompt_hmac = sign_payload(secret, prompt.encode("utf-8"))

        delay = SCHEDULER_RETRY_INITIAL_DELAY
        for attempt in range(SCHEDULER_MAX_RETRIES + 1):
            try:
                return await self._on_trigger(chat_id, prompt, prompt_hmac)  # type: ignore[misc]
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
        """Execute a due task by invoking the bot.

        All reads from the shared ``task`` dict are snapshot at entry (before
        any ``await``) so that concurrent ``asyncio.gather`` executions never
        observe partially-mutated state.  Mutations (``last_result``,
        ``last_run``, ``last_run_epoch``) are written back in a single
        synchronous block after execution completes.

        Note: the caller (_loop) is responsible for persisting updated task
        state to disk after all tasks in a tick have completed.  This allows
        batching — one write per unique chat_id instead of one per task.
        """
        # ── snapshot all reads from the shared dict (no awaits above) ──
        task_id: str = task.get("task_id", "unknown")
        prompt = task.get("prompt", "")
        compare = task.get("compare", False)
        last_result = task.get("last_result")
        task_label = task.get("label", task_id)

        log.info(
            "━━━ ⏰ RUNNING SCHEDULED TASK '%s' ━━━  [chat: %s]",
            task_label,
            chat_id,
        )

        if compare and last_result:
            prompt = (
                f"{prompt}\n\n"
                f"[PREVIOUS RESULT]:\n{last_result}\n\n"
                f"Compare with the current result and report any changes."
            )

        try:
            if not self._on_trigger:
                log.warning("No on_trigger callback — skipping task %s", task_id)
                return

            try:
                result = await asyncio.wait_for(
                    self._trigger_with_retry(chat_id, prompt, task_id),
                    timeout=DEFAULT_SCHEDULER_TASK_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Scheduled task {task_id} timed out after "
                    f"{DEFAULT_SCHEDULER_TASK_TIMEOUT}s"
                ) from None

            # Write results back to shared dict in one synchronous block
            now_dt = _now()
            task["last_result"] = (result or "")[:2000]
            task["last_run"] = now_dt.isoformat()
            task["last_run_epoch"] = now_dt.timestamp()

            # Deliver result — transport layer handles reconnection
            if not result:
                log.warning(
                    "Scheduled task %s produced empty result — nothing to send",
                    task_id,
                )
            elif not self._on_send:
                log.warning(
                    "No on_send callback — result for task %s not delivered",
                    task_id,
                )
            else:
                formatted = f"⏰ **{task_label}**\n\n{result}"
                # Outbound dedup: skip sending if the same content was
                # already delivered to this chat within the TTL window.
                # Uses the unified DeduplicationService.
                if self._dedup and self._dedup.check_and_record_outbound(chat_id, formatted):
                    log.info(
                        "Scheduled task %s response suppressed (duplicate outbound) for chat %s",
                        task_id,
                        chat_id,
                    )
                else:
                    await self._deliver_with_retry(chat_id, formatted, task_id)

            log.info(
                "━━━ ✔ SCHEDULED TASK '%s' DONE ━━━  [chat: %s]",
                task_label,
                chat_id,
            )
            self._success_count += 1
            self._recent_executions.append({
                "task_id": task_id,
                "chat_id": chat_id,
                "status": "success",
                "timestamp": _now().isoformat(),
                "error_summary": None,
            })
        except Exception as exc:
            self._failure_count += 1
            self._recent_executions.append({
                "task_id": task_id,
                "chat_id": chat_id,
                "status": "failure",
                "timestamp": _now().isoformat(),
                "error_summary": f"{type(exc).__name__}: {exc}"[:200],
            })
            log.error("Error executing task %s: %s", task_id, exc, exc_info=True)

    async def _deliver_with_retry(
        self, chat_id: str, formatted: str, task_id: str,
    ) -> None:
        """Deliver a scheduled task result with retry on transient send failures.

        The channel layer already retries once with reconnection, but usync
        timeouts can persist across multiple attempts.  This adds a second
        layer of retry with a short delay to handle transient WhatsApp
        device-resolution failures.
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                await self._on_send(chat_id, formatted)
                log.info(
                    "Delivered scheduled task %s to chat %s",
                    task_id,
                    chat_id,
                )
                return
            except Exception as exc:
                if attempt >= max_attempts - 1:
                    raise
                if not is_transient_error(exc):
                    raise
                from src.utils.retry import calculate_delay_with_jitter
                delay = calculate_delay_with_jitter(15.0 * (attempt + 1))
                log.warning(
                    "Send attempt %d/%d failed for task %s: %s — retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    task_id,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    # ── main loop ──────────────────────────────────────────────────────────

    def _time_to_next_due(self) -> float | None:
        """Compute seconds until the next task is due.

        Uses a heap to find the minimum time-to-due in O(n) build +
        O(1) peek instead of the previous O(n) full scan approach.
        The heap is rebuilt each call (lightweight for typical task
        counts < 100).
        """
        if not self._tasks:
            return None

        now = _now()
        local_offset = self._get_cached_utc_offset()
        candidates: list[float] = []

        for tasks in self._tasks.values():
            for task in tasks:
                if not task.get("enabled", True):
                    continue

                schedule = task.get("schedule", {})
                stype = schedule.get("type", "")
                last_run = task.get("last_run")

                if stype == "interval":
                    interval_sec = schedule.get("seconds", 3600)
                    if not last_run:
                        candidate = 0.0
                    else:
                        last_run_epoch = task.get("last_run_epoch")
                        if last_run_epoch is not None:
                            elapsed = now.timestamp() - last_run_epoch
                        else:
                            elapsed = (now - datetime.fromisoformat(last_run)).total_seconds()
                        candidate = max(0.0, interval_sec - elapsed)

                elif stype in ("daily", "cron"):
                    target_hour = schedule.get("hour", 0)
                    target_min = schedule.get("minute", 0)
                    local_total_min = target_hour * 60 + target_min
                    utc_total_min = (local_total_min - int(local_offset * 60)) % (24 * 60)
                    utc_hour = utc_total_min // 60
                    utc_minute = utc_total_min % 60

                    target_today = now.replace(
                        hour=utc_hour, minute=utc_minute,
                        second=0, microsecond=0,
                    )
                    if target_today > now:
                        candidate = (target_today - now).total_seconds()
                    else:
                        candidate = (target_today + timedelta(days=1) - now).total_seconds()

                    if stype == "cron":
                        weekdays = schedule.get("weekdays", list(range(7)))
                        next_occ = target_today if target_today > now else target_today + timedelta(days=1)
                        for _ in range(8):
                            if next_occ.weekday() in weekdays:
                                break
                            next_occ += timedelta(days=1)
                        candidate = max(0.0, (next_occ - now).total_seconds())

                else:
                    continue

                candidates.append(candidate)

        if not candidates:
            return None
        heapq.heapify(candidates)
        return candidates[0]

    def _compute_adaptive_sleep(self) -> float:
        """Return the sleep duration for the next loop iteration.

        - No tasks registered  → ``SCHEDULER_MAX_SLEEP_SECONDS`` (5 min)
        - Tasks exist, imminent → ``max(SCHEDULER_MIN_SLEEP, time_to_next)``
        - Tasks exist, distant  → ``min(TICK_SECONDS, time_to_next)``
        """
        time_to_next = self._time_to_next_due()

        if time_to_next is None:
            # No tasks at all — sleep long
            return SCHEDULER_MAX_SLEEP_SECONDS

        if time_to_next <= TICK_SECONDS:
            # Task is imminent or overdue — clamp to minimum to avoid spinning
            return max(SCHEDULER_MIN_SLEEP_SECONDS, time_to_next)

        # Next task is further away than TICK_SECONDS — cap at TICK_SECONDS
        return TICK_SECONDS

    async def _run_loop(self) -> None:
        """Background loop: check tasks with adaptive sleep intervals.

        Instead of a fixed ``TICK_SECONDS`` sleep, computes the time until
        the next task is due and sleeps accordingly.  This reduces CPU
        wakeups from ~2880/day to a fraction when few tasks are scheduled.

        When consecutive ticks encounter failures (task exceptions or outer
        loop errors), the sleep interval is multiplied by ``2 ** failures``,
        capped at ``SCHEDULER_LOOP_BACKOFF_CAP``.  The counter resets on the
        first successful tick that executes at least one task.

        All due tasks are collected first (snapshot), then executed
        concurrently so that long-running tasks don't block co-scheduled ones.
        After execution, dirty chat_ids are persisted in batch — one file
        write per unique chat_id instead of one per task.
        """
        while self._running:
            tick_had_failure = False
            tick_had_tasks = False

            try:
                now = _now()
                due_tasks: list[tuple[str, dict[str, Any]]] = []
                for chat_id, tasks in list(self._tasks.items()):
                    for task in tasks:
                        if self._is_due(task, now):
                            due_tasks.append((chat_id, task))

                if due_tasks:
                    tick_had_tasks = True
                    results = await asyncio.gather(
                        *[self._execute_task(cid, t) for cid, t in due_tasks],
                        return_exceptions=True,
                    )
                    for i, result in enumerate(results):
                        if isinstance(result, BaseException) and not isinstance(result, Exception):
                            cid, t = due_tasks[i]
                            log.critical(
                                "Scheduled task %s raised BaseException: %s",
                                t.get("task_id"),
                                result,
                                exc_info=(type(result), result, result.__traceback__),
                            )
                            raise result
                        if isinstance(result, Exception):
                            tick_had_failure = True
                            cid, t = due_tasks[i]
                            log.error(
                                "Scheduled task %s raised: %s",
                                t.get("task_id"),
                                result,
                                exc_info=(type(result), result, result.__traceback__),
                            )

                    # Batch persist: one write per unique dirty chat_id
                    dirty_chat_ids = {cid for cid, _ in due_tasks}
                    await asyncio.gather(
                        *(self._persist(cid) for cid in dirty_chat_ids)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tick_had_failure = True
                log.error("Scheduler loop error: %s", exc, exc_info=True)

            # Update consecutive-failure tracking and apply backoff
            if tick_had_failure:
                self._consecutive_failures += 1
            elif tick_had_tasks:
                if self._consecutive_failures > 0:
                    log.info(
                        "Scheduler recovered after %d consecutive failures",
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0

            sleep_duration = self._compute_adaptive_sleep()
            if self._consecutive_failures > 0:
                sleep_duration = min(
                    sleep_duration * 2 ** self._consecutive_failures,
                    SCHEDULER_LOOP_BACKOFF_CAP,
                )
                log.info(
                    "Scheduler tick failed (consecutive: %d), backoff sleep %.1fs",
                    self._consecutive_failures,
                    sleep_duration,
                )
            await asyncio.sleep(sleep_duration)


def _same_day(iso_a: str, iso_b: str) -> bool:
    """Check if two ISO timestamps are on the same calendar day (UTC)."""
    try:
        da = datetime.fromisoformat(iso_a)
        db = datetime.fromisoformat(iso_b)
        return da.date() == db.date()
    except (ValueError, TypeError):
        return False
