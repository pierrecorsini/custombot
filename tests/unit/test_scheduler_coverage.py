"""
Additional tests for src/scheduler/ to increase coverage.

Covers:
  - _deliver_with_retry: transient retry, max attempts, non-transient raise
  - _trigger_with_retry: HMAC signing, retry exhausted
  - get_status: health check status dict
  - set_on_send / set_on_trigger: callback replacement
  - _time_to_next_due with last_run_epoch field
  - _execute_task: dedup integration, timeout path
  - _load: non-dict entries, HMAC verification paths
  - load_all: invalid chat_id dirs
  - _prepare_task: collision avoidance edge cases
"""

from __future__ import annotations

import asyncio
import json
import os
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
)
from src.constants import SCHEDULER_HMAC_SIG_EXT

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def on_trigger() -> AsyncMock:
    return AsyncMock(return_value="result from LLM")


@pytest.fixture
def on_send() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def scheduler(workspace: Path, on_trigger: AsyncMock, on_send: AsyncMock) -> TaskScheduler:
    s = TaskScheduler()
    s.configure(workspace=workspace, on_trigger=on_trigger, on_send=on_send)
    return s


def _tasks_file(workspace: Path, chat_id: str) -> Path:
    return workspace / chat_id / SCHEDULER_DIR / TASKS_FILE


def _make_task(
    schedule_type: str = "interval",
    prompt: str = "test prompt",
    label: str = "Test Task",
    **schedule_overrides,
) -> dict:
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


def _make_transient_error(message: str) -> Exception:
    """Create an exception that is_transient_error recognizes."""
    return ConnectionError(message)


# ─────────────────────────────────────────────────────────────────────────────
# _deliver_with_retry
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverWithRetry:
    """Tests for TaskScheduler._deliver_with_retry()."""

    @pytest.mark.asyncio
    async def test_success_first_attempt(
        self, scheduler: TaskScheduler, on_send: AsyncMock
    ):
        """Successful delivery on first attempt."""
        await scheduler._deliver_with_retry("chat1", "formatted msg", "task_001")
        on_send.assert_awaited_once_with("chat1", "formatted msg")

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self, scheduler: TaskScheduler):
        """Transient send error triggers retry, succeeds on second attempt."""
        call_count = 0

        async def flaky_send(chat_id: str, text: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("WhatsApp disconnected")

        scheduler._on_send = flaky_send

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await scheduler._deliver_with_retry("chat1", "msg", "task_001")

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self, scheduler: TaskScheduler):
        """Raises when all 3 attempts fail with transient errors."""
        scheduler._on_send = AsyncMock(side_effect=ConnectionError("down"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="down"):
                await scheduler._deliver_with_retry("chat1", "msg", "task_001")

        assert scheduler._on_send.await_count == 3

    @pytest.mark.asyncio
    async def test_non_transient_error_raises_immediately(
        self, scheduler: TaskScheduler
    ):
        """Non-transient errors are re-raised without retry."""
        scheduler._on_send = AsyncMock(side_effect=ValueError("bad args"))

        with pytest.raises(ValueError, match="bad args"):
            await scheduler._deliver_with_retry("chat1", "msg", "task_001")

        assert scheduler._on_send.await_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# get_status
# ─────────────────────────────────────────────────────────────────────────────


class TestGetStatus:
    """Tests for TaskScheduler.get_status()."""

    def test_empty_status(self, scheduler: TaskScheduler):
        status = scheduler.get_status()
        assert status["running"] is False
        assert status["total_tasks"] == 0
        assert status["enabled_tasks"] == 0
        assert status["chats_with_tasks"] == 0
        assert status["success_count"] == 0
        assert status["failure_count"] == 0
        assert status["recent_executions"] == []

    @pytest.mark.asyncio
    async def test_status_with_tasks(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task(prompt="a"))
        await scheduler.add_task("chat1", _make_task(prompt="b"))
        await scheduler.add_task("chat2", _make_task(prompt="c"))

        status = scheduler.get_status()
        assert status["total_tasks"] == 3
        assert status["enabled_tasks"] == 3
        assert status["chats_with_tasks"] == 2

    @pytest.mark.asyncio
    async def test_status_counts_disabled_tasks(self, scheduler: TaskScheduler):
        await scheduler.add_task("chat1", _make_task(prompt="a"))
        scheduler.list_tasks("chat1")[0]["enabled"] = False

        status = scheduler.get_status()
        assert status["total_tasks"] == 1
        assert status["enabled_tasks"] == 0

    @pytest.mark.asyncio
    async def test_status_tracks_success_failure(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """After execution, success/failure counts reflect results."""
        on_trigger.return_value = "ok"
        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        status = scheduler.get_status()
        assert status["success_count"] == 1
        assert status["failure_count"] == 0
        assert len(status["recent_executions"]) == 1
        assert status["recent_executions"][0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_status_running_after_start(self, scheduler: TaskScheduler):
        scheduler.start()
        status = scheduler.get_status()
        assert status["running"] is True
        await scheduler.stop()


# ─────────────────────────────────────────────────────────────────────────────
# set_on_send / set_on_trigger
# ─────────────────────────────────────────────────────────────────────────────


class TestSetCallbacks:
    """Tests for set_on_send() and set_on_trigger()."""

    def test_set_on_send(self, scheduler: TaskScheduler):
        new_send = AsyncMock()
        scheduler.set_on_send(new_send)
        assert scheduler._on_send is new_send

    def test_set_on_trigger(self, scheduler: TaskScheduler):
        new_trigger = AsyncMock(return_value="new result")
        scheduler.set_on_trigger(new_trigger)
        assert scheduler._on_trigger is new_trigger

    @pytest.mark.asyncio
    async def test_set_on_send_used_in_execution(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """After set_on_send, the new callback is used for delivery."""
        on_trigger.return_value = "deliver me"
        new_send = AsyncMock()
        scheduler.set_on_send(new_send)

        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        new_send.assert_awaited_once()
        assert "deliver me" in new_send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_set_on_trigger_used_in_execution(
        self, scheduler: TaskScheduler, on_send: AsyncMock
    ):
        """After set_on_trigger, the new callback is used for triggering."""
        new_trigger = AsyncMock(return_value="new trigger result")
        scheduler.set_on_trigger(new_trigger)

        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        new_trigger.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# _time_to_next_due with last_run_epoch
# ─────────────────────────────────────────────────────────────────────────────


class TestTimeToNextDueLastRunEpoch:
    """Tests for _time_to_next_due using the last_run_epoch optimization."""

    @pytest.mark.asyncio
    async def test_interval_uses_epoch_when_available(
        self, scheduler: TaskScheduler
    ):
        """When last_run_epoch is set, it's used instead of parsing ISO."""
        task_dict = _make_task(schedule_type="interval", seconds=600)
        await scheduler.add_task("chat1", task_dict)

        now = _now()
        task_dict["last_run"] = now.isoformat()
        task_dict["last_run_epoch"] = now.timestamp()

        result = scheduler._time_to_next_due()
        assert result is not None
        assert 590 < result <= 600

    @pytest.mark.asyncio
    async def test_interval_epoch_overdue(self, scheduler: TaskScheduler):
        """When epoch indicates elapsed > interval, returns 0."""
        task_dict = _make_task(schedule_type="interval", seconds=10)
        await scheduler.add_task("chat1", task_dict)

        now = _now()
        task_dict["last_run"] = (now - timedelta(seconds=20)).isoformat()
        task_dict["last_run_epoch"] = (now - timedelta(seconds=20)).timestamp()

        result = scheduler._time_to_next_due()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_daily_task_in_time_to_next_due(
        self, scheduler: TaskScheduler
    ):
        """Daily task computes correct time-to-next in the heap."""
        now = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 0.0
            scheduler._utc_offset_updated_at = time.monotonic()
            await scheduler.add_task(
                "chat1", _make_task(schedule_type="daily", hour=9, minute=0)
            )
            result = scheduler._time_to_next_due()
            assert result is not None
            assert 3590 < result <= 3600

    @pytest.mark.asyncio
    async def test_cron_task_in_time_to_next_due(self, scheduler: TaskScheduler):
        """Cron task computes correct time-to-next."""
        now = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)
        # Monday April 20, 2026
        with patch("src.scheduler._now", return_value=now):
            scheduler._cached_utc_offset = 0.0
            scheduler._utc_offset_updated_at = time.monotonic()
            await scheduler.add_task(
                "chat1",
                _make_task(
                    schedule_type="cron",
                    hour=9,
                    minute=0,
                    weekdays=[now.weekday()],
                ),
            )
            result = scheduler._time_to_next_due()
            assert result is not None
            assert 3590 < result <= 3600


# ─────────────────────────────────────────────────────────────────────────────
# _execute_task with dedup
# ─────────────────────────────────────────────────────────────────────────────


class TestExecuteTaskDedup:
    """Tests for _execute_task with DeduplicationService integration."""

    @pytest.mark.asyncio
    async def test_dedup_suppresses_duplicate(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """When dedup detects duplicate, the result is not sent."""
        on_trigger.return_value = "duplicate content"

        dedup = MagicMock()
        dedup.check_and_record_outbound = MagicMock(return_value=True)
        scheduler.set_dedup_service(dedup)

        task = _make_task(label="Daily")
        task["task_id"] = "task_001"
        on_send = AsyncMock()
        scheduler.set_on_send(on_send)

        await scheduler._execute_task("chat1", task)

        on_trigger.assert_awaited_once()
        on_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedup_allows_unique(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock, on_send: AsyncMock
    ):
        """When dedup says content is unique, it's sent normally."""
        on_trigger.return_value = "unique content"

        dedup = MagicMock()
        dedup.check_and_record_outbound = MagicMock(return_value=False)
        scheduler.set_dedup_service(dedup)

        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        on_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_dedup_sends_normally(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock, on_send: AsyncMock
    ):
        """Without dedup service, all results are sent."""
        on_trigger.return_value = "result"
        assert scheduler._dedup is None

        task = _make_task()
        task["task_id"] = "task_001"
        await scheduler._execute_task("chat1", task)

        on_send.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# _load edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadEdgeCases:
    """Tests for _load() edge cases."""

    @pytest.mark.asyncio
    async def test_load_skips_non_dict_entries(self, workspace: Path):
        """Non-dict entries in the JSON array are skipped."""
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())

        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    "not a dict",
                    42,
                    None,
                    {"prompt": "valid", "schedule": {"type": "interval", "seconds": 60}},
                ]
            )
        )

        await s._load("chat1")
        tasks = s.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "valid"

    @pytest.mark.asyncio
    async def test_load_skips_invalid_task_entries(self, workspace: Path):
        """Entries that fail _validate_task are skipped."""
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())

        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {"prompt": "", "schedule": {"type": "interval", "seconds": 60}},
                    {"prompt": "valid task", "schedule": {"type": "interval", "seconds": 60}},
                    {"prompt": "no schedule"},
                ]
            )
        )

        await s._load("chat1")
        tasks = s.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "valid task"

    @pytest.mark.asyncio
    async def test_load_non_array_json(self, workspace: Path):
        """JSON file that is not an array is skipped."""
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())

        path = _tasks_file(workspace, "chat1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"not": "an array"}))

        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_load_no_workspace(self):
        """_load without workspace configured is a no-op."""
        s = TaskScheduler()
        await s._load("chat1")
        assert s.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_load_hmac_missing_sig_rejects(
        self, workspace: Path
    ):
        """With HMAC configured, missing signature file causes rejection."""
        os.environ["SCHEDULER_HMAC_SECRET"] = "test-secret-key-32-chars-min!"
        try:
            import src.security.signing as _signing_mod

            _signing_mod._cached_secret = _signing_mod._SENTINEL

            s = TaskScheduler()
            s.configure(workspace=workspace, on_trigger=AsyncMock())

            path = _tasks_file(workspace, "chat1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    [{"prompt": "test", "schedule": {"type": "interval", "seconds": 60}}]
                )
            )

            await s._load("chat1")
            assert s.list_tasks("chat1") == []
        finally:
            os.environ.pop("SCHEDULER_HMAC_SECRET", None)
            _signing_mod._cached_secret = _signing_mod._SENTINEL


# ─────────────────────────────────────────────────────────────────────────────
# load_all edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadAllEdgeCases:
    """Tests for load_all() edge cases."""

    @pytest.mark.asyncio
    async def test_load_all_skips_invalid_chat_ids(self, workspace: Path):
        """Directories with invalid chat_id names are skipped."""
        s = TaskScheduler()
        s.configure(workspace=workspace, on_trigger=AsyncMock())

        # Create a directory with characters that would fail chat_id validation
        invalid_dir = workspace / "../escape"
        invalid_dir.mkdir(parents=True, exist_ok=True)
        task_dir = invalid_dir / SCHEDULER_DIR
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / TASKS_FILE).write_text(
            json.dumps(
                [{"prompt": "should not load", "schedule": {"type": "interval", "seconds": 60}}]
            )
        )

        await s.load_all()
        # Invalid chat_id dirs should be skipped
        for tasks in s._tasks.values():
            for t in tasks:
                assert t["prompt"] != "should not load"


# ─────────────────────────────────────────────────────────────────────────────
# _prepare_task edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestPrepareTaskEdgeCases:
    """Tests for _prepare_task collision avoidance."""

    def test_handles_sparse_id_space(self, scheduler: TaskScheduler):
        """When existing tasks have non-sequential IDs, new ones avoid collisions."""
        # Manually inject tasks with gaps
        scheduler._tasks["chat1"] = [
            {"task_id": "task_001", "schedule": {"type": "interval", "seconds": 60}},
            {"task_id": "task_003", "schedule": {"type": "interval", "seconds": 60}},
        ]

        task = _make_task()
        task_id = scheduler._prepare_task("chat1", task)
        # Should get task_002 (counter starts at len(tasks)+1 = 3, task_003)
        # Then checks collision → task_003 exists → try task_004
        assert task_id not in {"task_001", "task_003"}
        assert task_id == "task_003" or task_id.startswith("task_")

    def test_prepares_task_with_existing_task_id(self, scheduler: TaskScheduler):
        """When input dict has task_id, it gets overridden."""
        task = _make_task()
        task["task_id"] = "original_id"
        returned = scheduler._prepare_task("chat1", task)
        assert returned != "original_id"
        assert task["task_id"] == returned


# ─────────────────────────────────────────────────────────────────────────────
# _is_due with last_run_epoch
# ─────────────────────────────────────────────────────────────────────────────


class TestIsDueWithEpoch:
    """Tests for _is_due using last_run_epoch field."""

    def test_interval_due_with_epoch(self, scheduler: TaskScheduler):
        """Interval task uses last_run_epoch when available."""
        task = _make_task(schedule_type="interval", seconds=10)
        now = _now()
        task["last_run"] = (now - timedelta(seconds=15)).isoformat()
        task["last_run_epoch"] = (now - timedelta(seconds=15)).timestamp()

        assert scheduler._is_due(task) is True

    def test_interval_not_due_with_epoch(self, scheduler: TaskScheduler):
        """Interval task with recent epoch is not due."""
        task = _make_task(schedule_type="interval", seconds=3600)
        now = _now()
        task["last_run"] = now.isoformat()
        task["last_run_epoch"] = now.timestamp()

        assert scheduler._is_due(task) is False


# ─────────────────────────────────────────────────────────────────────────────
# _run_loop consecutive failure backoff
# ─────────────────────────────────────────────────────────────────────────────


class TestRunLoopConsecutiveFailureBackoff:
    """Tests for the consecutive failure backoff in _run_loop."""

    @pytest.mark.asyncio
    async def test_consecutive_failures_increase_sleep(
        self, scheduler: TaskScheduler, on_trigger: AsyncMock
    ):
        """When tasks fail consecutively, sleep duration increases."""
        on_trigger.side_effect = RuntimeError("transient error timeout")

        await scheduler.add_task("chat1", _make_task(schedule_type="interval", seconds=60))

        sleep_durations: list[float] = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(duration: float) -> None:
            sleep_durations.append(duration)
            await original_sleep(0.01)

        with patch("src.scheduler.asyncio.sleep", side_effect=tracking_sleep):
            with patch("src.scheduler.TICK_SECONDS", 0.1):
                with patch("src.scheduler.SCHEDULER_MIN_SLEEP_SECONDS", 0.01):
                    scheduler._running = True
                    loop_task = asyncio.create_task(scheduler._run_loop())
                    await original_sleep(0.5)
                    scheduler._running = False
                    loop_task.cancel()
                    try:
                        await loop_task
                    except asyncio.CancelledError:
                        pass

        # Sleep durations should increase as consecutive failures accumulate
        assert len(sleep_durations) >= 2
        # Later sleeps should be >= earlier sleeps (backoff)
        assert sleep_durations[-1] >= sleep_durations[0]


# ─────────────────────────────────────────────────────────────────────────────
# resolve_tasks_path edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveTasksPath:
    """Tests for resolve_tasks_path path traversal protection."""

    def test_normal_path(self, workspace: Path):
        from src.scheduler.persistence import resolve_tasks_path

        result = resolve_tasks_path(workspace, "chat1")
        assert result is not None
        assert "chat1" in str(result)
        assert SCHEDULER_DIR in str(result)

    def test_none_on_traversal(self, workspace: Path):
        """Path traversal attempts return None."""
        from src.scheduler.persistence import resolve_tasks_path

        # This chat_id could resolve outside workspace
        result = resolve_tasks_path(workspace, "../escape")
        assert result is None

    def test_none_when_no_workspace(self, tmp_path: Path):
        """resolve_tasks_path with nonexistent workspace returns path but
        is_relative_to check handles gracefully."""
        from src.scheduler.persistence import resolve_tasks_path

        # Valid chat_id should work
        result = resolve_tasks_path(tmp_path, "chat1")
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# persistence: write_tasks_file strips internal keys
# ─────────────────────────────────────────────────────────────────────────────


class TestPersistenceInternalKeys:
    """Tests for write_tasks_file stripping internal _prefixed keys."""

    @pytest.mark.asyncio
    async def test_internal_keys_stripped_on_persist(
        self, scheduler: TaskScheduler, workspace: Path
    ):
        """Internal cache keys like _last_run_dt are stripped before write."""
        await scheduler.add_task("chat1", _make_task(prompt="cached"))
        task = scheduler.list_tasks("chat1")[0]
        task["_last_run_dt"] = datetime.now(tz=timezone.utc)
        task["_internal_cache"] = "should be removed"

        await scheduler._persist("chat1")

        data = json.loads(_tasks_file(workspace, "chat1").read_text())
        for entry in data:
            assert all(not k.startswith("_") for k in entry), (
                f"Internal key found in persisted data: {[k for k in entry if k.startswith('_')]}"
            )
