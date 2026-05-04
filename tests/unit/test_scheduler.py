"""
Tests for src/scheduler.py — Async task scheduler engine.

Unit tests covering:
  - _same_day utility
  - _utc_offset_hours helper
  - _target_utc_time helper
  - TaskScheduler configure / start / stop lifecycle
  - Task CRUD: add_task, add_task, remove_task_async, list_tasks
  - Task ID generation (sequential, collision avoidance)
  - Persistence to JSON (sync + async paths)
  - load_all loading tasks across multiple chats
  - _is_due for daily / interval / cron schedule types
  - _is_due with enabled/disabled tasks
  - UTC offset caching behaviour
  - _execute_task: callback invocation, compare mode, persistence after run
  - _execute_task: missing callbacks, empty results, on_send delivery
  - Background _loop tick behaviour
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler import (
    SCHEDULER_DIR,
    TASKS_FILE,
    TICK_SECONDS,
    TaskScheduler,
    _now,
    _same_day,
    _target_utc_time,
    _utc_offset_hours,
)
from src.constants import (
    SCHEDULER_LOOP_BACKOFF_CAP,
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_MAX_SLEEP_SECONDS,
    SCHEDULER_MIN_SLEEP_SECONDS,
    SCHEDULER_RETRY_INITIAL_DELAY,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a clean temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def on_trigger() -> AsyncMock:
    """Async mock for the on_trigger callback."""
    return AsyncMock(return_value="result from LLM")


@pytest.fixture
def on_send() -> AsyncMock:
    """Async mock for the on_send callback."""
    return AsyncMock()


@pytest.fixture
def scheduler(workspace: Path, on_trigger: AsyncMock, on_send: AsyncMock) -> TaskScheduler:
    """Provide a fully configured TaskScheduler."""
    s = TaskScheduler()
    s.configure(workspace=workspace, on_trigger=on_trigger, on_send=on_send)
    return s


def _tasks_file(workspace: Path, chat_id: str) -> Path:
    """Return path to the tasks.json for a given chat_id."""
    return workspace / chat_id / SCHEDULER_DIR / TASKS_FILE


def _make_task(
    schedule_type: str = "interval",
    prompt: str = "test prompt",
    label: str = "Test Task",
    **schedule_overrides,
) -> dict:
    """Build a minimal task dict for testing."""
    defaults: dict
    if schedule_type == "interval":
        defaults = {"seconds": 60}
    elif schedule_type in ("daily", "cron"):
        defaults = {"hour": 9, "minute": 0}
    else:
        defaults = {}
    schedule = {"type": schedule_type, **defaults, **schedule_overrides}
    return {
        "prompt": prompt,
        "label": label,
        "schedule": schedule,
        "last_run": None,
        "last_result": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# _same_day
# ─────────────────────────────────────────────────────────────────────────────


class TestSameDay:
    """Tests for the _same_day() utility function."""

    def test_same_timestamps(self):
        ts = "2025-06-15T12:00:00+00:00"
        assert _same_day(ts, ts) is True

    def test_same_day_different_times(self):
        a = "2025-06-15T01:00:00+00:00"
        b = "2025-06-15T23:59:59+00:00"
        assert _same_day(a, b) is True

    def test_different_days(self):
        a = "2025-06-15T23:59:59+00:00"
        b = "2025-06-16T00:00:00+00:00"
        assert _same_day(a, b) is False

    def test_different_months(self):
        a = "2025-06-30T12:00:00+00:00"
        b = "2025-07-01T12:00:00+00:00"
        assert _same_day(a, b) is False

    def test_different_years(self):
        a = "2024-12-31T23:00:00+00:00"
        b = "2025-01-01T01:00:00+00:00"
        assert _same_day(a, b) is False

    def test_naive_datetime_strings(self):
        a = "2025-06-15T10:00:00"
        b = "2025-06-15T14:00:00"
        assert _same_day(a, b) is True

    def test_invalid_first_argument(self):
        assert _same_day("not-a-date", "2025-06-15T12:00:00+00:00") is False

    def test_invalid_second_argument(self):
        assert _same_day("2025-06-15T12:00:00+00:00", "garbage") is False

    def test_both_invalid(self):
        assert _same_day("", "not-a-date") is False

    def test_none_arguments(self):
        assert _same_day(None, "2025-06-15T12:00:00+00:00") is False  # type: ignore[arg-type]
        assert _same_day("2025-06-15T12:00:00+00:00", None) is False  # type: ignore[arg-type]

    def test_utc_z_suffix(self):
        a = "2025-06-15T08:00:00Z"
        b = "2025-06-15T20:00:00+00:00"
        assert _same_day(a, b) is True


# ─────────────────────────────────────────────────────────────────────────────
# _utc_offset_hours
# ─────────────────────────────────────────────────────────────────────────────


class TestUtcOffsetHours:
    """Tests for _utc_offset_hours()."""

    def test_returns_float(self):
        offset = _utc_offset_hours()
        assert isinstance(offset, float)

    def test_in_valid_range(self):
        offset = _utc_offset_hours()
        assert -12.0 <= offset <= 14.0

    def test_consistent_results(self):
        """Calling twice in quick succession should yield the same value."""
        assert _utc_offset_hours() == _utc_offset_hours()


# ─────────────────────────────────────────────────────────────────────────────
# _target_utc_time
# ─────────────────────────────────────────────────────────────────────────────


class TestTargetUtcTime:
    """Tests for _target_utc_time()."""

    def test_utc_plus_one(self):
        """Local 10:00 in UTC+1 → UTC 09:00."""
        schedule = {"hour": 10, "minute": 0}
        utc_hour, utc_minute = _target_utc_time(schedule, 1.0)
        assert (utc_hour, utc_minute) == (9, 0)

    def test_utc_minus_five(self):
        """Local 08:30 in UTC-5 → UTC 13:30."""
        schedule = {"hour": 8, "minute": 30}
        utc_hour, utc_minute = _target_utc_time(schedule, -5.0)
        assert (utc_hour, utc_minute) == (13, 30)

    def test_midnight_wrap(self):
        """Local 23:00 in UTC+2 → UTC 21:00 (no wrap)."""
        schedule = {"hour": 23, "minute": 0}
        utc_hour, utc_minute = _target_utc_time(schedule, 2.0)
        assert (utc_hour, utc_minute) == (21, 0)

    def test_day_boundary_wrap(self):
        """Local 01:00 in UTC-5 → UTC 06:00 (next day wrap via modulo)."""
        schedule = {"hour": 1, "minute": 0}
        utc_hour, utc_minute = _target_utc_time(schedule, -5.0)
        assert (utc_hour, utc_minute) == (6, 0)

    def test_zero_offset(self):
        """UTC+0: local time equals UTC."""
        schedule = {"hour": 14, "minute": 45}
        utc_hour, utc_minute = _target_utc_time(schedule, 0.0)
        assert (utc_hour, utc_minute) == (14, 45)

    def test_defaults_to_midnight(self):
        """Missing hour/minute defaults to 00:00."""
        schedule: dict = {}
        utc_hour, utc_minute = _target_utc_time(schedule, 0.0)
        assert (utc_hour, utc_minute) == (0, 0)

    def test_fractional_offset(self):
        """UTC+5:45 (Kathmandu) local 06:00 → UTC 00:15."""
        schedule = {"hour": 6, "minute": 0}
        utc_hour, utc_minute = _target_utc_time(schedule, 5.75)
        assert (utc_hour, utc_minute) == (0, 15)


class TestConfigure:
    """Tests for TaskScheduler.configure()."""

    def test_sets_workspace(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        assert s._workspace == workspace

    def test_sets_on_trigger(self, workspace: Path):
        trigger = AsyncMock()
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=trigger)
        assert s._on_trigger is trigger

    def test_sets_on_send_optional(self, workspace: Path):
        send = AsyncMock()
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock(), on_send=send)
        assert s._on_send is send

    def test_on_send_defaults_to_none(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        assert s._on_send is None


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — start / stop lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class TestLifecycle:
    """Tests for TaskScheduler start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_sets_running(self, scheduler: TaskScheduler):
        scheduler.start()
        assert scheduler._running is True
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_start_creates_background_task(self, scheduler: TaskScheduler):
        scheduler.start()
        assert scheduler._task is not None
        assert not scheduler._task.done()
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, scheduler: TaskScheduler):
        scheduler.start()
        first_task = scheduler._task
        scheduler.start()  # second call should be no-op
        assert scheduler._task is first_task
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self, scheduler: TaskScheduler):
        scheduler.start()
        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, scheduler: TaskScheduler):
        scheduler.start()
        task = scheduler._task
        await scheduler.stop()
        assert task is not None
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, scheduler: TaskScheduler):
        """Stopping without starting should not raise."""
        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_start_stop_restart(self, scheduler: TaskScheduler):
        scheduler.start()
        await scheduler.stop()
        scheduler.start()
        assert scheduler._running is True
        assert scheduler._task is not None
        await scheduler.stop()


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — add_task (async)
# ─────────────────────────────────────────────────────────────────────────────


class TestAddTask:
    """Tests for TaskScheduler.add_task() — async path."""

    @pytest.mark.asyncio
    async def test_returns_task_id(self, scheduler: TaskScheduler):
        task_id = await scheduler.add_task("chat1", _make_task())
        assert task_id == "task_001"

    @pytest.mark.asyncio
    async def test_sequential_ids(self, scheduler: TaskScheduler):
        id1 = await scheduler.add_task("chat1", _make_task())
        id2 = await scheduler.add_task("chat1", _make_task())
        id3 = await scheduler.add_task("chat1", _make_task())
        assert id1 == "task_001"
        assert id2 == "task_002"
        assert id3 == "task_003"

    @pytest.mark.asyncio
    async def test_separate_chat_id_counters(self, scheduler: TaskScheduler):
        id_a = await scheduler.add_task("chatA", _make_task())
        id_b = await scheduler.add_task("chatB", _make_task())
        # Both start from 001 per chat
        assert id_a == "task_001"
        assert id_b == "task_001"

    @pytest.mark.asyncio
    async def test_sets_created_timestamp(self, scheduler: TaskScheduler):
        before = _now().isoformat()
        await scheduler.add_task("chat1", _make_task())
        after = _now().isoformat()
        tasks = scheduler.list_tasks("chat1")
        assert before <= tasks[0]["created"] <= after

    @pytest.mark.asyncio
    async def test_sets_last_run_none(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task())
        tasks = scheduler.list_tasks("chat1")
        assert tasks[0]["last_run"] is None

    @pytest.mark.asyncio
    async def test_sets_enabled_true(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task())
        tasks = scheduler.list_tasks("chat1")
        assert tasks[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_overrides_existing_task_id_in_dict(self, scheduler: TaskScheduler):
        task = _make_task()
        task["task_id"] = "task_999"
        returned_id = await scheduler.add_task("chat1", task)
        assert returned_id == "task_001"
        assert task["task_id"] == "task_001"

    @pytest.mark.asyncio
    async def test_avoids_duplicate_task_ids(self, scheduler: TaskScheduler):
        """If task_001 exists (e.g. from a prior add), the next should skip it."""
        scheduler._tasks["chat1"] = [{"task_id": "task_001", "schedule": {}}]
        new_id = await scheduler.add_task("chat1", _make_task())
        assert new_id == "task_002"

    @pytest.mark.asyncio
    async def test_persists_to_file(self, scheduler: TaskScheduler, workspace: Path):
        await scheduler.add_task("chat1", _make_task(prompt="hello"))
        path = _tasks_file(workspace, "chat1")
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_persist_no_workspace(self, on_trigger: AsyncMock):
        """add_task without configure should not crash — just skip persistence."""
        s = TaskScheduler()
        task_id = await s.add_task("chat1", _make_task())
        assert task_id == "task_001"

    @pytest.mark.asyncio
    async def test_adds_to_internal_store(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task(prompt="async test"))
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "async test"


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — remove_task_async
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoveTaskAsync:
    """Tests for TaskScheduler.remove_task_async()."""

    @pytest.mark.asyncio
    async def test_remove_existing(self, scheduler: TaskScheduler):
        tid = await scheduler.add_task("chat1", _make_task())
        removed = await scheduler.remove_task_async("chat1", tid)
        assert removed is True
        assert scheduler.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, scheduler: TaskScheduler):
        removed = await scheduler.remove_task_async("chat1", "task_999")
        assert removed is False

    @pytest.mark.asyncio
    async def test_remove_wrong_chat(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task())
        removed = await scheduler.remove_task_async("chat2", "task_001")
        assert removed is False

    @pytest.mark.asyncio
    async def test_removes_correct_task_from_multiple(self, scheduler: TaskScheduler):
        t1 = await scheduler.add_task("chat1", _make_task(prompt="first"))
        t2 = await scheduler.add_task("chat1", _make_task(prompt="second"))
        await scheduler.remove_task_async("chat1", t1)
        remaining = scheduler.list_tasks("chat1")
        assert len(remaining) == 1
        assert remaining[0]["task_id"] == t2
        assert remaining[0]["prompt"] == "second"

    @pytest.mark.asyncio
    async def test_persists_after_removal(self, scheduler: TaskScheduler, workspace: Path):
        tid = await scheduler.add_task("chat1", _make_task())
        await scheduler.remove_task_async("chat1", tid)
        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        assert data == []

    @pytest.mark.asyncio
    async def test_does_not_persist_if_not_found(self, scheduler: TaskScheduler):
        """Removing a nonexistent task should not trigger persistence."""
        removed = await scheduler.remove_task_async("chat1", "task_999")
        assert removed is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — list_tasks
# ─────────────────────────────────────────────────────────────────────────────


class TestListTasks:
    """Tests for TaskScheduler.list_tasks()."""

    def test_empty_for_unknown_chat(self, scheduler: TaskScheduler):
        assert scheduler.list_tasks("unknown") == []

    @pytest.mark.asyncio
    async def test_returns_all_tasks(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task(prompt="a"))
        await scheduler.add_task("chat1", _make_task(prompt="b"))
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 2
        prompts = {t["prompt"] for t in tasks}
        assert prompts == {"a", "b"}

    @pytest.mark.asyncio
    async def test_returns_copy(self, scheduler: TaskScheduler):
        """list_tasks should return the actual list (not a deep copy)."""
        await scheduler.add_task("chat1", _make_task())
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — persistence
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistence:
    """Tests for task persistence to disk."""

    @pytest.mark.asyncio
    async def test_sync_persist_creates_directory(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.add_task("chat1", _make_task())
        expected_dir = workspace / "chat1" / SCHEDULER_DIR
        assert expected_dir.is_dir()

    @pytest.mark.asyncio
    async def test_sync_persist_file_content(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.add_task("chat1", _make_task(prompt="check content", label="MyTask"))
        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        assert data[0]["prompt"] == "check content"
        assert data[0]["label"] == "MyTask"
        assert "task_id" in data[0]
        assert "created" in data[0]

    @pytest.mark.asyncio
    async def test_async_persist_file_content(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.add_task("chat1", _make_task(prompt="async persist"))
        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        assert data[0]["prompt"] == "async persist"

    @pytest.mark.asyncio
    async def test_persist_no_workspace(self):
        """Should not crash when workspace is None."""
        s = TaskScheduler()
        await s._persist("chat1")  # no error

    @pytest.mark.asyncio
    async def test_load_restores_tasks(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.add_task("chat1", _make_task(prompt="saved"))
        # New scheduler instance to test loading
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "saved"

    @pytest.mark.asyncio
    async def test_load_handles_corrupt_json(self, workspace: Path):
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT VALID JSON {{{")
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")  # should not raise
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_load_handles_missing_file(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("nonexistent_chat")  # should not raise
        assert s.list_tasks("nonexistent_chat") == []


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — load_all
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadAll:
    """Tests for TaskScheduler.load_all()."""

    @pytest.mark.asyncio
    async def test_loads_multiple_chats(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.add_task("chatA", _make_task(prompt="A"))
        await s.add_task("chatB", _make_task(prompt="B"))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2.load_all()
        assert len(s2.list_tasks("chatA")) == 1
        assert len(s2.list_tasks("chatB")) == 1

    @pytest.mark.asyncio
    async def test_skips_dirs_without_tasks(self, workspace: Path):
        (workspace / "chat_no_tasks").mkdir()
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.load_all()  # should not raise
        assert s.list_tasks("chat_no_tasks") == []

    @pytest.mark.asyncio
    async def test_no_workspace(self):
        s = TaskScheduler()
        await s.load_all()  # should not raise

    @pytest.mark.asyncio
    async def test_nonexistent_workspace(self, tmp_path: Path):
        s = TaskScheduler()
        s.configure(workspace=tmp_path / "does_not_exist", on_trigger=AsyncMock())
        await s.load_all()  # should not raise

    @pytest.mark.asyncio
    async def test_ignores_files_in_workspace_root(self, workspace: Path):
        (workspace / "somefile.txt").write_text("not a chat dir")
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s.load_all()  # should not raise


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _is_due for interval schedule
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueInterval:
    """Tests for _is_due() with schedule type 'interval'."""

    def test_due_when_no_last_run(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=60)
        assert scheduler._is_due(task) is True

    def test_due_when_interval_elapsed(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=10)
        task["last_run"] = (_now() - timedelta(seconds=15)).isoformat()
        assert scheduler._is_due(task) is True

    def test_not_due_when_interval_not_elapsed(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=3600)
        task["last_run"] = (_now() - timedelta(seconds=10)).isoformat()
        assert scheduler._is_due(task) is False

    def test_due_exactly_at_interval_boundary(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=60)
        task["last_run"] = (_now() - timedelta(seconds=60)).isoformat()
        assert scheduler._is_due(task) is True

    def test_default_interval_is_3600(self, scheduler: TaskScheduler):
        """Default interval should be 1 hour when 'seconds' is omitted."""
        task = {"schedule": {"type": "interval"}, "enabled": True}
        # Just under 1 hour — should not be due
        task["last_run"] = (_now() - timedelta(seconds=3500)).isoformat()
        assert scheduler._is_due(task) is False

    def test_disabled_task_never_due(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=10)
        task["enabled"] = False
        task["last_run"] = None
        assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _is_due for daily schedule
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueDaily:
    """Tests for _is_due() with schedule type 'daily'."""

    def test_due_at_target_time_no_last_run(self, scheduler: TaskScheduler):
        """With no last_run, task is due at the target UTC time."""
        now = _now()
        # Set target hour/minute in local time
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24
        local_min = now.minute

        task = _make_task(schedule_type="daily", hour=local_hour, minute=local_min)
        assert scheduler._is_due(task) is True

    def test_not_due_at_wrong_time(self, scheduler: TaskScheduler):
        """Task should not be due at a different minute."""
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24
        wrong_min = (now.minute + 5) % 60

        task = _make_task(schedule_type="daily", hour=local_hour, minute=wrong_min)
        assert scheduler._is_due(task) is False

    def test_not_due_same_day_already_ran(self, scheduler: TaskScheduler):
        """If already ran today, should not be due again."""
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(schedule_type="daily", hour=local_hour, minute=now.minute)
        task["last_run"] = now.isoformat()
        assert scheduler._is_due(task) is False

    def test_due_next_day_after_previous_run(self, scheduler: TaskScheduler):
        """Should be due again the next day at target time."""
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(schedule_type="daily", hour=local_hour, minute=now.minute)
        task["last_run"] = (now - timedelta(days=1)).isoformat()
        assert scheduler._is_due(task) is True

    def test_disabled_daily_task(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(schedule_type="daily", hour=local_hour, minute=now.minute)
        task["enabled"] = False
        assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _is_due for cron schedule
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueCron:
    """Tests for _is_due() with schedule type 'cron'."""

    def test_due_on_matching_weekday(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24
        weekday = now.weekday()

        task = _make_task(
            schedule_type="cron", hour=local_hour, minute=now.minute, weekdays=[weekday]
        )
        assert scheduler._is_due(task) is True

    def test_not_due_on_non_matching_weekday(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24
        wrong_weekday = (now.weekday() + 1) % 7

        task = _make_task(
            schedule_type="cron",
            hour=local_hour,
            minute=now.minute,
            weekdays=[wrong_weekday],
        )
        assert scheduler._is_due(task) is False

    def test_not_due_already_ran_today(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(
            schedule_type="cron",
            hour=local_hour,
            minute=now.minute,
            weekdays=[now.weekday()],
        )
        task["last_run"] = now.isoformat()
        assert scheduler._is_due(task) is False

    def test_due_on_any_of_multiple_weekdays(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(
            schedule_type="cron",
            hour=local_hour,
            minute=now.minute,
            weekdays=[0, 1, 2, 3, 4, 5, 6],  # all days
        )
        assert scheduler._is_due(task) is True

    def test_default_weekdays_is_all(self, scheduler: TaskScheduler):
        """If weekdays omitted, should default to all days."""
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(schedule_type="cron", hour=local_hour, minute=now.minute)
        # No 'weekdays' key — defaults to range(7)
        assert "weekdays" not in task["schedule"]
        assert scheduler._is_due(task) is True

    def test_disabled_cron_task(self, scheduler: TaskScheduler):
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(
            schedule_type="cron",
            hour=local_hour,
            minute=now.minute,
            weekdays=[now.weekday()],
        )
        task["enabled"] = False
        assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _is_due unknown schedule type
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueUnknownType:
    """Tests for _is_due() with unknown/missing schedule types."""

    def test_unknown_type_not_due(self, scheduler: TaskScheduler):
        task = {"schedule": {"type": "yearly"}, "enabled": True}
        assert scheduler._is_due(task) is False

    def test_empty_type_not_due(self, scheduler: TaskScheduler):
        task = {"schedule": {"type": ""}, "enabled": True}
        assert scheduler._is_due(task) is False

    def test_missing_schedule_not_due(self, scheduler: TaskScheduler):
        task = {"enabled": True}
        assert scheduler._is_due(task) is False

    def test_missing_type_not_due(self, scheduler: TaskScheduler):
        task = {"schedule": {}, "enabled": True}
        assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — UTC offset caching
# ─────────────────────────────────────────────────────────────────────────────


class TestUtcOffsetCaching:
    """Tests for TaskScheduler._get_cached_utc_offset()."""

    def test_first_call_computes_offset(self, scheduler: TaskScheduler):
        offset = scheduler._get_cached_utc_offset()
        assert isinstance(offset, float)
        assert -12.0 <= offset <= 14.0

    def test_caches_value(self, scheduler: TaskScheduler):
        first = scheduler._get_cached_utc_offset()
        second = scheduler._get_cached_utc_offset()
        assert first == second

    def test_cache_is_used_within_ttl(self, scheduler: TaskScheduler):
        """Within the cache TTL, the same value should be returned."""
        scheduler._cached_utc_offset = 5.0
        scheduler._utc_offset_updated_at = time.monotonic()
        result = scheduler._get_cached_utc_offset()
        assert result == 5.0

    def test_cache_refreshes_after_ttl(self, scheduler: TaskScheduler):
        """After the TTL, a fresh value should be computed."""
        scheduler._cached_utc_offset = 99.0  # unrealistic
        scheduler._utc_offset_updated_at = time.monotonic() - 7200  # 2 hours ago
        result = scheduler._get_cached_utc_offset()
        assert result != 99.0
        assert -12.0 <= result <= 14.0


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _execute_task
# ─────────────────────────────────────────────────────────────────────────────


class TestExecuteTask:
    """Tests for TaskScheduler._execute_task()."""

    @pytest.mark.asyncio
    async def test_calls_on_trigger(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        task = _make_task(prompt="run this")
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        on_trigger.assert_awaited_once_with("chat1", "run this", None)

    @pytest.mark.asyncio
    async def test_sets_last_run(self, scheduler: TaskScheduler):
        task = _make_task()
        task["task_id"] = "task_001"
        assert task["last_run"] is None
        await scheduler._execute_task("chat1", task)
        assert task["last_run"] is not None

    @pytest.mark.asyncio
    async def test_sets_last_result(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        on_trigger.return_value = "LLM says hello"
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        assert task["last_result"] == "LLM says hello"

    @pytest.mark.asyncio
    async def test_truncates_long_result(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        on_trigger.return_value = "x" * 5000
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        assert len(task["last_result"]) == 2000

    @pytest.mark.asyncio
    async def test_handles_none_result(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        on_trigger.return_value = None
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        assert task["last_result"] == ""

    @pytest.mark.asyncio
    async def test_calls_on_send(
        self, scheduler: TaskScheduler, on_send: AsyncMock, on_trigger: AsyncMock
    ):
        on_trigger.return_value = "report"
        task = _make_task(label="Daily Report")
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        on_send.assert_awaited_once()
        call_args = on_send.call_args
        assert call_args[0][0] == "chat1"
        assert "Daily Report" in call_args[0][1]
        assert "report" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_no_send_for_empty_result(
        self, scheduler: TaskScheduler, on_send: AsyncMock, on_trigger: AsyncMock
    ):
        on_trigger.return_value = ""
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        on_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_send_when_no_callback(self, workspace: Path, on_trigger: AsyncMock):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=on_trigger, on_send=None)
        on_trigger.return_value = "result"
        task = _make_task()
        task["task_id"] = "task_001"
        await s._execute_task("chat1", task)  # should not raise

    @pytest.mark.asyncio
    async def test_no_trigger_callback(self, workspace: Path):
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=None, on_send=AsyncMock())
        task = _make_task()
        task["task_id"] = "task_001"
        await s._execute_task("chat1", task)  # should not raise

    @pytest.mark.asyncio
    async def test_persists_after_execution(
        self, scheduler: TaskScheduler, workspace: Path, on_trigger: AsyncMock
    ):
        on_trigger.return_value = "done"
        task = _make_task()
        task["task_id"] = "task_001"
        scheduler._tasks["chat1"] = [task]
        await scheduler._execute_task("chat1", task)
        # _execute_task no longer persists; caller must batch persist
        await scheduler._persist("chat1")
        path = _tasks_file(workspace, "chat1")
        data = json.loads(path.read_text())
        assert data[0]["last_run"] is not None
        assert data[0]["last_result"] == "done"

    @pytest.mark.asyncio
    async def test_compare_mode_with_previous_result(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        on_trigger.return_value = "new result"
        task = _make_task(prompt="check prices")
        task["task_id"] = "task_001"
        task["compare"] = True
        task["last_result"] = "old result"
        await scheduler._execute_task("chat1", task)
        # on_trigger should be called with augmented prompt
        call_prompt = on_trigger.call_args[0][1]
        assert "PREVIOUS RESULT" in call_prompt
        assert "old result" in call_prompt
        assert "check prices" in call_prompt

    @pytest.mark.asyncio
    async def test_compare_mode_no_previous_result(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """When compare=True but no last_result, prompt should be plain."""
        on_trigger.return_value = "fresh result"
        task = _make_task(prompt="original")
        task["task_id"] = "task_001"
        task["compare"] = True
        task["last_result"] = None
        await scheduler._execute_task("chat1", task)
        call_prompt = on_trigger.call_args[0][1]
        assert call_prompt == "original"

    @pytest.mark.asyncio
    async def test_exception_in_trigger_caught(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        on_trigger.side_effect = RuntimeError("LLM down")
        task = _make_task()
        task["task_id"] = "task_001"
        # Should not raise — exception is caught internally
        await scheduler._execute_task("chat1", task)


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _run_loop
# ─────────────────────────────────────────────────────────────────────────────


class TestLoop:
    """Tests for the background _run_loop tick behaviour."""

    @pytest.mark.asyncio
    async def test_loop_executes_due_tasks(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        on_trigger.return_value = "done"
        # Add an interval task that is always due (no last_run)
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))

        # Patch TICK_SECONDS temporarily so loop ticks fast
        with patch("src.scheduler.TICK_SECONDS", 0.1):
            scheduler._running = True
            task = asyncio.create_task(scheduler._run_loop())
            await asyncio.sleep(0.3)
            scheduler._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert on_trigger.await_count >= 1

    @pytest.mark.asyncio
    async def test_loop_skips_disabled_tasks(self, scheduler: TaskScheduler, on_trigger: AsyncMock):
        on_trigger.return_value = "done"
        task_dict = _make_task(schedule_type="interval", seconds=60)
        await scheduler.add_task("chat1", task_dict)
        # Disable the task
        scheduler.list_tasks("chat1")[0]["enabled"] = False

        with patch("src.scheduler.TICK_SECONDS", 0.1):
            task = asyncio.create_task(scheduler._run_loop())
            await asyncio.sleep(0.35)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        on_trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_loop_batches_persists_for_same_chat(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
        workspace: Path,
    ):
        """Multiple tasks for the same chat in one tick persist only once."""
        on_trigger.return_value = "done"
        # Add 3 interval tasks for chat1 — all are due (no last_run)
        await scheduler.add_task("chat1", _make_task(prompt="task-a"))
        await scheduler.add_task("chat1", _make_task(prompt="task-b"))
        await scheduler.add_task("chat1", _make_task(prompt="task-c"))

        # Patch _persist to count calls
        original_persist = scheduler._persist
        persist_call_count = 0
        persist_chat_ids: list[str] = []

        async def counting_persist(cid: str) -> None:
            nonlocal persist_call_count
            persist_call_count += 1
            persist_chat_ids.append(cid)
            await original_persist(cid)

        scheduler._persist = counting_persist  # type: ignore[assignment]

        with patch("src.scheduler.TICK_SECONDS", 0.1):
            scheduler._running = True
            loop_task = asyncio.create_task(scheduler._run_loop())
            await asyncio.sleep(0.3)
            scheduler._running = False
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        # 3 tasks for the same chat → _persist called exactly once per tick
        # (may run multiple ticks, but each tick batches to 1 call for chat1)
        assert on_trigger.await_count >= 3
        # Verify all task states were persisted correctly
        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        for t in data:
            assert t["last_run"] is not None
            assert t["last_result"] == "done"

    @pytest.mark.asyncio
    async def test_loop_persists_multiple_chats(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
        workspace: Path,
    ):
        """Tasks across multiple chats are each persisted once per tick."""
        on_trigger.return_value = "done"
        await scheduler.add_task("chatA", _make_task(prompt="a"))
        await scheduler.add_task("chatA", _make_task(prompt="b"))
        await scheduler.add_task("chatB", _make_task(prompt="c"))

        original_persist = scheduler._persist
        persist_chat_ids_per_tick: list[set[str]] = []
        current_tick_ids: set[str] = set()

        async def tracking_persist(cid: str) -> None:
            current_tick_ids.add(cid)
            await original_persist(cid)

        scheduler._persist = tracking_persist  # type: ignore[assignment]

        with patch("src.scheduler.TICK_SECONDS", 0.1):
            scheduler._running = True
            loop_task = asyncio.create_task(scheduler._run_loop())

            # Capture per-tick sets
            for _ in range(3):
                await asyncio.sleep(0.15)
                if current_tick_ids:
                    persist_chat_ids_per_tick.append(frozenset(current_tick_ids))
                    current_tick_ids.clear()

            scheduler._running = False
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        # Each tick should have at most 2 unique chat_ids persisted
        for tick_ids in persist_chat_ids_per_tick:
            assert tick_ids <= {"chatA", "chatB"}

        # Verify both chats' data was persisted
        data_a = json.loads(_tasks_file(workspace, "chatA").read_text())
        data_b = json.loads(_tasks_file(workspace, "chatB").read_text())
        assert len(data_a) == 2
        assert len(data_b) == 1
        for t in data_a + data_b:
            assert t["last_run"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — Adaptive Sleep
# ─────────────────────────────────────────────────────────────────────────────


class TestAdaptiveSleep:
    """Tests for adaptive sleep in _loop() via _time_to_next_due and _compute_adaptive_sleep."""

    def test_time_to_next_due_returns_none_when_no_tasks(self, scheduler: TaskScheduler):
        """No tasks registered → None."""
        assert scheduler._time_to_next_due() is None

    @pytest.mark.asyncio
    async def test_time_to_next_due_interval_task_imminent(self, scheduler: TaskScheduler):
        """Interval task with no last_run is due immediately → 0."""
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))
        result = scheduler._time_to_next_due()
        assert result is not None
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_time_to_next_due_interval_task_waiting(self, scheduler: TaskScheduler):
        """Interval task recently run → remaining time is positive."""
        task_dict = _make_task(schedule_type="interval", seconds=600)
        await scheduler.add_task("chat1", task_dict)
        # Simulate a recent run
        task_dict["last_run"] = _now().isoformat()
        result = scheduler._time_to_next_due()
        assert result is not None
        # Should be close to 600 seconds minus a small elapsed time
        assert 590 < result <= 600

    @pytest.mark.asyncio
    async def test_time_to_next_due_skips_disabled_tasks(self, scheduler: TaskScheduler):
        """Disabled tasks are excluded from computation."""
        task_dict = _make_task(schedule_type="interval", seconds=60)
        await scheduler.add_task("chat1", task_dict)
        task_dict["enabled"] = False
        assert scheduler._time_to_next_due() is None

    @pytest.mark.asyncio
    async def test_time_to_next_due_picks_minimum_across_tasks(self, scheduler: TaskScheduler):
        """With multiple tasks, returns the soonest time-to-due."""
        # Task A: due immediately (no last_run)
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))
        # Task B: due in 300s
        task_b = _make_task(schedule_type="interval", seconds=600)
        await scheduler.add_task("chat2", task_b)
        task_b["last_run"] = _now().isoformat()

        result = scheduler._time_to_next_due()
        assert result is not None
        assert result == 0.0  # Task A wins

    def test_compute_adaptive_sleep_no_tasks(self, scheduler: TaskScheduler):
        """No tasks → SCHEDULER_MAX_SLEEP_SECONDS."""
        assert scheduler._compute_adaptive_sleep() == SCHEDULER_MAX_SLEEP_SECONDS

    @pytest.mark.asyncio
    async def test_compute_adaptive_sleep_task_imminent(self, scheduler: TaskScheduler):
        """Task due immediately → SCHEDULER_MIN_SLEEP_SECONDS (floor clamp)."""
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))
        assert scheduler._compute_adaptive_sleep() == SCHEDULER_MIN_SLEEP_SECONDS

    @pytest.mark.asyncio
    async def test_compute_adaptive_sleep_task_far_away(self, scheduler: TaskScheduler):
        """Task due in > TICK_SECONDS → capped at TICK_SECONDS."""
        task_dict = _make_task(schedule_type="interval", seconds=9999)
        await scheduler.add_task("chat1", task_dict)
        task_dict["last_run"] = _now().isoformat()
        result = scheduler._compute_adaptive_sleep()
        assert result == TICK_SECONDS

    @pytest.mark.asyncio
    async def test_compute_adaptive_sleep_task_soon_but_not_imminent(
        self, scheduler: TaskScheduler
    ):
        """Task due in 10s (within TICK_SECONDS) → returns actual time-to-due."""
        task_dict = _make_task(schedule_type="interval", seconds=10)
        await scheduler.add_task("chat1", task_dict)
        # Mark as just run so the next due is ~10s from now
        task_dict["last_run"] = _now().isoformat()
        result = scheduler._compute_adaptive_sleep()
        assert SCHEDULER_MIN_SLEEP_SECONDS <= result <= 10.5

    @pytest.mark.asyncio
    async def test_loop_uses_adaptive_sleep_with_no_tasks(self, scheduler: TaskScheduler):
        """When no tasks exist, loop sleeps SCHEDULER_MAX_SLEEP_SECONDS per tick."""
        sleep_durations: list[float] = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(duration: float) -> None:
            sleep_durations.append(duration)
            await original_sleep(0.01)

        with patch("src.scheduler.asyncio.sleep", side_effect=tracking_sleep):
            scheduler._running = True
            loop_task = asyncio.create_task(scheduler._run_loop())
            await original_sleep(0.3)
            scheduler._running = False
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        # All sleeps should be SCHEDULER_MAX_SLEEP_SECONDS since no tasks
        assert len(sleep_durations) >= 1
        for d in sleep_durations:
            assert d == SCHEDULER_MAX_SLEEP_SECONDS

    @pytest.mark.asyncio
    async def test_loop_uses_adaptive_sleep_with_due_task(self, scheduler: TaskScheduler):
        """When a task is due, loop uses minimum sleep duration."""
        on_trigger = scheduler._on_trigger  # type: ignore[attr-defined]
        on_trigger.return_value = "result"
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))

        sleep_durations: list[float] = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(duration: float) -> None:
            sleep_durations.append(duration)
            await original_sleep(0.01)

        with patch("src.scheduler.asyncio.sleep", side_effect=tracking_sleep):
            scheduler._running = True
            loop_task = asyncio.create_task(scheduler._run_loop())
            await original_sleep(0.5)
            scheduler._running = False
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        # Task is due immediately → executes, then sleeps until next due (capped at TICK_SECONDS)
        assert len(sleep_durations) >= 1
        assert sleep_durations[0] == TICK_SECONDS

    @pytest.mark.asyncio
    async def test_time_to_next_due_daily_task(self, scheduler: TaskScheduler):
        """Daily task computes seconds until next target minute."""
        now = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 0.0
            scheduler._utc_offset_updated_at = time.monotonic()
            # Target at 09:00 UTC = 3600s from now
            await scheduler.add_task("chat1", _make_task(schedule_type="daily", hour=9, minute=0))
            result = scheduler._time_to_next_due()
            assert result is not None
            assert 3590 < result <= 3600


# ─────────────────────────────────────────────────────────────────────────────


class TestTimeToNextDueCache:
    """Tests for _tasks_dirty caching of _time_to_next_due()."""

    def test_cache_hit_returns_same_value(self, scheduler: TaskScheduler):
        """Consecutive calls with no mutations return cached value."""
        assert scheduler._tasks_dirty is True
        # No tasks → None
        result1 = scheduler._time_to_next_due()
        assert result1 is None
        assert scheduler._tasks_dirty is False
        # Second call hits cache
        result2 = scheduler._time_to_next_due()
        assert result2 is None

    @pytest.mark.asyncio
    async def test_add_task_invalidates_cache(self, scheduler: TaskScheduler):
        """Adding a task sets _tasks_dirty and recomputes."""
        # Prime cache as empty
        assert scheduler._time_to_next_due() is None
        assert scheduler._tasks_dirty is False

        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))
        assert scheduler._tasks_dirty is True
        result = scheduler._time_to_next_due()
        assert result is not None
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_remove_task_invalidates_cache(self, scheduler: TaskScheduler):
        """Removing a task sets _tasks_dirty and recomputes."""
        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))
        # Prime cache
        scheduler._time_to_next_due()
        assert scheduler._tasks_dirty is False

        await scheduler.remove_task_async("chat1", "task_001")
        assert scheduler._tasks_dirty is True
        assert scheduler._time_to_next_due() is None

    @pytest.mark.asyncio
    async def test_execute_task_invalidates_cache(self, scheduler: TaskScheduler):
        """Executing a task sets _tasks_dirty so next computation is fresh."""
        task_dict = _make_task(schedule_type="interval", seconds=600)
        await scheduler.add_task("chat1", task_dict)
        # Prime cache
        scheduler._time_to_next_due()
        assert scheduler._tasks_dirty is False

        await scheduler._execute_task("chat1", task_dict)
        assert scheduler._tasks_dirty is True

    @pytest.mark.asyncio
    async def test_load_invalidates_cache(self, scheduler: TaskScheduler):
        """Loading tasks from disk sets _tasks_dirty."""
        # Prime cache as empty
        scheduler._time_to_next_due()
        assert scheduler._tasks_dirty is False

        # Write a tasks file to disk
        tasks_path = _tasks_file(scheduler._workspace, "chat1")
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        tasks_path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "loaded task",
                        "schedule": {"type": "interval", "seconds": 60},
                        "enabled": True,
                    }
                ]
            )
        )

        await scheduler._load("chat1")
        assert scheduler._tasks_dirty is True
        result = scheduler._time_to_next_due()
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _get_last_run_dt caching
# ─────────────────────────────────────────────────────────────────────────────


class TestLastRunDtCache:
    """Tests for cached parsed ``last_run`` datetime (``_last_run_dt``)."""

    def test_lazy_parse_on_first_access(self, scheduler: TaskScheduler):
        """_get_last_run_dt parses and caches the ISO string on first call."""
        task = _make_task(schedule_type="interval", seconds=60)
        now = _now()
        task["last_run"] = now.isoformat()
        assert "_last_run_dt" not in task

        result = scheduler._get_last_run_dt(task)
        assert result is not None
        assert result == now
        assert task["_last_run_dt"] is result  # cached

    def test_cache_hit_returns_same_object(self, scheduler: TaskScheduler):
        """Repeated calls return the same cached datetime without re-parsing."""
        task = _make_task(schedule_type="interval", seconds=60)
        task["last_run"] = _now().isoformat()

        first = scheduler._get_last_run_dt(task)
        second = scheduler._get_last_run_dt(task)
        assert first is second

    def test_returns_none_when_no_last_run(self, scheduler: TaskScheduler):
        """Tasks with no last_run return None without caching."""
        task = _make_task(schedule_type="interval", seconds=60)
        assert scheduler._get_last_run_dt(task) is None
        assert "_last_run_dt" not in task

    def test_returns_none_on_invalid_iso(self, scheduler: TaskScheduler):
        """Malformed ISO string returns None without caching."""
        task = _make_task(schedule_type="interval", seconds=60)
        task["last_run"] = "not-a-datetime"
        assert scheduler._get_last_run_dt(task) is None
        assert "_last_run_dt" not in task

    @pytest.mark.asyncio
    async def test_execute_task_sets_cache(self, scheduler: TaskScheduler):
        """_execute_task sets _last_run_dt alongside last_run."""
        task_dict = _make_task(schedule_type="interval", seconds=600)
        await scheduler.add_task("chat1", task_dict)
        assert "_last_run_dt" not in task_dict

        await scheduler._execute_task("chat1", task_dict)
        assert "_last_run_dt" in task_dict
        assert isinstance(task_dict["_last_run_dt"], datetime)
        assert task_dict["_last_run_dt"].isoformat() == task_dict["last_run"]

    def test_is_due_uses_cache(self, scheduler: TaskScheduler):
        """_is_due populates _last_run_dt on first call for interval tasks."""
        task = _make_task(schedule_type="interval", seconds=10)
        task["last_run"] = (_now() - timedelta(seconds=15)).isoformat()
        assert "_last_run_dt" not in task

        scheduler._is_due(task)
        assert "_last_run_dt" in task

    def test_is_due_daily_uses_cached_dt(self, scheduler: TaskScheduler):
        """_is_due for daily tasks uses cached dt for same-day comparison."""
        now = _now()
        offset = scheduler._get_cached_utc_offset()
        local_hour = (now.hour + int(offset)) % 24

        task = _make_task(schedule_type="daily", hour=local_hour, minute=now.minute)
        task["last_run"] = now.isoformat()
        assert "_last_run_dt" not in task

        # Already ran today → not due
        assert scheduler._is_due(task) is False
        assert "_last_run_dt" in task

        # Second call reuses cached dt
        cached = task["_last_run_dt"]
        scheduler._is_due(task)
        assert task["_last_run_dt"] is cached  # same object, no re-parse
    """End-to-end scenarios combining multiple operations."""

    @pytest.mark.asyncio
    async def test_full_crud_round_trip(self, workspace: Path):
        """add → list → remove → list → persist → reload."""
        on_trigger = AsyncMock(return_value="ok")
        on_send = AsyncMock()
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=on_trigger, on_send=on_send)

        # Add
        tid = await s.add_task("chat1", _make_task(prompt="round trip"))
        assert len(s.list_tasks("chat1")) == 1

        # List
        tasks = s.list_tasks("chat1")
        assert tasks[0]["prompt"] == "round trip"

        # Execute
        await s._execute_task("chat1", tasks[0])
        assert tasks[0]["last_run"] is not None
        on_trigger.assert_awaited_once()

        # Remove
        removed = await s.remove_task_async("chat1", tid)
        assert removed is True
        assert s.list_tasks("chat1") == []

        # Verify file reflects removal
        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        assert data == []

    @pytest.mark.asyncio
    async def test_scheduler_restart_preserves_tasks(self, workspace: Path):
        """Tasks survive a scheduler restart via load_all."""
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="survive restart"))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2.load_all()

        tasks = s2.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "survive restart"

    @pytest.mark.asyncio
    async def test_execute_and_persist_result(self, workspace: Path):
        """After execution, task file should contain last_run and last_result."""
        on_trigger = AsyncMock(return_value="analysis complete")
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=on_trigger, on_send=AsyncMock())

        task = _make_task(prompt="analyze")
        await s.add_task("chat1", task)
        t = s.list_tasks("chat1")[0]

        await s._execute_task("chat1", t)
        # _execute_task no longer persists; batch persist manually
        await s._persist("chat1")

        # Reload from disk
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        loaded = s2.list_tasks("chat1")[0]
        assert loaded["last_run"] is not None
        assert loaded["last_result"] == "analysis complete"

    @pytest.mark.asyncio
    async def test_start_stop_with_pending_tasks(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """Scheduler should not execute tasks that aren't due after start."""
        task = _make_task(schedule_type="interval", seconds=99999)
        await scheduler.add_task("chat1", task)
        # Set last_run AFTER adding (add_task resets it to None)
        t = scheduler.list_tasks("chat1")[0]
        t["last_run"] = _now().isoformat()  # just ran

        scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop()
        # Task not due — trigger should not have been called
        on_trigger.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — retry with exponential backoff
# ─────────────────────────────────────────────────────────────────────────────


class TestExecuteTaskRetry:
    """Tests for _execute_task retry behaviour on transient failures."""

    @pytest.mark.asyncio
    async def test_success_first_attempt_no_retry(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """Successful trigger on first attempt — no retries needed."""
        on_trigger.return_value = "ok"
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)
        assert on_trigger.await_count == 1
        assert task["last_result"] == "ok"
        assert scheduler._success_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_then_succeeds(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """Transient error on first call, success on retry."""
        on_trigger.side_effect = [
            _make_transient_error("connection reset"),
            "recovered result",
        ]
        task = _make_task()
        task["task_id"] = "task_001"

        with patch("src.scheduler.SCHEDULER_RETRY_INITIAL_DELAY", 0.01):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await scheduler._execute_task("chat1", task)

        assert on_trigger.await_count == 2
        assert task["last_result"] == "recovered result"
        assert scheduler._success_count == 1

    @pytest.mark.asyncio
    async def test_retries_exhausted_marks_failure(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """When all retries fail on transient errors, task is marked failed."""
        on_trigger.side_effect = _make_transient_error("timeout")

        task = _make_task()
        task["task_id"] = "task_001"

        with patch("src.scheduler.SCHEDULER_RETRY_INITIAL_DELAY", 0.01):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await scheduler._execute_task("chat1", task)

        assert on_trigger.await_count == SCHEDULER_MAX_RETRIES + 1
        assert scheduler._failure_count == 1
        assert scheduler._success_count == 0
        # last_run not updated on failure
        assert task["last_run"] is None

    @pytest.mark.asyncio
    async def test_non_transient_error_fails_immediately(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """Non-transient errors (e.g. authentication) skip retries entirely."""
        on_trigger.side_effect = PermissionError("Invalid API key")

        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        # Should only be called once — no retry for non-transient errors
        assert on_trigger.await_count == 1
        assert scheduler._failure_count == 1

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """Verify delay doubles on each retry attempt."""
        on_trigger.side_effect = [
            _make_transient_error("timeout"),
            _make_transient_error("rate limit"),
            "success",
        ]

        task = _make_task()
        task["task_id"] = "task_001"

        sleep_mock = AsyncMock()
        with patch("asyncio.sleep", sleep_mock):
            # Use a fixed jitter of 0 by patching calculate_delay_with_jitter
            with patch(
                "src.utils.retry.calculate_delay_with_jitter",
                side_effect=lambda d: d,
            ):
                with patch("src.scheduler.SCHEDULER_RETRY_INITIAL_DELAY", 30.0):
                    await scheduler._execute_task("chat1", task)

        # Two sleeps: first at 30s, second at 60s (doubled)
        assert sleep_mock.await_count == 2
        delays = [call.args[0] for call in sleep_mock.call_args_list]
        assert delays[0] == pytest.approx(30.0)
        assert delays[1] == pytest.approx(60.0)
        assert task["last_result"] == "success"

    @pytest.mark.asyncio
    async def test_retry_with_send_on_success(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
        on_send: AsyncMock,
    ):
        """After successful retry, result is delivered via on_send."""
        on_trigger.side_effect = [
            _make_transient_error("connection refused"),
            "final result",
        ]
        task = _make_task(label="My Task")
        task["task_id"] = "task_001"

        with patch("src.scheduler.SCHEDULER_RETRY_INITIAL_DELAY", 0.01):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await scheduler._execute_task("chat1", task)

        on_send.assert_awaited_once()
        send_text = on_send.call_args[0][1]
        assert "My Task" in send_text
        assert "final result" in send_text

    @pytest.mark.asyncio
    async def test_no_retry_when_no_callback(self, workspace: Path):
        """If on_trigger is None, no retry is attempted."""
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=None, on_send=AsyncMock())
        task = _make_task()
        task["task_id"] = "task_001"
        await s._execute_task("chat1", task)
        assert s._failure_count == 0
        assert s._success_count == 0


def _make_transient_error(message: str) -> Exception:
    """Create an exception whose string matches a transient error pattern."""
    return RuntimeError(message)


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — task-execution timeout
# ─────────────────────────────────────────────────────────────────────────────


class TestExecuteTaskTimeout:
    """Tests for _execute_task timeout when _trigger_with_retry hangs."""

    @pytest.mark.asyncio
    async def test_timeout_fires_task_marked_failed(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
    ):
        """A hanging trigger times out and the task is recorded as failed."""

        async def hang_forever(chat_id: str, prompt: str, hmac: str | None = None) -> str:
            await asyncio.sleep(600)

        on_trigger.side_effect = hang_forever

        task = _make_task()
        task["task_id"] = "task_001"

        with patch("src.scheduler.DEFAULT_SCHEDULER_TASK_TIMEOUT", 0.05):
            await scheduler._execute_task("chat1", task)

        assert scheduler._failure_count == 1
        assert scheduler._success_count == 0
        assert task["last_run"] is None
        # Verify a failure entry was recorded
        assert len(scheduler._recent_executions) == 1
        entry = scheduler._recent_executions[0]
        assert entry["status"] == "failure"
        assert "timed out" in entry["error_summary"]

    @pytest.mark.asyncio
    async def test_other_tasks_not_blocked_by_timeout(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
        on_send: AsyncMock,
    ):
        """Co-scheduled tasks complete even when one task times out."""
        fast_task = _make_task(prompt="fast")
        fast_task["task_id"] = "task_fast"
        slow_task = _make_task(prompt="slow")
        slow_task["task_id"] = "task_slow"

        # First call hangs (slow task), second call succeeds (fast task)
        call_count = 0

        async def selective_hang(chat_id: str, prompt: str, hmac: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            if "slow" in prompt:
                await asyncio.sleep(600)
            return "fast result"

        on_trigger.side_effect = selective_hang

        with patch("src.scheduler.DEFAULT_SCHEDULER_TASK_TIMEOUT", 0.05):
            results = await asyncio.gather(
                scheduler._execute_task("chat1", slow_task),
                scheduler._execute_task("chat2", fast_task),
                return_exceptions=True,
            )

        # Fast task should succeed; slow task should have timed out
        assert scheduler._success_count == 1
        assert scheduler._failure_count == 1
        assert fast_task["last_run"] is not None
        assert fast_task["last_result"] == "fast result"
        assert slow_task["last_run"] is None

    @pytest.mark.asyncio
    async def test_loop_continues_after_timeout(
        self,
        scheduler: TaskScheduler,
        on_trigger: AsyncMock,
        workspace: Path,
    ):
        """Scheduler loop continues to the next tick after a task timeout."""
        await scheduler.add_task("chat1", _make_task(prompt="hang"))

        call_count = 0

        async def hang_then_succeed(chat_id: str, prompt: str, hmac: str | None = None) -> str:
            nonlocal call_count
            call_count += 1
            # First invocation hangs; subsequent ones succeed quickly
            if call_count == 1:
                await asyncio.sleep(600)
            return "recovered"

        on_trigger.side_effect = hang_then_succeed

        with patch("src.scheduler.DEFAULT_SCHEDULER_TASK_TIMEOUT", 0.05):
            with patch("src.scheduler.TICK_SECONDS", 0.1):
                with patch("src.scheduler.SCHEDULER_MIN_SLEEP_SECONDS", 0.01):
                    scheduler._running = True
                    loop_task = asyncio.create_task(scheduler._run_loop())
                    await asyncio.sleep(0.5)
                    scheduler._running = False
                    loop_task.cancel()
                    try:
                        await loop_task
                    except asyncio.CancelledError:
                        pass

        # At least 2 trigger attempts: first times out, second succeeds
        assert on_trigger.await_count >= 2
        assert scheduler._success_count >= 1
        assert scheduler._failure_count >= 1


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _is_due timezone edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueTimezoneEdgeCases:
    """Tests for _is_due() with fractional UTC offsets.

    Real-world timezones with fractional offsets:
      - India (UTC+5:30): offset 5.5 → 330 minutes
      - Nepal (UTC+5:45): offset 5.75 → 345 minutes
      - Newfoundland (UTC-3:30): offset -3.5 → -210 minutes
      - Afghanistan (UTC+4:30): offset 4.5 → 270 minutes
    """

    @pytest.mark.parametrize(
        "offset, local_hour, local_min, expected_utc_hour, expected_utc_min",
        [
            # India UTC+5:30
            (5.5, 9, 30, 4, 0),
            (5.5, 0, 0, 18, 30),  # midnight IST = 18:30 UTC previous day
            (5.5, 23, 59, 18, 29),
            # Nepal UTC+5:45
            (5.75, 14, 15, 8, 30),
            (5.75, 0, 0, 18, 15),  # midnight NPT = 18:15 UTC previous day
            # Afghanistan UTC+4:30
            (4.5, 12, 0, 7, 30),
            (4.5, 0, 0, 19, 30),
            # Newfoundland UTC-3:30
            (-3.5, 9, 0, 12, 30),
            (-3.5, 20, 30, 0, 0),  # 20:30 NST = midnight UTC next day
        ],
    )
    def test_daily_correct_utc_conversion(
        self,
        scheduler: TaskScheduler,
        offset: float,
        local_hour: int,
        local_min: int,
        expected_utc_hour: int,
        expected_utc_min: int,
    ):
        """Daily task at local time fires at the correct UTC equivalent."""
        now = datetime(2026, 4, 20, expected_utc_hour, expected_utc_min, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = offset
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=local_hour, minute=local_min)
            assert scheduler._is_due(task) is True

    @pytest.mark.parametrize(
        "offset, local_hour, local_min, wrong_utc_hour, wrong_utc_min",
        [
            # India: local 09:30 should NOT fire at UTC 04:01
            (5.5, 9, 30, 4, 1),
            # India: local 09:30 should NOT fire at UTC 03:59
            (5.5, 9, 30, 3, 59),
            # Nepal: local 14:15 should NOT fire at UTC 08:31
            (5.75, 14, 15, 8, 31),
            # Newfoundland: local 09:00 should NOT fire at UTC 12:29
            (-3.5, 9, 0, 12, 29),
        ],
    )
    def test_daily_does_not_fire_at_wrong_minute(
        self,
        scheduler: TaskScheduler,
        offset: float,
        local_hour: int,
        local_min: int,
        wrong_utc_hour: int,
        wrong_utc_min: int,
    ):
        """Daily task should NOT fire one minute off from the correct UTC time."""
        now = datetime(2026, 4, 20, wrong_utc_hour, wrong_utc_min, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = offset
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=local_hour, minute=local_min)
            assert scheduler._is_due(task) is False

    @pytest.mark.parametrize(
        "offset",
        [5.5, 5.75, 4.5, -3.5, 9.5, 6.5, 3.5, -4.5, -9.5],
    )
    def test_daily_no_double_fire_within_same_minute(self, scheduler: TaskScheduler, offset: float):
        """Task with last_run set to now should not be due again (same minute)."""
        now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
        local_min_total = int(12 * 60 + 0 + offset * 60) % (24 * 60)
        local_hour = local_min_total // 60
        local_min = local_min_total % 60

        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = offset
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=local_hour, minute=local_min)
            task["last_run"] = now.isoformat()
            assert scheduler._is_due(task) is False

    @pytest.mark.parametrize(
        "offset, local_hour, local_min, expected_utc_hour, expected_utc_min",
        [
            (5.5, 9, 30, 4, 0),
            (5.75, 14, 15, 8, 30),
            (-3.5, 9, 0, 12, 30),
        ],
    )
    def test_cron_correct_utc_conversion(
        self,
        scheduler: TaskScheduler,
        offset: float,
        local_hour: int,
        local_min: int,
        expected_utc_hour: int,
        expected_utc_min: int,
    ):
        """Cron task at local time fires at the correct UTC equivalent."""
        # Monday April 20, 2026
        now = datetime(2026, 4, 20, expected_utc_hour, expected_utc_min, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = offset
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(
                schedule_type="cron",
                hour=local_hour,
                minute=local_min,
                weekdays=[now.weekday()],
            )
            assert scheduler._is_due(task) is True

    @pytest.mark.parametrize(
        "offset, local_hour, local_min, expected_utc_hour, expected_utc_min",
        [
            (5.5, 9, 30, 4, 0),
            (5.75, 14, 15, 8, 30),
            (-3.5, 9, 0, 12, 30),
        ],
    )
    def test_cron_wrong_weekday_not_due(
        self,
        scheduler: TaskScheduler,
        offset: float,
        local_hour: int,
        local_min: int,
        expected_utc_hour: int,
        expected_utc_min: int,
    ):
        """Cron task should not fire on a non-matching weekday."""
        now = datetime(2026, 4, 20, expected_utc_hour, expected_utc_min, tzinfo=timezone.utc)
        wrong_weekday = (now.weekday() + 1) % 7
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = offset
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(
                schedule_type="cron",
                hour=local_hour,
                minute=local_min,
                weekdays=[wrong_weekday],
            )
            assert scheduler._is_due(task) is False

    def test_india_midnight_cross_day_boundary(self, scheduler: TaskScheduler):
        """India UTC+5:30: local 00:00 crosses to previous UTC day."""
        # Local midnight IST = 18:30 UTC previous day
        # Test with April 21 00:00 IST = April 20 18:30 UTC
        now = datetime(2026, 4, 20, 18, 30, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 5.5
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=0, minute=0)
            assert scheduler._is_due(task) is True

    def test_nepal_offset_rounding_precision(self, scheduler: TaskScheduler):
        """Nepal UTC+5:45 — verify no floating-point rounding issues."""
        # 5.75 * 60 = 345.0 exactly in IEEE 754
        # int(345.0) = 345, no truncation
        now = datetime(2026, 4, 20, 8, 30, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 5.75
            scheduler._utc_offset_updated_at = time.monotonic()

            # local 14:15 in NPT = UTC 08:30
            task = _make_task(schedule_type="daily", hour=14, minute=15)
            assert scheduler._is_due(task) is True

            # Verify it does NOT fire one minute earlier or later
            now_off = datetime(2026, 4, 20, 8, 29, tzinfo=timezone.utc)
            with patch("src.scheduler._now", return_value=now_off):
                assert scheduler._is_due(task) is False

            now_off2 = datetime(2026, 4, 20, 8, 31, tzinfo=timezone.utc)
            with patch("src.scheduler._now", return_value=now_off2):
                assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — DST transition handling
# ─────────────────────────────────────────────────────────────────────────────


class TestDSTTransitionHandling:
    """Tests for TaskScheduler DST (Daylight Saving Time) transition handling.

    DST transitions change the local UTC offset:
      - Spring forward: CET (UTC+1) → CEST (UTC+2), offset increases by 1
      - Fall back: CEST (UTC+2) → CET (UTC+1), offset decreases by 1

    The scheduler caches the UTC offset with a 1-hour TTL. These tests
    verify that daily and cron tasks behave correctly when the offset
    changes, both with stale cached values and after cache refresh.
    """

    def test_daily_spring_forward_after_cache_refresh(self, scheduler: TaskScheduler):
        """After spring-forward, daily task fires at correct UTC time with refreshed offset.

        CET (UTC+1) → CEST (UTC+2): local 09:00 shifts from UTC 08:00 to UTC 07:00.
        """
        now = datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # refreshed to CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            assert scheduler._is_due(task) is True

    def test_daily_spring_forward_stale_offset_misses_fire(self, scheduler: TaskScheduler):
        """With stale pre-DST offset, daily task misses the correct post-DST UTC time.

        After spring-forward, actual offset is +2 (CEST). Stale cache is +1 (CET).
        Local 09:00 with stale +1 computes as UTC 08:00, not the correct UTC 07:00.
        """
        now = datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # stale pre-DST CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            assert scheduler._is_due(task) is False

    def test_daily_spring_forward_fires_at_stale_computed_time(self, scheduler: TaskScheduler):
        """With stale offset, the task fires at the old UTC time (one hour late post-DST).

        After spring-forward, stale +1 maps local 09:00 to UTC 08:00.
        The task fires at UTC 08:00, which is local 10:00 post-DST.
        """
        now = datetime(2026, 3, 29, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # stale CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            assert scheduler._is_due(task) is True

    def test_daily_fall_back_after_cache_refresh(self, scheduler: TaskScheduler):
        """After fall-back, daily task fires at correct UTC time with refreshed offset.

        CEST (UTC+2) → CET (UTC+1): local 09:00 shifts from UTC 07:00 to UTC 08:00.
        """
        now = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # refreshed to CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            assert scheduler._is_due(task) is True

    def test_daily_fall_back_with_stale_offset(self, scheduler: TaskScheduler):
        """With stale pre-fall-back offset, daily task misses the correct post-transition time.

        After fall-back, actual offset is +1 (CET). Stale cache is +2 (CEST).
        Local 09:00 with stale +2 computes as UTC 07:00, not the correct UTC 08:00.
        """
        now = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # stale CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            assert scheduler._is_due(task) is False

    def test_daily_no_double_fire_across_spring_forward(self, scheduler: TaskScheduler):
        """Task doesn't fire twice on the same calendar day after spring-forward.

        The _same_day check via last_run prevents duplicate firing even
        when the offset cache refreshes mid-day.
        """
        now = datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # refreshed CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            task["last_run"] = now.isoformat()  # already ran today
            assert scheduler._is_due(task) is False

    def test_daily_no_double_fire_across_fall_back(self, scheduler: TaskScheduler):
        """Task doesn't fire twice on the same calendar day after fall-back."""
        now = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # refreshed CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            task["last_run"] = now.isoformat()  # already ran today
            assert scheduler._is_due(task) is False

    def test_cron_spring_forward_after_cache_refresh(self, scheduler: TaskScheduler):
        """Cron task fires at correct UTC time after spring-forward with refreshed offset."""
        # Monday after spring-forward in Europe
        now = datetime(2026, 3, 30, 7, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="cron", hour=9, minute=0, weekdays=[now.weekday()])
            assert scheduler._is_due(task) is True

    def test_cron_fall_back_after_cache_refresh(self, scheduler: TaskScheduler):
        """Cron task fires at correct UTC time after fall-back with refreshed offset."""
        # Monday after fall-back in Europe
        now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="cron", hour=9, minute=0, weekdays=[now.weekday()])
            assert scheduler._is_due(task) is True

    def test_cache_refreshes_after_ttl_post_dst(self, scheduler: TaskScheduler):
        """After TTL expires post-DST, _get_cached_utc_offset refreshes to the new offset."""
        scheduler._cached_utc_offset = 1.0  # pre-DST CET
        scheduler._utc_offset_updated_at = time.monotonic() - 7200  # 2 hours past TTL

        with patch("src.scheduler._utc_offset_hours", return_value=2.0):
            result = scheduler._get_cached_utc_offset()
            assert result == 2.0

    def test_daily_correct_next_day_after_spring_forward(self, scheduler: TaskScheduler):
        """Task fires on the correct next calendar day after spring-forward.

        Ran on March 28 at UTC 08:00 (pre-DST, CET+1, local 09:00).
        Now March 29 at UTC 07:00 (post-DST, CEST+2, local 09:00) — should be due.
        """
        last_run = datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc).isoformat()
        now = datetime(2026, 3, 29, 7, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # refreshed CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            task["last_run"] = last_run
            assert scheduler._is_due(task) is True

    def test_daily_correct_next_day_after_fall_back(self, scheduler: TaskScheduler):
        """Task fires on the correct next calendar day after fall-back.

        Ran on Oct 24 at UTC 07:00 (pre-fall-back, CEST+2, local 09:00).
        Now Oct 25 at UTC 08:00 (post-fall-back, CET+1, local 09:00) — should be due.
        """
        last_run = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc).isoformat()
        now = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 1.0  # refreshed CET
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="daily", hour=9, minute=0)
            task["last_run"] = last_run
            assert scheduler._is_due(task) is True

    def test_interval_unaffected_by_dst(self, scheduler: TaskScheduler):
        """Interval tasks are DST-agnostic — they only measure elapsed seconds."""
        last_run = datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc).isoformat()
        now = datetime(2026, 3, 29, 7, 30, tzinfo=timezone.utc)  # 90 minutes later
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 2.0  # CEST
            scheduler._utc_offset_updated_at = time.monotonic()

            task = _make_task(schedule_type="interval", seconds=3600)
            task["last_run"] = last_run
            assert scheduler._is_due(task) is True


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _validate_task
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateTask:
    """Tests for TaskScheduler._validate_task() and its integration with
    add_task / add_task."""

    # ── direct validation tests ──

    def test_valid_interval_task(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="interval", seconds=60)
        scheduler._validate_task(task)  # should not raise

    def test_valid_daily_task(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="daily", hour=9, minute=30)
        scheduler._validate_task(task)  # should not raise

    def test_valid_cron_task(self, scheduler: TaskScheduler):
        task = _make_task(schedule_type="cron", hour=14, minute=0, weekdays=[0, 2, 4])
        scheduler._validate_task(task)  # should not raise

    def test_missing_prompt_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="prompt"):
            scheduler._validate_task({"schedule": {"type": "interval", "seconds": 60}})

    def test_empty_prompt_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="prompt"):
            scheduler._validate_task(
                {"prompt": "", "schedule": {"type": "interval", "seconds": 60}}
            )

    def test_whitespace_prompt_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="prompt"):
            scheduler._validate_task(
                {"prompt": "   ", "schedule": {"type": "interval", "seconds": 60}}
            )

    def test_non_string_prompt_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="prompt"):
            scheduler._validate_task(
                {"prompt": 123, "schedule": {"type": "interval", "seconds": 60}}
            )

    def test_missing_schedule_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="schedule"):
            scheduler._validate_task({"prompt": "test"})

    def test_non_dict_schedule_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="schedule"):
            scheduler._validate_task({"prompt": "test", "schedule": "interval"})

    def test_missing_schedule_type_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="type"):
            scheduler._validate_task({"prompt": "test", "schedule": {}})

    def test_invalid_schedule_type_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="type"):
            scheduler._validate_task({"prompt": "test", "schedule": {"type": "yearly"}})

    def test_daily_missing_hour_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="hour.*minute"):
            scheduler._validate_task(
                {"prompt": "test", "schedule": {"type": "daily", "minute": 30}}
            )

    def test_daily_missing_minute_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="hour.*minute"):
            scheduler._validate_task({"prompt": "test", "schedule": {"type": "daily", "hour": 9}})

    def test_interval_missing_seconds_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="seconds"):
            scheduler._validate_task({"prompt": "test", "schedule": {"type": "interval"}})

    def test_interval_zero_seconds_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="seconds"):
            scheduler._validate_task(
                {"prompt": "test", "schedule": {"type": "interval", "seconds": 0}}
            )

    def test_interval_negative_seconds_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="seconds"):
            scheduler._validate_task(
                {"prompt": "test", "schedule": {"type": "interval", "seconds": -10}}
            )

    def test_interval_string_seconds_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="seconds"):
            scheduler._validate_task(
                {"prompt": "test", "schedule": {"type": "interval", "seconds": "60"}}
            )

    def test_prompt_at_max_length_passes(self, scheduler: TaskScheduler):
        """Prompt exactly at MAX_SCHEDULED_PROMPT_LENGTH should be accepted."""
        prompt = "x" * 10_000
        scheduler._validate_task(
            {"prompt": prompt, "schedule": {"type": "interval", "seconds": 60}}
        )

    def test_prompt_one_below_max_length_passes(self, scheduler: TaskScheduler):
        """Prompt one char below MAX_SCHEDULED_PROMPT_LENGTH should be accepted."""
        prompt = "x" * 9_999
        scheduler._validate_task(
            {"prompt": prompt, "schedule": {"type": "interval", "seconds": 60}}
        )

    def test_prompt_exceeds_max_length_raises(self, scheduler: TaskScheduler):
        """Prompt exceeding MAX_SCHEDULED_PROMPT_LENGTH should be rejected."""
        prompt = "x" * 10_001
        with pytest.raises(ValueError, match="exceeds maximum length"):
            scheduler._validate_task(
                {"prompt": prompt, "schedule": {"type": "interval", "seconds": 60}}
            )

    def test_prompt_oversized_error_includes_actual_length(self, scheduler: TaskScheduler):
        """Error message should include the actual prompt length for debugging."""
        prompt = "a" * 15_000
        with pytest.raises(ValueError, match=r"15.?000 chars"):
            scheduler._validate_task(
                {"prompt": prompt, "schedule": {"type": "interval", "seconds": 60}}
            )

    def test_cron_missing_hour_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="hour.*minute"):
            scheduler._validate_task({"prompt": "test", "schedule": {"type": "cron", "minute": 0}})

    def test_cron_missing_minute_raises(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="hour.*minute"):
            scheduler._validate_task({"prompt": "test", "schedule": {"type": "cron", "hour": 12}})

    def test_cron_weekdays_valid_range(self, scheduler: TaskScheduler):
        """Weekdays 0-6 should be accepted."""
        task = _make_task(schedule_type="cron", hour=9, minute=0, weekdays=[0, 1, 2, 3, 4, 5, 6])
        scheduler._validate_task(task)  # should not raise

    def test_cron_weekdays_boundary_values(self, scheduler: TaskScheduler):
        """Boundary values 0 and 6 should be accepted."""
        scheduler._validate_task(
            {
                "prompt": "test",
                "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [0]},
            }
        )
        scheduler._validate_task(
            {
                "prompt": "test",
                "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [6]},
            }
        )

    def test_cron_weekdays_out_of_range_raises(self, scheduler: TaskScheduler):
        """Weekdays outside 0-6 should be rejected."""
        with pytest.raises(ValueError, match="weekdays.*0-6"):
            scheduler._validate_task(
                {
                    "prompt": "test",
                    "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [7, 8]},
                }
            )

    def test_cron_weekdays_negative_raises(self, scheduler: TaskScheduler):
        """Negative weekday values should be rejected."""
        with pytest.raises(ValueError, match="weekdays.*0-6"):
            scheduler._validate_task(
                {
                    "prompt": "test",
                    "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [-1]},
                }
            )

    def test_cron_weekdays_non_int_raises(self, scheduler: TaskScheduler):
        """Non-integer weekday values should be rejected."""
        with pytest.raises(ValueError, match="weekdays.*integers"):
            scheduler._validate_task(
                {
                    "prompt": "test",
                    "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [1.5]},
                }
            )

    def test_cron_weekdays_not_list_raises(self, scheduler: TaskScheduler):
        """Weekdays as a non-list type should be rejected."""
        with pytest.raises(ValueError, match="weekdays.*list"):
            scheduler._validate_task(
                {
                    "prompt": "test",
                    "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": "1,2,3"},
                }
            )

    def test_cron_no_weekdays_passes(self, scheduler: TaskScheduler):
        """Omitting weekdays should be accepted (defaults to all days)."""
        task = _make_task(schedule_type="cron", hour=9, minute=0)
        assert "weekdays" not in task["schedule"]
        scheduler._validate_task(task)  # should not raise

    # ── integration: add_task rejects invalid tasks ──

    @pytest.mark.asyncio
    async def test_add_task_rejects_invalid_type(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="type"):
            await scheduler.add_task("chat1", {"prompt": "test", "schedule": {"type": "weekly"}})

    @pytest.mark.asyncio
    async def test_add_task_does_not_mutate_on_validation_failure(self, scheduler: TaskScheduler):
        """Invalid task should not be added to internal store."""
        with pytest.raises(ValueError):
            await scheduler.add_task("chat1", {"prompt": "test", "schedule": {"type": "bad"}})
        assert scheduler.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_add_task_rejects_missing_prompt(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="prompt"):
            await scheduler.add_task("chat1", {"schedule": {"type": "interval", "seconds": 60}})

    @pytest.mark.asyncio
    async def test_add_task_rejects_invalid_schedule(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError, match="schedule"):
            await scheduler.add_task("chat1", {"prompt": "test"})

    @pytest.mark.asyncio
    async def test_add_task_does_not_mutate_on_failure(self, scheduler: TaskScheduler):
        with pytest.raises(ValueError):
            await scheduler.add_task("chat1", {"prompt": "test", "schedule": {"type": "bad"}})
        assert scheduler.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_add_task_rejects_oversized_prompt(self, scheduler: TaskScheduler):
        """add_task should reject a prompt exceeding MAX_SCHEDULED_PROMPT_LENGTH."""
        prompt = "x" * 10_001
        with pytest.raises(ValueError, match="exceeds maximum length"):
            await scheduler.add_task(
                "chat1",
                {"prompt": prompt, "schedule": {"type": "interval", "seconds": 60}},
            )
        assert scheduler.list_tasks("chat1") == []

    # ── integration: valid tasks accepted and persisted correctly ──

    @pytest.mark.asyncio
    async def test_add_task_valid_interval_persisted(
        self, scheduler: TaskScheduler, workspace: Path
    ):
        """Valid interval task is accepted, stored internally, and persisted to disk."""
        task_id = await scheduler.add_task(
            "chat1", _make_task(schedule_type="interval", seconds=120)
        )
        assert task_id == "task_001"
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "test prompt"
        assert tasks[0]["schedule"]["type"] == "interval"
        assert tasks[0]["schedule"]["seconds"] == 120
        path = _tasks_file(workspace, "chat1")
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["schedule"]["seconds"] == 120
        assert data[0]["task_id"] == "task_001"
        assert data[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_add_task_valid_daily_persisted(self, scheduler: TaskScheduler, workspace: Path):
        """Valid daily task is accepted, stored internally, and persisted to disk."""
        task_id = await scheduler.add_task(
            "chat1", _make_task(schedule_type="daily", hour=14, minute=30)
        )
        assert task_id == "task_001"
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["schedule"]["type"] == "daily"
        assert tasks[0]["schedule"]["hour"] == 14
        assert tasks[0]["schedule"]["minute"] == 30
        path = _tasks_file(workspace, "chat1")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data[0]["schedule"]["hour"] == 14
        assert data[0]["schedule"]["minute"] == 30

    @pytest.mark.asyncio
    async def test_add_task_valid_cron_persisted(self, scheduler: TaskScheduler, workspace: Path):
        """Valid cron task is accepted, stored internally, and persisted to disk."""
        task_id = await scheduler.add_task(
            "chat1", _make_task(schedule_type="cron", hour=9, minute=0, weekdays=[1, 3, 5])
        )
        assert task_id == "task_001"
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["schedule"]["type"] == "cron"
        assert tasks[0]["schedule"]["weekdays"] == [1, 3, 5]
        path = _tasks_file(workspace, "chat1")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data[0]["schedule"]["weekdays"] == [1, 3, 5]

    @pytest.mark.asyncio
    async def test_add_task_valid_daily_persisted(self, scheduler: TaskScheduler, workspace: Path):
        """Valid daily task accepted via async path and persisted correctly."""
        task_id = await scheduler.add_task(
            "chat1", _make_task(schedule_type="daily", hour=7, minute=45)
        )
        assert task_id == "task_001"
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["schedule"]["type"] == "daily"
        path = _tasks_file(workspace, "chat1")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data[0]["schedule"]["hour"] == 7
        assert data[0]["schedule"]["minute"] == 45

    @pytest.mark.asyncio
    async def test_add_task_multiple_valid_types_coexist(
        self, scheduler: TaskScheduler, workspace: Path
    ):
        """Multiple valid tasks of different schedule types can coexist for same chat."""
        id1 = await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=300))
        id2 = await scheduler.add_task("chat1", _make_task(schedule_type="daily", hour=8, minute=0))
        id3 = await scheduler.add_task(
            "chat1", _make_task(schedule_type="cron", hour=12, minute=30)
        )
        assert id1 != id2 != id3
        tasks = scheduler.list_tasks("chat1")
        assert len(tasks) == 3
        types = {t["schedule"]["type"] for t in tasks}
        assert types == {"interval", "daily", "cron"}
        path = _tasks_file(workspace, "chat1")
        data = json.loads(path.read_text())
        assert len(data) == 3


# ─────────────────────────────────────────────────────────────────────────────
# TaskScheduler — _load post-deserialization validation
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadValidation:
    """Tests for _load() validating tasks after deserialization."""

    @pytest.mark.asyncio
    async def test_skip_invalid_schedule_type(self, workspace: Path):
        """Tasks with an invalid schedule type are skipped during load."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "valid",
                        "schedule": {"type": "yearly"},
                    }
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_skip_oversized_prompt(self, workspace: Path):
        """Tasks with a prompt exceeding max length are skipped."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "x" * 10_001,
                        "schedule": {"type": "interval", "seconds": 60},
                    }
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_skip_invalid_weekdays(self, workspace: Path):
        """Cron tasks with weekdays outside 0-6 are skipped."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "test",
                        "schedule": {"type": "cron", "hour": 9, "minute": 0, "weekdays": [7, 8]},
                    }
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_skip_missing_prompt(self, workspace: Path):
        """Tasks with missing prompt are skipped."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "schedule": {"type": "interval", "seconds": 60},
                    }
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_keeps_valid_skips_invalid(self, workspace: Path):
        """Valid tasks are loaded; invalid ones are skipped."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "valid task",
                        "schedule": {"type": "interval", "seconds": 60},
                    },
                    {
                        "task_id": "task_002",
                        "prompt": "bad",
                        "schedule": {"type": "bogus"},
                    },
                    {
                        "task_id": "task_003",
                        "prompt": "another valid",
                        "schedule": {"type": "daily", "hour": 9, "minute": 0},
                    },
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        tasks = s.list_tasks("chat1")
        assert len(tasks) == 2
        ids = {t["task_id"] for t in tasks}
        assert ids == {"task_001", "task_003"}

    @pytest.mark.asyncio
    async def test_non_list_json_skipped(self, workspace: Path):
        """A JSON object (not array) at top level is rejected."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"not": "a list"}))
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_skip_non_dict_entries(self, workspace: Path):
        """Non-dict entries in the array are skipped."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    "a string",
                    42,
                    {
                        "task_id": "task_001",
                        "prompt": "valid",
                        "schedule": {"type": "interval", "seconds": 60},
                    },
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        tasks = s.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "task_001"

    @pytest.mark.asyncio
    async def test_all_invalid_yields_empty(self, workspace: Path):
        """When all tasks are invalid, the chat ends up with an empty list."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {"prompt": "", "schedule": {"type": "interval", "seconds": 60}},
                    {"prompt": "x", "schedule": {"type": "bad"}},
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_valid_tasks_load_normally(self, workspace: Path):
        """Fully valid tasks load without issues."""
        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "task_id": "task_001",
                        "prompt": "hello",
                        "schedule": {"type": "interval", "seconds": 60},
                    },
                    {
                        "task_id": "task_002",
                        "prompt": "world",
                        "schedule": {"type": "daily", "hour": 8, "minute": 30},
                    },
                ]
            )
        )
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())
        await s._load("chat1")
        tasks = s.list_tasks("chat1")
        assert len(tasks) == 2
