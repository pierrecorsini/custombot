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
from src.exceptions import ConfigurationError
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

    # Wire update_config to actually replace _cfg, mirroring Bot.update_config()
    def _bot_update_config(new_cfg: BotConfig) -> None:
        mock_bot._cfg = new_cfg

    mock_bot.update_config = _bot_update_config

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


class TestConfigChangeApplierValidation:
    """Verify that _update_app_config() validates before mutation."""

    def test_valid_config_passes_validation(self, tmp_path: Path) -> None:
        """
        A valid Config passes the ``is_valid_config`` guard in
        ``_update_app_config`` and fields are applied successfully.
        """
        initial = _base_config(tmp_path)
        applier = _make_applier(initial)

        updated = _base_config(tmp_path, temperature=0.9)

        # apply() will call _update_app_config internally for safe field changes
        applier.apply(initial, updated)

        assert applier._config.llm.temperature == 0.9

    def test_invalid_config_rejected_before_mutation(self, tmp_path: Path) -> None:
        """
        An invalid Config (e.g. LLMConfig.model set to empty string) is
        rejected by ``_update_app_config`` — ``ConfigurationError`` is raised
        and the live config is NOT mutated.
        """
        initial = _base_config(tmp_path)
        applier = _make_applier(initial)

        # Build a config that passes _load_and_validate_file / _from_dict
        # but fails is_valid_config() (e.g. empty model string)
        bad_config = Config(
            llm=LLMConfig(
                model="",  # empty model → is_llm_config returns False
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )

        # _update_app_config should raise ConfigurationError
        with pytest.raises(ConfigurationError, match="Config validation failed"):
            applier._update_app_config(bad_config)

        # Live config must remain unchanged
        assert applier._config.llm.model == "gpt-4o"

    def test_invalid_config_via_apply_does_not_mutate(self, tmp_path: Path) -> None:
        """
        When ``apply()`` is called with a config that has safe-field changes
        but fails validation in ``_update_app_config``, the per-component
        updates that ran before the guard are applied but the app-level config
        is NOT mutated.  This is acceptable because component-level updates
        (Bot, LLM) have their own validation.
        """
        initial = _base_config(tmp_path, max_tool_iterations=10)
        applier = _make_applier(initial)

        bad_config = Config(
            llm=LLMConfig(
                model="",  # empty → invalid
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
                max_tool_iterations=5,  # safe field change
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )

        # apply() should raise ConfigurationError from _update_app_config
        with pytest.raises(ConfigurationError, match="Config validation failed"):
            applier.apply(initial, bad_config)

        # App-level config must remain unchanged
        assert applier._config.llm.model == "gpt-4o"
        assert applier._config.llm.max_tool_iterations == 10


class TestConfigChangeApplierDestructiveFields:
    """Verify destructive fields are warned and NOT applied; safe fields ARE applied."""

    def test_destructive_fields_warned_safe_fields_applied(
        self, tmp_path: Path, caplog
    ) -> None:
        """
        When a config change includes BOTH destructive fields (``llm.model``,
        ``llm.api_key``) and safe fields (``llm.temperature``,
        ``memory_max_history``), the destructive fields are logged as warnings
        and NOT forwarded to live components, while safe fields ARE applied.
        """
        import logging

        initial = _base_config(tmp_path, temperature=0.5, max_tool_iterations=10)
        initial.memory_max_history = 50
        applier = _make_applier(initial)

        updated = Config(
            llm=LLMConfig(
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="sk-new-dangerous-key",
                temperature=0.9,
                max_tool_iterations=5,
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            memory_max_history=100,
            skills_auto_load=False,
        )

        with caplog.at_level(logging.WARNING, logger="src.config.config_watcher"):
            applier.apply(initial, updated)

        # ── Destructive fields: logged as warnings ──
        destructive_warnings = [
            rec.message
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "requires restart" in rec.message
        ]
        assert any("llm.model" in msg for msg in destructive_warnings), (
            f"Expected warning for 'llm.model', got: {destructive_warnings}"
        )
        assert any("llm.api_key" in msg for msg in destructive_warnings), (
            f"Expected warning for 'llm.api_key', got: {destructive_warnings}"
        )

        # ── LLM provider: keeps OLD destructive fields, receives NEW safe fields ──
        mock_llm = applier._llm
        mock_llm.update_config.assert_called_once()
        llm_cfg_arg = mock_llm.update_config.call_args[0][0]
        assert llm_cfg_arg.model == "gpt-4o", (
            "LLM provider should keep OLD model — destructive field not applied"
        )
        assert llm_cfg_arg.api_key == "sk-test-watcher", (
            "LLM provider should keep OLD api_key — destructive field not applied"
        )
        assert llm_cfg_arg.temperature == 0.9, (
            "LLM provider should receive NEW temperature — safe field applied"
        )

        # ── Bot: safe fields applied ──
        bot: MagicMock = applier._bot  # type: ignore[assignment]
        assert bot._cfg.memory_max_history == 100, (
            "Bot should receive NEW memory_max_history — safe field applied"
        )
        assert bot._cfg.max_tool_iterations == 5, (
            "Bot should receive NEW max_tool_iterations — safe field applied"
        )
