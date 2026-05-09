"""
test_scheduler_vector_coverage.py — Additional coverage tests for scheduler and vector memory.

Targets untested code paths in:
  - scheduler/cron.py: time calculations, weekday logic
  - scheduler/persistence.py: file I/O, path resolution
  - vector_memory/health.py: health check, circuit breaker integration
  - vector_memory/batch.py: batch embedding logic
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.scheduler.cron import _now, _target_utc_time, _utc_offset_hours, TICK_SECONDS
from src.scheduler.persistence import (
    resolve_tasks_path,
    read_tasks_file,
    write_tasks_file,
    SCHEDULER_DIR,
    TASKS_FILE,
)


# ── scheduler/cron.py ──────────────────────────────────────────────────────


class TestCronTimeFunctions:
    """Tests for cron time calculation helpers."""

    def test_now_returns_utc_datetime(self):
        result = _now()
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_utc_offset_hours_is_float(self):
        offset = _utc_offset_hours()
        assert isinstance(offset, float)
        assert -12.0 <= offset <= 14.0

    def test_target_utc_time_daily(self):
        schedule = {"type": "daily", "hour": 9, "minute": 30}
        local_offset = 0.0  # UTC
        utc_hour, utc_minute = _target_utc_time(schedule, local_offset)
        assert utc_hour == 9
        assert utc_minute == 30

    def test_target_utc_time_with_positive_offset(self):
        schedule = {"type": "daily", "hour": 9, "minute": 0}
        local_offset = 3.0  # UTC+3
        utc_hour, utc_minute = _target_utc_time(schedule, local_offset)
        assert utc_hour == 6  # 9 - 3 = 6

    def test_target_utc_time_cron(self):
        schedule = {"type": "cron", "hour": 14, "minute": 0}
        local_offset = 0.0
        utc_hour, utc_minute = _target_utc_time(schedule, local_offset)
        assert utc_hour == 14
        assert utc_minute == 0

    def test_tick_seconds_is_positive(self):
        assert TICK_SECONDS > 0


# ── scheduler/persistence.py ────────────────────────────────────────────────


class TestSchedulerPersistence:
    """Tests for scheduler task file persistence."""

    def test_resolve_tasks_path_valid_chat(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        chat_dir = workspace / "chat_123"
        chat_dir.mkdir(parents=True)

        result = resolve_tasks_path(workspace, "chat_123")
        assert result is not None
        assert result.parent.name == SCHEDULER_DIR
        assert result.name == TASKS_FILE

    def test_write_and_read_tasks(self, tmp_path: Path) -> None:
        tasks = [
            {
                "task_id": "task_001",
                "prompt": "Check weather",
                "schedule": {"type": "daily", "hour": 9, "minute": 0},
                "enabled": True,
            }
        ]
        dest = tmp_path / "tasks.json"
        write_tasks_file(dest, tasks)

        assert dest.exists()
        raw = read_tasks_file(dest)
        assert raw is not None
        loaded = json.loads(raw)
        assert len(loaded) == 1
        assert loaded[0]["task_id"] == "task_001"

    def test_read_tasks_missing_file(self, tmp_path: Path) -> None:
        result = read_tasks_file(tmp_path / "missing.json")
        assert result is None

    def test_write_tasks_creates_parent_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "nested" / "dir" / "tasks.json"
        tasks = [{"task_id": "t1", "prompt": "Test"}]
        write_tasks_file(dest, tasks)
        assert dest.exists()


# ── vector_memory health and batch ──────────────────────────────────────────


class TestVectorMemoryHealth:
    """Tests for vector memory health monitoring."""

    def test_health_snapshot_structure(self) -> None:
        from src.vector_memory import VectorMemory

        vm = VectorMemory.__new__(VectorMemory)
        vm._embed_cache_size = 256
        vm._pending_retries = []
        vm._circuit_breaker = MagicMock()
        from src.utils.circuit_breaker import CircuitState
        vm._circuit_breaker.state = CircuitState.CLOSED

        snapshot = vm.health_snapshot()
        assert "embedding_api_healthy" in snapshot
        assert "retry_queue_depth" in snapshot
        assert "circuit_breaker_state" in snapshot

    def test_probe_embedding_model_success(self) -> None:
        from src.vector_memory import VectorMemory

        vm = VectorMemory.__new__(VectorMemory)
        vm._client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1] * 8)]
        vm._client.embeddings.create = AsyncMock(return_value=mock_resp)
        vm._embedding_model = "test-model"
        vm._mark_embedding_api_healthy = AsyncMock()

        # probe_embedding_model is async
        result = asyncio.get_event_loop().run_until_complete(
            vm.probe_embedding_model(timeout=5.0)
        )
        success, msg = result
        assert success is True
        assert "dims=8" in msg

    def test_probe_embedding_model_timeout(self) -> None:
        from src.vector_memory import VectorMemory

        vm = VectorMemory.__new__(VectorMemory)
        vm._client = AsyncMock()
        vm._client.embeddings.create = AsyncMock(side_effect=asyncio.TimeoutError())
        vm._embedding_model = "test-model"
        vm._mark_embedding_api_unhealthy = AsyncMock()

        result = asyncio.get_event_loop().run_until_complete(
            vm.probe_embedding_model(timeout=1.0)
        )
        success, msg = result
        assert success is False
        assert "Timeout" in msg


class TestVectorMemoryCacheKey:
    """Tests for embedding cache key utility."""

    def test_cache_key_deterministic(self) -> None:
        from src.vector_memory._utils import _cache_key

        assert _cache_key("hello") == _cache_key("hello")

    def test_cache_key_different_for_different_text(self) -> None:
        from src.vector_memory._utils import _cache_key

        assert _cache_key("hello") != _cache_key("world")

    def test_cache_key_is_string(self) -> None:
        from src.vector_memory._utils import _cache_key

        result = _cache_key("test text")
        assert isinstance(result, str)
        assert len(result) > 0
