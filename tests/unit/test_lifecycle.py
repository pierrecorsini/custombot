"""
tests/unit/test_lifecycle.py — Tests for lifecycle logging helpers.

Covers verbosity-gated logging, shutdown ordering, and uptime computation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.lifecycle import (
    _log_cleanup_step,
    _log_component_init,
    _log_component_ready,
    _log_shutdown_begin,
    _log_shutdown_complete,
    _log_skills_loaded,
    _log_startup_begin,
    _log_startup_complete,
    perform_shutdown,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_config(**overrides: Any) -> MagicMock:
    """Create a mock Config object for logging tests."""
    cfg = MagicMock()
    cfg.llm.model = overrides.get("model", "test-model")
    cfg.llm.base_url = overrides.get("base_url", "http://localhost")
    cfg.llm.api_key = overrides.get("api_key", "sk-test")
    cfg.whatsapp.provider = overrides.get("provider", "test")
    cfg.whatsapp.neonize.db_path = overrides.get("db_path", "/tmp/db")
    cfg.memory_max_history = overrides.get("memory_max_history", 50)
    cfg.skills_auto_load = overrides.get("skills_auto_load", True)
    cfg.skills_user_directory = overrides.get("skills_user_directory", "skills")
    cfg.log_format = overrides.get("log_format", "text")
    cfg.shutdown_timeout = overrides.get("shutdown_timeout", 10)
    return cfg


# ── _log_startup_begin ───────────────────────────────────────────────────────


class TestLogStartupBegin:
    def test_returns_float(self):
        cfg = _make_config()
        with patch("src.lifecycle._get_verbosity", return_value="normal"):
            result = _log_startup_begin(cfg)
        assert isinstance(result, float)
        assert result > 0

    def test_quiet_mode_returns_time(self):
        cfg = _make_config()
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            result = _log_startup_begin(cfg)
        assert isinstance(result, float)


# ── _log_startup_complete ────────────────────────────────────────────────────


class TestLogStartupComplete:
    def test_computes_duration(self):
        start = time.time() - 1.5  # 1.5 seconds ago
        with patch("src.lifecycle._get_verbosity", return_value="normal"):
            # Should not raise
            _log_startup_complete(start, ["db", "llm", "channel"])

    def test_quiet_mode_no_output(self, caplog):
        start = time.time()
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_startup_complete(start, ["db"])
        # Quiet mode should not log
        assert not caplog.records


# ── _log_shutdown_begin ──────────────────────────────────────────────────────


class TestLogShutdownBegin:
    def test_logs_metrics(self, caplog):
        metrics = {
            "uptime": 120.5,
            "messages_processed": 42,
            "skills_executed": 10,
            "errors_count": 2,
        }
        with patch("src.lifecycle._get_verbosity", return_value="normal"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_shutdown_begin(metrics)
        assert any("Shutdown" in r.message for r in caplog.records)

    def test_quiet_mode_silent(self, caplog):
        metrics = {"uptime": 10}
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_shutdown_begin(metrics)
        assert not caplog.records


# ── _log_cleanup_step ────────────────────────────────────────────────────────


class TestLogCleanupStep:
    def test_verbose_logs_step(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="verbose"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_cleanup_step(1, 3, "Stopping things")
        assert any("CLEANUP" in r.message for r in caplog.records)

    def test_quiet_mode_silent(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_cleanup_step(1, 3, "Stopping things")
        assert not caplog.records


# ── _log_component_init / _log_component_ready ──────────────────────────────


class TestLogComponentInit:
    def test_verbose_logs_component(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="verbose"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_component_init("Database")
        assert any("DATABASE" in r.message for r in caplog.records)

    def test_quiet_silent(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_component_init("Database")
        assert not caplog.records


class TestLogComponentReady:
    def test_verbose_with_details(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="verbose"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_component_ready("LLM", "gpt-4")
        assert any("READY" in r.message and "gpt-4" in r.message for r in caplog.records)

    def test_verbose_without_details(self, caplog):
        with patch("src.lifecycle._get_verbosity", return_value="verbose"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_component_ready("LLM")
        assert any("READY" in r.message for r in caplog.records)


# ── _log_skills_loaded ──────────────────────────────────────────────────────


class TestLogSkillsLoaded:
    def test_logs_skill_count(self, caplog):
        mock_reg = MagicMock()
        skill1 = MagicMock()
        skill1.name = "bash"
        skill1.description = "Run bash commands"
        skill2 = MagicMock()
        skill2.name = "search"
        skill2.description = "Search the web"
        mock_reg.all.return_value = [skill1, skill2]

        with patch("src.lifecycle._get_verbosity", return_value="normal"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_skills_loaded(mock_reg)
        assert any("2" in r.message for r in caplog.records)

    def test_quiet_silent(self, caplog):
        mock_reg = MagicMock()
        mock_reg.all.return_value = []
        with patch("src.lifecycle._get_verbosity", return_value="quiet"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_skills_loaded(mock_reg)
        assert not caplog.records


# ── _log_shutdown_complete ───────────────────────────────────────────────────


class TestLogShutdownComplete:
    def test_logs_duration(self, caplog):
        start = time.time() - 0.5
        with patch("src.lifecycle._get_verbosity", return_value="normal"):
            with caplog.at_level(logging.INFO, logger="lifecycle"):
                _log_shutdown_complete(start)
        assert any("Shutdown complete" in r.message for r in caplog.records)


# ── perform_shutdown ordering ────────────────────────────────────────────────


class TestPerformShutdown:
    @pytest.fixture()
    def shutdown_mocks(self):
        """Create all mocks needed for perform_shutdown."""
        shutdown = MagicMock()
        shutdown.request_shutdown = MagicMock()
        shutdown.wait_for_in_flight = AsyncMock(return_value=True)

        channel = MagicMock()
        channel.request_shutdown = MagicMock()
        channel.close = AsyncMock()

        scheduler = MagicMock()
        scheduler.stop = AsyncMock()

        health_server = MagicMock()
        health_server.stop = AsyncMock()

        db = MagicMock()
        db.close = AsyncMock()

        vector_memory = MagicMock()
        project_store = MagicMock()

        message_queue = MagicMock()
        message_queue.close = AsyncMock()

        llm = MagicMock()
        llm.close = AsyncMock()

        return {
            "shutdown": shutdown,
            "channel": channel,
            "scheduler": scheduler,
            "health_server": health_server,
            "db": db,
            "vector_memory": vector_memory,
            "project_store": project_store,
            "message_queue": message_queue,
            "llm": llm,
        }

    @pytest.mark.asyncio()
    async def test_shutdown_calls_all_steps(self, shutdown_mocks):
        session_metrics = {"start_time": time.time() - 10}
        m = shutdown_mocks

        await perform_shutdown(
            shutdown=m["shutdown"],
            channel=m["channel"],
            scheduler=m["scheduler"],
            health_server=m["health_server"],
            db=m["db"],
            vector_memory=m["vector_memory"],
            project_store=m["project_store"],
            message_queue=m["message_queue"],
            llm=m["llm"],
            session_metrics=session_metrics,
            log=logging.getLogger("test"),
        )

        # Verify all components were shut down
        m["shutdown"].request_shutdown.assert_called_once()
        m["channel"].request_shutdown.assert_called_once()
        m["shutdown"].wait_for_in_flight.assert_awaited_once()
        m["scheduler"].stop.assert_awaited_once()
        m["health_server"].stop.assert_awaited_once()
        m["channel"].close.assert_awaited_once()
        m["db"].close.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_shutdown_computes_uptime_if_missing(self, shutdown_mocks):
        session_metrics = {"start_time": time.time() - 30}
        m = shutdown_mocks

        await perform_shutdown(
            shutdown=m["shutdown"],
            channel=m["channel"],
            scheduler=m["scheduler"],
            health_server=m["health_server"],
            db=m["db"],
            vector_memory=m["vector_memory"],
            project_store=m["project_store"],
            message_queue=m["message_queue"],
            llm=m["llm"],
            session_metrics=session_metrics,
            log=logging.getLogger("test"),
        )

        assert "uptime" in session_metrics
        assert session_metrics["uptime"] > 0

    @pytest.mark.asyncio()
    async def test_shutdown_tolerates_failures(self, shutdown_mocks):
        """Shutdown should not raise even if components fail."""
        m = shutdown_mocks
        m["channel"].close = AsyncMock(side_effect=RuntimeError("boom"))
        m["scheduler"].stop = AsyncMock(side_effect=RuntimeError("boom"))
        m["db"].close = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise
        await perform_shutdown(
            shutdown=m["shutdown"],
            channel=m["channel"],
            scheduler=m["scheduler"],
            health_server=m["health_server"],
            db=m["db"],
            vector_memory=m["vector_memory"],
            project_store=m["project_store"],
            message_queue=m["message_queue"],
            llm=m["llm"],
            session_metrics={"uptime": 5},
            log=logging.getLogger("test"),
        )

    @pytest.mark.asyncio()
    async def test_shutdown_without_health_server(self, shutdown_mocks):
        m = shutdown_mocks
        # health_server=None should be handled
        await perform_shutdown(
            shutdown=m["shutdown"],
            channel=m["channel"],
            scheduler=m["scheduler"],
            health_server=None,
            db=m["db"],
            vector_memory=m["vector_memory"],
            project_store=m["project_store"],
            message_queue=m["message_queue"],
            llm=m["llm"],
            session_metrics={"uptime": 5},
            log=logging.getLogger("test"),
        )
        # Should complete without error
        m["db"].close.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_db_closed_last(self, shutdown_mocks):
        """Database should be the last component closed."""
        m = shutdown_mocks
        call_order = []

        m["channel"].close = AsyncMock(side_effect=lambda: call_order.append("channel"))
        m["scheduler"].stop = AsyncMock(side_effect=lambda: call_order.append("scheduler"))
        m["db"].close = AsyncMock(side_effect=lambda: call_order.append("db"))

        await perform_shutdown(
            shutdown=m["shutdown"],
            channel=m["channel"],
            scheduler=m["scheduler"],
            health_server=None,
            db=m["db"],
            vector_memory=m["vector_memory"],
            project_store=m["project_store"],
            message_queue=m["message_queue"],
            llm=m["llm"],
            session_metrics={"uptime": 5},
            log=logging.getLogger("test"),
        )

        # DB should be the last call
        assert call_order[-1] == "db"
