"""
test_config_hotreload_e2e.py — End-to-end integration tests for config hot-reload.

Tests the full pipeline: config file change on disk → ConfigWatcher detection →
component reconfiguration with new values.  Each test exercises the complete
watcher lifecycle (start → modify → verify → stop) against a real temp file.

Mirrors the helpers and mocking approach from ``test_config_watcher.py``.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from src.bot import BotConfig
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig, save_config
from src.config.config_watcher import ConfigChangeApplier, ConfigWatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (mirrors test_config_watcher.py)
# ─────────────────────────────────────────────────────────────────────────────


def _base_config(tmp_path: Path, **llm_overrides) -> Config:
    """Build a valid Config pointing at *tmp_path* workspace."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-hotreload",
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

    def _bot_update_config(new_cfg: BotConfig) -> None:
        mock_bot._cfg = new_cfg

    mock_bot.update_config = _bot_update_config

    mock_channel = MagicMock()
    mock_channel.apply_channel_config = MagicMock()

    mock_llm = MagicMock()
    mock_llm._cfg = config.llm

    from src.shutdown import GracefulShutdown

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


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigHotReloadE2E:
    """End-to-end: config file → watcher detection → component reconfiguration."""

    @pytest.mark.asyncio
    async def test_config_file_change_triggers_reconfiguration(self, tmp_path: Path) -> None:
        """
        Full pipeline: write a config, start the watcher, modify a safe field
        on disk, and verify the component received a reconfigure call with the
        new value.

        Verifies:
            1. ``applier.apply()`` is invoked with old and new configs
            2. The bot component's config reflects the new ``max_tool_iterations``
            3. The LLM provider received ``update_config()`` with the new temperature
        """
        # ── Arrange ──
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        # Track apply calls
        original_apply = applier.apply
        apply_calls: list[tuple[Config, Config]] = []

        def _tracking_apply(old: Config, new: Config):
            apply_calls.append((old, new))
            return original_apply(old, new)

        applier.apply = _tracking_apply  # type: ignore[assignment]

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # Pre-condition: no calls yet
            assert len(apply_calls) == 0

            # ── Act: write updated config ──
            updated = _base_config(tmp_path, max_tool_iterations=5, temperature=0.3)
            save_config(updated, config_path)

            # Wait for detection + apply
            await asyncio.sleep(0.5)

            # ── Assert: apply was called ──
            assert len(apply_calls) >= 1, (
                "applier.apply() was never called — watcher did not detect the change"
            )

            old_cfg, new_cfg = apply_calls[0]
            assert old_cfg is initial
            assert new_cfg.llm.max_tool_iterations == 5
            assert new_cfg.llm.temperature == 0.3

            # ── Assert: bot component reconfigured ──
            assert applier._bot._cfg.max_tool_iterations == 5, (
                "Bot should reflect the new max_tool_iterations after hot-reload"
            )

            # ── Assert: LLM provider received update_config ──
            applier._llm.update_config.assert_called_once()
            llm_arg = applier._llm.update_config.call_args[0][0]
            assert llm_arg.temperature == 0.3, (
                "LLM provider should receive the new temperature"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_invalid_config_does_not_break_watcher(self, tmp_path: Path, caplog) -> None:
        """
        Writing invalid JSON to the config file causes the watcher to log an
        error but continue polling.  The current config remains in effect and
        a subsequent valid write is still detected and applied.
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

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # ── Step 1: Write invalid config ──
            config_path.write_text("{invalid json!!!", encoding="utf-8")

            with caplog.at_level(logging.ERROR, logger="src.config.config_watcher"):
                await asyncio.sleep(0.5)

            # ── Assert: watcher logged error ──
            assert any("Config hot-reload failed" in rec.message for rec in caplog.records), (
                "Expected error log about config hot-reload failure"
            )

            # ── Assert: watcher still running ──
            assert watcher._running, "Watcher should still be running after invalid config"

            # ── Assert: current config unchanged ──
            assert applier._bot._cfg.max_tool_iterations == 10, (
                "Bot config should remain unchanged after invalid config"
            )

            # ── Step 2: Write a valid updated config ──
            updated = _base_config(tmp_path, max_tool_iterations=3)
            save_config(updated, config_path)

            await asyncio.sleep(0.5)

            # ── Assert: watcher recovered and applied valid change ──
            assert applier._bot._cfg.max_tool_iterations == 3, (
                "Bot should reflect new max_tool_iterations after recovery"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_multiple_rapid_changes_only_last_applied(self, tmp_path: Path) -> None:
        """
        Writing multiple config changes in rapid succession within the debounce
        window should result in only one reload cycle.  The final value on disk
        is what gets applied.

        Uses ``debounce=0.3`` so that writes happening within the debounce
        window are coalesced into a single detection cycle.
        """
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        # Track apply calls
        original_apply = applier.apply
        apply_calls: list[tuple[Config, Config]] = []

        def _tracking_apply(old: Config, new: Config):
            apply_calls.append((old, new))
            return original_apply(old, new)

        applier.apply = _tracking_apply  # type: ignore[assignment]

        # Use a non-zero debounce to coalesce rapid writes
        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.3,
        )

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # ── Act: write 3 different configs rapidly ──
            for iterations in (20, 15, 5):
                cfg = _base_config(tmp_path, max_tool_iterations=iterations)
                save_config(cfg, config_path)
                # Tiny sleep to let mtime diverge but stay within debounce
                await asyncio.sleep(0.02)

            # Wait for debounce to pass + detection
            await asyncio.sleep(0.8)

            # ── Assert: only the last value is applied ──
            assert applier._bot._cfg.max_tool_iterations == 5, (
                "Only the last rapid write's value (max_tool_iterations=5) "
                "should be applied after debounce coalescing"
            )

            # ── Assert: at least one apply call happened ──
            assert len(apply_calls) >= 1, (
                "At least one apply call should have occurred after debounce window"
            )

            # The applied config should have the final value
            _, final_new = apply_calls[-1]
            assert final_new.llm.max_tool_iterations == 5, (
                "The last applied config should have max_tool_iterations=5"
            )
        finally:
            await watcher.stop()
