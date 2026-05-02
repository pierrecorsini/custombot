"""
test_config_watcher.py — Integration tests for ConfigWatcher hot-reload pipeline.

Verifies:
  1. Writing a valid updated config to disk triggers the applier callback
     with the new config value.
  2. Writing malformed JSON to disk does NOT crash the watcher loop — the
     watcher logs an error and keeps polling.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.bot import BotConfig
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig, save_config
from src.config.config_watcher import ConfigChangeApplier, ConfigWatcher
from src.shutdown import GracefulShutdown


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _base_config(tmp_path: Path, **llm_overrides) -> Config:
    """Build a valid Config pointing at *tmp_path* workspace."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-watcher",
            **llm_overrides,
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
        ),
        skills_auto_load=False,
    )


def _make_applier(config: Config):
    """Wire up a ConfigChangeApplier with mock components."""
    mock_bot = MagicMock()
    mock_bot._cfg = BotConfig(
        max_tool_iterations=10,
        memory_max_history=100,
        system_prompt_prefix="",
        stream_response=False,
    )

    mock_channel = MagicMock()
    mock_channel.apply_channel_config = MagicMock()

    mock_llm = MagicMock()
    mock_llm._cfg = config.llm

    shutdown_mgr = GracefulShutdown(timeout=30.0)
    reconfigure_logging = MagicMock()

    return ConfigChangeApplier(
        app_config=config,
        bot=mock_bot,
        channel=mock_channel,
        llm=mock_llm,
        shutdown_mgr=shutdown_mgr,
        reconfigure_logging=reconfigure_logging,
    )


def _write_raw(path: Path, content: str) -> None:
    """Write raw string content to *path*, bypassing save_config validation."""
    path.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# # ────────────────────────────────────────────────────────────────────────────


class TestConfigWatcherCallback:
    """Verify that ConfigWatcher invokes the applier with the new config."""

    @pytest.mark.asyncio
    async def test_valid_change_triggers_callback(self, tmp_path: Path) -> None:
        """
        Writing a valid config with a changed safe field (``memory_max_history``)
        triggers ``applier.apply()`` with the old and new configs, and the
        new value is reflected on the live component.
        """
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        # Track that apply() was called and capture arguments
        original_apply = applier.apply
        apply_calls: list[tuple[Config, Config]] = []

        def _tracking_apply(old: Config, new: Config) -> None:
            apply_calls.append((old, new))
            original_apply(old, new)

        applier.apply = _tracking_apply  # type: ignore[assignment]

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # Sanity: no calls yet
            assert len(apply_calls) == 0

            # Write updated config — change memory_max_history (safe field)
            updated = _base_config(tmp_path, max_tool_iterations=5)
            save_config(updated, config_path)

            # Wait for detection + apply
            await asyncio.sleep(0.5)

            # Assert: apply() was called at least once
            assert len(apply_calls) >= 1, (
                "applier.apply() was never called — watcher did not detect the change"
            )

            old_cfg, new_cfg = apply_calls[0]
            assert old_cfg is initial
            assert new_cfg.llm.max_tool_iterations == 5

            # The bot component should reflect the new value
            bot: MagicMock = applier._bot  # type: ignore[assignment]
            assert bot._cfg.max_tool_iterations == 5
        finally:
            await watcher.stop()


class TestConfigWatcherMalformedJSON:
    """Verify that malformed JSON does not crash the watcher loop."""

    @pytest.mark.asyncio
    async def test_malformed_json_does_not_crash_watcher(
        self, tmp_path: Path
    ) -> None:
        """
        Writing malformed JSON (e.g. ``{invalid}``) to the config file causes
        the watcher to log an error but continue polling. A subsequent valid
        write is still detected and applied.
        """
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        # Track apply calls
        original_apply = applier.apply
        apply_calls: list[tuple[Config, Config]] = []

        def _tracking_apply(old: Config, new: Config) -> None:
            apply_calls.append((old, new))
            original_apply(old, new)

        applier.apply = _tracking_apply  # type: ignore[assignment]

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # ── Step 1: Write malformed JSON ──
            _write_raw(config_path, "{invalid json!!!")

            await asyncio.sleep(0.5)

            # Assert: watcher did NOT call apply (malformed config rejected)
            assert len(apply_calls) == 0, (
                "applier.apply() should not have been called for malformed JSON"
            )

            # Assert: watcher is still running
            assert watcher._running, "Watcher loop crashed after malformed JSON"

            # ── Step 2: Write a valid updated config ──
            updated = _base_config(tmp_path, max_tool_iterations=3)
            save_config(updated, config_path)

            await asyncio.sleep(0.5)

            # Assert: watcher recovered and detected the valid change
            assert len(apply_calls) >= 1, (
                "applier.apply() was never called after recovery — "
                "watcher did not resume polling after malformed JSON"
            )

            _, new_cfg = apply_calls[0]
            assert new_cfg.llm.max_tool_iterations == 3
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_malformed_json_keeps_current_config(
        self, tmp_path: Path, caplog
    ) -> None:
        """
        When the watcher encounters malformed JSON, the current (last-known-good)
        config remains in effect — no fields are changed on live components.
        """
        import logging

        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        bot_before = applier._bot._cfg.max_tool_iterations

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            _write_raw(config_path, "not json at all")

            with caplog.at_level(logging.ERROR, logger="src.config.config_watcher"):
                await asyncio.sleep(0.5)

            # Bot config must be unchanged
            assert applier._bot._cfg.max_tool_iterations == bot_before

            # Watcher should have logged an error about the invalid config
            assert any(
                "Config hot-reload failed" in rec.message
                for rec in caplog.records
            ), (
                "Expected an error log about config hot-reload failure, "
                f"got: {[rec.message for rec in caplog.records]}"
            )
        finally:
            await watcher.stop()
