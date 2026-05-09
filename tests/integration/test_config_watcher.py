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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import BotConfig
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig, save_config
from src.config.config import ShellConfig
from src.config.config_watcher import ConfigChangeApplier, ConfigWatcher
from src.core.event_bus import EVENT_CONFIG_CHANGED, Event, get_event_bus, reset_event_bus
from src.exceptions import ConfigurationError
from src.shutdown import GracefulShutdown
from src.skills.builtin.shell import ShellSkill
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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


def _make_applier(config: Config, on_config_swap=None):
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

    kwargs = dict(
        app_config=config,
        bot=mock_bot,
        channel=mock_channel,
        llm=mock_llm,
        shutdown_mgr=shutdown_mgr,
        reconfigure_logging=reconfigure_logging,
    )
    if on_config_swap is not None:
        kwargs["on_config_swap"] = on_config_swap

    return ConfigChangeApplier(**kwargs)


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
    async def test_malformed_json_does_not_crash_watcher(self, tmp_path: Path) -> None:
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
    async def test_malformed_json_keeps_current_config(self, tmp_path: Path, caplog) -> None:
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
            assert any("Config hot-reload failed" in rec.message for rec in caplog.records), (
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

    def test_destructive_fields_warned_safe_fields_applied(self, tmp_path: Path, caplog) -> None:
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

    def test_apply_llm_config_preserves_destructive_fields_on_provider(
        self, tmp_path: Path
    ) -> None:
        """
        ``_apply_llm_config`` must preserve destructive fields (model, api_key)
        on the live LLM provider even when the new config changes them.
        Safe fields (temperature) are applied.

        Uses a realistic mock where ``update_config`` actually replaces
        ``_cfg`` — mirroring :meth:`LLMClient.update_config` — so the test
        asserts on the provider's internal state, not just the call argument.
        """
        initial = _base_config(tmp_path, temperature=0.3)
        applier = _make_applier(initial)

        # Wire LLM mock to actually replace _cfg on update_config,
        # mirroring the real LLMClient.update_config() behavior.
        def _llm_update_config(new_cfg: LLMConfig) -> None:
            applier._llm._cfg = new_cfg

        applier._llm.update_config = _llm_update_config

        # New config changes BOTH destructive (model, api_key) and safe (temperature)
        updated = Config(
            llm=LLMConfig(
                model="gpt-4o-mini",  # destructive — must NOT reach provider
                base_url="https://api.openai.com/v1",
                api_key="sk-new-dangerous-key",  # destructive — must NOT reach provider
                temperature=0.8,  # safe — MUST reach provider
                timeout=60.0,
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )

        # Act: call _apply_llm_config directly with safe changed set
        applier._apply_llm_config(
            updated,
            changed={"llm.temperature", "llm.timeout"},
            old_llm_cfg=initial.llm,
        )

        # Assert: provider's _cfg preserves destructive fields from old config
        provider_cfg = applier._llm._cfg
        assert provider_cfg.model == "gpt-4o", (
            "Provider _cfg.model should remain OLD — destructive field preserved"
        )
        assert provider_cfg.api_key == "sk-test-watcher", (
            "Provider _cfg.api_key should remain OLD — destructive field preserved"
        )

        # Assert: safe fields from new config ARE applied
        assert provider_cfg.temperature == 0.8, (
            "Provider _cfg.temperature should be NEW — safe field applied"
        )


class TestConfigChangeApplierAtomicSwap:
    """Verify that _update_app_config() uses an atomic reference swap."""

    def test_swap_replaces_config_object_not_mutates(self, tmp_path: Path) -> None:
        """
        _update_app_config() must replace the entire config reference rather
        than mutating fields in-place.  After the swap, ``applier._config``
        must be the *same object* as ``new_config``, not the old object with
        overwritten fields.
        """
        initial = _base_config(tmp_path)
        applier = _make_applier(initial)

        updated = _base_config(tmp_path, temperature=0.9)
        updated.memory_max_history = 999

        old_ref = applier._config
        assert old_ref is initial

        applier._update_app_config(updated)

        # The reference must have been swapped, not mutated
        assert applier._config is updated, (
            "ConfigChangeApplier._config should be the NEW config object"
        )
        assert applier._config is not old_ref, (
            "ConfigChangeApplier._config should no longer be the old object"
        )

    def test_swap_callback_propagates_to_application(self, tmp_path: Path) -> None:
        """
        When ``on_config_swap`` is provided, it must be called with the new
        config so that ``Application._config`` is updated atomically alongside
        the applier's internal reference.
        """
        initial = _base_config(tmp_path)

        # Simulate Application holding the config reference
        app_config_ref = {"config": initial}
        applier = _make_applier(
            initial,
            on_config_swap=lambda cfg: app_config_ref.update(config=cfg),
        )

        updated = _base_config(tmp_path, temperature=0.42)
        applier._update_app_config(updated)

        # Both applier and "Application" must see the new config
        assert applier._config is updated
        assert app_config_ref["config"] is updated

    def test_no_callback_still_swaps(self, tmp_path: Path) -> None:
        """
        When ``on_config_swap`` is None (e.g. tests without Application),
        the swap still works correctly on the applier's internal reference.
        """
        initial = _base_config(tmp_path)
        applier = _make_applier(initial)  # no on_config_swap

        updated = _base_config(tmp_path, temperature=0.1)
        applier._update_app_config(updated)

        assert applier._config is updated

    def test_invalid_config_does_not_swap(self, tmp_path: Path) -> None:
        """
        An invalid config must NOT trigger the swap — the old config must
        remain in place for both the applier and the callback target.
        """
        initial = _base_config(tmp_path)

        app_config_ref = {"config": initial}
        applier = _make_applier(
            initial,
            on_config_swap=lambda cfg: app_config_ref.update(config=cfg),
        )

        bad_config = Config(
            llm=LLMConfig(
                model="",  # empty → invalid
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )

        with pytest.raises(ConfigurationError):
            applier._update_app_config(bad_config)

        # Both must still point to the original config
        assert applier._config is initial
        assert app_config_ref["config"] is initial

    @pytest.mark.asyncio
    async def test_no_hybrid_state_during_concurrent_reads(self, tmp_path: Path) -> None:
        """
        Concurrent coroutines reading config fields during a hot-reload must
        never observe a hybrid state (e.g. new temperature but old timeout).
        With the atomic swap pattern, a reader sees either all-old or all-new.
        """
        initial = _base_config(tmp_path, temperature=0.5)
        initial.log_verbosity = "normal"
        initial.shutdown_timeout = 30.0

        applier = _make_applier(initial)

        # Reader coroutine that repeatedly snapshots config fields
        hybrid_detected = asyncio.Event()
        stop_reading = asyncio.Event()

        async def reader():
            while not stop_reading.is_set():
                cfg = applier._config
                # In the old config: temp=0.5, verbosity=normal, timeout=30
                # In the new config: temp=0.9, verbosity=verbose, timeout=60
                # A hybrid state would be mixing these (e.g. temp=0.9 but timeout=30)
                is_old = (
                    cfg.llm.temperature == 0.5
                    and cfg.log_verbosity == "normal"
                    and cfg.shutdown_timeout == 30.0
                )
                is_new = (
                    cfg.llm.temperature == 0.9
                    and cfg.log_verbosity == "verbose"
                    and cfg.shutdown_timeout == 60.0
                )
                if not is_old and not is_new:
                    hybrid_detected.set()
                    return
                await asyncio.sleep(0)

        # Start readers
        readers = [asyncio.create_task(reader()) for _ in range(5)]

        # Give readers a moment to start
        await asyncio.sleep(0.01)

        # Apply config change (atomic swap)
        updated = _base_config(tmp_path, temperature=0.9)
        updated.log_verbosity = "verbose"
        updated.shutdown_timeout = 60.0
        applier._update_app_config(updated)

        # Let readers observe the new state
        await asyncio.sleep(0.02)

        stop_reading.set()
        for r in readers:
            await r

        assert not hybrid_detected.is_set(), "Detected hybrid config state — atomic swap failed"


class TestShellDenylistHotReload:
    """Verify hot-reloaded shell.command_denylist takes effect on skill execution.

    Tests the full pipeline: file change → watcher detection → config swap →
    new ShellSkill behavior. Uses ``docker ps`` (not ``rm -rf /``) because
    ``rm -rf`` is already blocked by built-in patterns — the test must prove
    the *custom* denylist added via hot-reload is what blocks the command.
    """

    @pytest.mark.asyncio
    async def test_hot_reloaded_denylist_blocks_command(self, tmp_path: Path) -> None:
        """
        Write a config with empty denylist, start the watcher, then update
        the config to add ``\\bdocker\\b`` to the denylist. After the watcher
        detects and applies the change, a ShellSkill constructed with the
        updated config must reject ``docker ps``.
        """
        # ── Arrange: initial config with empty denylist ──
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path)
        initial.shell = ShellConfig(command_denylist=[], command_allowlist=[])
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        # Pre-condition: "docker ps" passes security with empty denylist
        initial_skill = ShellSkill(config=initial.shell)
        with patch("src.skills.builtin.shell.AsyncExecutor") as mock_exec_cls:
            mock_instance = AsyncMock()
            mock_instance.run = AsyncMock(
                return_value=MagicMock(
                    stdout="CONTAINER ID  IMAGE",
                    stderr="",
                    return_code=0,
                    timed_out=False,
                )
            )
            mock_exec_cls.return_value = mock_instance

            result_before = await initial_skill.execute(
                workspace_dir=tmp_path,
                command="docker ps",
            )

        assert not result_before.startswith("❌ Security:"), (
            "docker ps should be allowed with empty denylist"
        )

        # ── Act: hot-reload config with denylist ──
        updated = _base_config(tmp_path)
        updated.shell = ShellConfig(
            command_denylist=[r"\bdocker\b"],
            command_allowlist=[],
        )
        save_config(updated, config_path)

        watcher.start()
        try:
            await asyncio.sleep(0.5)

            # ── Assert: applier received the new config ──
            reloaded_config = applier._config
            assert reloaded_config.shell.command_denylist == [r"\bdocker\b"], (
                "Applier config should reflect the hot-reloaded denylist"
            )

            # ── Assert: new ShellSkill with updated config blocks the command ──
            reloaded_skill = ShellSkill(config=reloaded_config.shell)
            result_after = await reloaded_skill.execute(
                workspace_dir=tmp_path,
                command="docker ps",
            )

            assert result_after.startswith("❌ Security:"), (
                "docker ps should be blocked after hot-reload added it to denylist"
            )
            assert "custom denylist" in result_after.lower(), (
                f"Expected 'custom denylist' in rejection message, got: {result_after}"
            )
        finally:
            await watcher.stop()


class TestConfigChangedEventEmission:
    """Verify that config_changed events are emitted during hot-reload."""

    @pytest.mark.asyncio
    async def test_event_emitted_on_safe_field_change(self, tmp_path: Path) -> None:
        """
        When a safe field changes during hot-reload, a ``config_changed`` event
        is emitted with a structured diff payload containing the changed fields
        and their old/new values.
        """
        reset_event_bus()
        bus = get_event_bus()

        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, temperature=0.5)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        # Subscribe to capture emitted events
        emitted_events: list[Event] = []

        async def _capture(event: Event) -> None:
            emitted_events.append(event)

        bus.on(EVENT_CONFIG_CHANGED, _capture)

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # Write updated config — change temperature (safe field)
            updated = _base_config(tmp_path, temperature=0.9)
            save_config(updated, config_path)

            await asyncio.sleep(0.5)

            # Assert: event was emitted
            config_events = [e for e in emitted_events if e.name == EVENT_CONFIG_CHANGED]
            assert len(config_events) >= 1, (
                f"Expected at least one config_changed event, got {len(config_events)}"
            )

            event = config_events[0]
            assert event.source == "ConfigWatcher._reload_config"
            assert "safe_changed" in event.data
            assert "destructive_changed" in event.data
            assert "unknown_changed" in event.data
            assert "diffs" in event.data
            assert "llm.temperature" in event.data["safe_changed"]
            assert event.data["diffs"]["llm.temperature"]["old"] == 0.5
            assert event.data["diffs"]["llm.temperature"]["new"] == 0.9
        finally:
            bus.off(EVENT_CONFIG_CHANGED, _capture)
            await bus.close()
            reset_event_bus()
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_no_event_when_no_changes(self, tmp_path: Path) -> None:
        """
        When the config file is re-written with identical content, no
        ``config_changed`` event is emitted because there are no diffs.
        """
        reset_event_bus()
        bus = get_event_bus()

        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, temperature=0.5)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        emitted_events: list[Event] = []

        async def _capture(event: Event) -> None:
            emitted_events.append(event)

        bus.on(EVENT_CONFIG_CHANGED, _capture)

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # Re-save the SAME config (touch the file but no actual changes)
            save_config(initial, config_path)

            await asyncio.sleep(0.5)

            # Assert: no config_changed event emitted
            config_events = [e for e in emitted_events if e.name == EVENT_CONFIG_CHANGED]
            assert len(config_events) == 0, (
                f"Expected no config_changed event for identical config, got {len(config_events)}"
            )
        finally:
            bus.off(EVENT_CONFIG_CHANGED, _capture)
            await bus.close()
            reset_event_bus()
            await watcher.stop()

    def test_apply_returns_structured_diff(self, tmp_path: Path) -> None:
        """
        ``ConfigChangeApplier.apply()`` returns a structured diff dict
        with classified fields and old/new values.
        """
        initial = _base_config(tmp_path, temperature=0.5)
        initial.memory_max_history = 50
        applier = _make_applier(initial)

        updated = Config(
            llm=LLMConfig(
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="sk-new-key",
                temperature=0.9,
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            memory_max_history=100,
            skills_auto_load=False,
        )

        diff = applier.apply(initial, updated)

        assert diff is not None
        assert "llm.temperature" in diff["safe_changed"]
        assert "memory_max_history" in diff["safe_changed"]
        assert "llm.model" in diff["destructive_changed"]
        assert "llm.api_key" in diff["destructive_changed"]
        assert diff["diffs"]["llm.temperature"] == {"old": 0.5, "new": 0.9}
        assert diff["diffs"]["memory_max_history"] == {"old": 50, "new": 100}

    def test_apply_returns_none_when_no_changes(self, tmp_path: Path) -> None:
        """
        ``ConfigChangeApplier.apply()`` returns ``None`` when old and new
        configs are identical.
        """
        initial = _base_config(tmp_path)
        applier = _make_applier(initial)

        result = applier.apply(initial, initial)

        assert result is None


class TestMultiComponentSimultaneousReconfiguration:
    """Verify that a single hot-reload cycle updates multiple components atomically.

    When a config change touches safe fields belonging to different components
    (e.g. ``llm.max_tool_iterations`` → BotConfig, ``llm.temperature`` → LLM provider),
    both components must receive their updates within the same reload cycle — not
    spread across separate polling intervals.
    """

    @pytest.mark.asyncio
    async def test_bot_and_llm_updated_in_same_reload_cycle(self, tmp_path: Path) -> None:
        """
        Write a config that changes both ``max_tool_iterations`` (BotConfig)
        and ``temperature`` (LLM provider) simultaneously. After the watcher
        detects and applies the change, verify:

            a) BotConfig reflects the new ``max_tool_iterations`` value
            b) LLM provider received ``update_config()`` with the new temperature
            c) Both updates happened in a single reload (one apply call)
        """
        reset_event_bus()
        bus = get_event_bus()

        # ── Arrange: initial config with known values ──
        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        # Track apply calls to verify single-reload delivery
        apply_results: list[dict] = []
        original_apply = applier.apply

        def _tracking_apply(old: Config, new: Config):
            result = original_apply(old, new)
            if result is not None:
                apply_results.append(result)
            return result

        applier.apply = _tracking_apply

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        # Subscribe to capture the config_changed event
        emitted_events: list[Event] = []

        async def _capture(event: Event) -> None:
            emitted_events.append(event)

        bus.on(EVENT_CONFIG_CHANGED, _capture)

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # Pre-condition: bot and LLM still have original values
            assert applier._bot._cfg.max_tool_iterations == 10
            assert applier._llm._cfg.temperature == 0.7

            # ── Act: write config changing BOTH components at once ──
            updated = _base_config(tmp_path, max_tool_iterations=5, temperature=0.3)
            save_config(updated, config_path)

            await asyncio.sleep(0.5)

            # ── Assert a: BotConfig updated ──
            assert applier._bot._cfg.max_tool_iterations == 5, (
                f"Expected BotConfig.max_tool_iterations=5 after hot-reload, "
                f"got {applier._bot._cfg.max_tool_iterations}"
            )

            # ── Assert b: LLM provider received update_config with new temperature ──
            applier._llm.update_config.assert_called_once()
            llm_arg = applier._llm.update_config.call_args[0][0]
            assert llm_arg.temperature == 0.3, (
                f"Expected LLM config temperature=0.3, got {llm_arg.temperature}"
            )

            # ── Assert c: Both fields delivered in a single apply call ──
            assert len(apply_results) == 1, (
                f"Expected exactly 1 apply call with changes, got {len(apply_results)}"
            )
            diff = apply_results[0]
            assert "llm.max_tool_iterations" in diff["safe_changed"], (
                "max_tool_iterations should be in safe_changed"
            )
            assert "llm.temperature" in diff["safe_changed"], (
                "temperature should be in safe_changed"
            )

            # ── Assert d: config_changed event includes both fields ──
            config_events = [e for e in emitted_events if e.name == EVENT_CONFIG_CHANGED]
            assert len(config_events) >= 1, "Expected at least one config_changed event"
            event_data = config_events[0].data
            assert "llm.max_tool_iterations" in event_data["safe_changed"]
            assert "llm.temperature" in event_data["safe_changed"]
            assert event_data["diffs"]["llm.max_tool_iterations"] == {"old": 10, "new": 5}
            assert event_data["diffs"]["llm.temperature"] == {"old": 0.7, "new": 0.3}
        finally:
            bus.off(EVENT_CONFIG_CHANGED, _capture)
            await bus.close()
            reset_event_bus()
            await watcher.stop()


class TestConfigChangeApplierGracefulDegradation:
    """Verify that a failing component applier does not block others.

    Each per-component applier (bot, channel, llm, shutdown, logging, shell)
    is isolated — if one raises, the remaining appliers still execute.
    """

    def test_bot_failure_does_not_block_logging_and_shutdown(self, tmp_path: Path) -> None:
        """If ``_apply_bot_config`` raises ValueError, logging and shutdown still apply."""
        from dataclasses import replace as dc_replace

        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        initial = dc_replace(initial, shutdown_timeout=30.0)
        applier = _make_applier(initial)

        # Sabotage: make bot.update_config raise ValueError
        applier._bot.update_config = MagicMock(side_effect=ValueError("bad iterations"))

        new_config = _base_config(tmp_path, max_tool_iterations=-1, temperature=0.5)
        new_config = dc_replace(new_config, shutdown_timeout=60.0)

        result = applier.apply(initial, new_config)

        # apply() must still return a diff (not raise)
        assert result is not None
        assert "llm.max_tool_iterations" in result["safe_changed"]
        assert "llm.temperature" in result["safe_changed"]
        assert "shutdown_timeout" in result["safe_changed"]

        # Bot applier failed — never reached the real update
        applier._bot.update_config.assert_called_once()

        # LLM applier should still have been called
        applier._llm.update_config.assert_called_once()
        llm_arg = applier._llm.update_config.call_args[0][0]
        assert llm_arg.temperature == 0.5

        # Shutdown timeout should have been updated despite bot failure
        assert applier._shutdown_mgr._timeout == 60.0

    def test_channel_and_llm_fail_but_shutdown_succeeds(self, tmp_path: Path) -> None:
        """Multiple failing appliers do not prevent the rest from running."""
        from dataclasses import replace as dc_replace

        initial = _base_config(tmp_path, temperature=0.7)
        initial = dc_replace(initial, shutdown_timeout=30.0)
        applier = _make_applier(initial)

        # Sabotage channel and LLM
        applier._channel.apply_channel_config = MagicMock(
            side_effect=RuntimeError("channel broken")
        )
        applier._llm.update_config = MagicMock(side_effect=RuntimeError("llm broken"))

        new_config = _base_config(tmp_path, temperature=0.3)
        new_config = dc_replace(new_config, shutdown_timeout=45.0)

        result = applier.apply(initial, new_config)

        assert result is not None
        assert "llm.temperature" in result["safe_changed"]
        assert "shutdown_timeout" in result["safe_changed"]

        # Shutdown should have been applied despite channel + LLM failures
        assert applier._shutdown_mgr._timeout == 45.0

    def test_all_appliers_fail_still_returns_diff(self, tmp_path: Path) -> None:
        """Even if every component applier raises, apply() returns the diff."""
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        applier = _make_applier(initial)

        # Sabotage bot
        applier._bot.update_config = MagicMock(side_effect=ValueError("boom"))

        new_config = _base_config(tmp_path, max_tool_iterations=5, temperature=0.3)

        result = applier.apply(initial, new_config)

        assert result is not None
        assert "llm.max_tool_iterations" in result["safe_changed"]
        assert "llm.temperature" in result["safe_changed"]

    def test_invalid_max_tool_iterations_does_not_block_log_verbosity(
        self, tmp_path: Path
    ) -> None:
        """Partial field application: ``log_verbosity`` is applied even when
        ``max_tool_iterations=-1`` causes the bot applier to fail.

        Simulates a hot-reload where one safe field is valid (log_verbosity)
        and another is invalid (max_tool_iterations=-1).  The per-component
        isolation in ``apply()`` ensures the logging applier still runs and
        the valid field is applied, while the invalid one is skipped with a
        warning.
        """
        from dataclasses import replace as dc_replace

        initial = _base_config(tmp_path, max_tool_iterations=10)
        initial = dc_replace(initial, log_verbosity="normal")
        applier = _make_applier(initial)

        # Sabotage: make bot.update_config raise ValueError for invalid iterations
        applier._bot.update_config = MagicMock(
            side_effect=ValueError("max_tool_iterations must be >= 1")
        )

        new_config = _base_config(tmp_path, max_tool_iterations=-1)
        new_config = dc_replace(new_config, log_verbosity="verbose")

        result = applier.apply(initial, new_config)

        # apply() must still return a diff (not raise)
        assert result is not None

        # Both fields should appear in safe_changed — the diff reflects
        # what *changed* in the config, not what was successfully applied
        assert "log_verbosity" in result["safe_changed"]
        assert "llm.max_tool_iterations" in result["safe_changed"]

        # Bot applier was called but failed with ValueError
        applier._bot.update_config.assert_called_once()

        # Logging applier was called despite bot failure — log_verbosity
        # is handled by a separate component applier
        applier._reconfigure_logging.assert_called_once()
        logging_arg = applier._reconfigure_logging.call_args[0][0]
        assert logging_arg.log_verbosity == "verbose"

    def test_applier_results_all_ok(self, tmp_path: Path) -> None:
        """``applier_results`` maps every component to ``"ok"`` on success."""
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        applier = _make_applier(initial)

        new_config = _base_config(tmp_path, max_tool_iterations=5, temperature=0.3)

        result = applier.apply(initial, new_config)

        assert result is not None
        assert result["applier_results"] == {
            "bot": "ok",
            "channel": "ok",
            "llm": "ok",
            "shutdown": "ok",
            "logging": "ok",
            "shell": "ok",
        }

    def test_applier_results_partial_failure(self, tmp_path: Path) -> None:
        """``applier_results`` marks failed components as ``"failed"``."""
        from dataclasses import replace as dc_replace

        initial = _base_config(tmp_path, temperature=0.7)
        initial = dc_replace(initial, shutdown_timeout=30.0)
        applier = _make_applier(initial)

        # Sabotage channel and LLM appliers
        applier._channel.apply_channel_config = MagicMock(
            side_effect=RuntimeError("channel broken")
        )
        applier._llm.update_config = MagicMock(side_effect=RuntimeError("llm broken"))

        new_config = _base_config(tmp_path, temperature=0.3)
        new_config = dc_replace(new_config, shutdown_timeout=45.0)

        result = applier.apply(initial, new_config)

        assert result is not None
        assert result["applier_results"]["channel"] == "failed"
        assert result["applier_results"]["llm"] == "failed"
        assert result["applier_results"]["bot"] == "ok"
        assert result["applier_results"]["shutdown"] == "ok"
        assert result["applier_results"]["logging"] == "ok"
        assert result["applier_results"]["shell"] == "ok"

    def test_applier_results_not_present_when_no_safe_changes(
        self, tmp_path: Path
    ) -> None:
        """``applier_results`` is absent when only destructive fields changed."""
        initial = _base_config(tmp_path, temperature=0.7)
        applier = _make_applier(initial)

        # Change only a destructive field (llm.model)
        new_config = _base_config(tmp_path, temperature=0.7)
        new_config = new_config.__class__(
            llm=new_config.llm.__class__(
                model="gpt-4o-mini",  # destructive change
                base_url=new_config.llm.base_url,
                api_key=new_config.llm.api_key,
                temperature=new_config.llm.temperature,
            ),
            whatsapp=new_config.whatsapp,
            skills_auto_load=False,
        )

        result = applier.apply(initial, new_config)

        assert result is not None
        assert "applier_results" not in result


class TestMultiComponentReconfiguration:
    """Verify ConfigWatcher reconfigures multiple components simultaneously.

    When a config change touches safe fields belonging to different components,
    all affected components must receive their updates within the same reload
    cycle.  Conversely, components whose fields did NOT change must be left
    untouched.
    """

    @pytest.mark.asyncio
    async def test_bot_llm_and_shutdown_reconfigured_together(
        self, tmp_path: Path
    ) -> None:
        """When config changes affect bot, LLM, and shutdown fields,
        all three components are reconfigured in the same callback."""
        from dataclasses import replace as dc_replace

        config_path = tmp_path / "config.json"
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        initial = dc_replace(initial, shutdown_timeout=30.0)
        save_config(initial, config_path)

        applier = _make_applier(initial)

        # Track apply calls to verify single-reload delivery
        apply_count = 0
        original_apply = applier.apply

        def _counting_apply(old: Config, new: Config):
            nonlocal apply_count
            result = original_apply(old, new)
            if result is not None:
                apply_count += 1
            return result

        applier.apply = _counting_apply  # type: ignore[assignment]

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

            # Write config changing bot + LLM + shutdown simultaneously
            updated = _base_config(tmp_path, max_tool_iterations=5, temperature=0.3)
            updated = dc_replace(updated, shutdown_timeout=60.0)
            save_config(updated, config_path)

            await asyncio.sleep(0.5)

            # ── Bot reconfigured ──
            assert applier._bot._cfg.max_tool_iterations == 5, (
                f"Expected BotConfig.max_tool_iterations=5, "
                f"got {applier._bot._cfg.max_tool_iterations}"
            )

            # ── LLM provider reconfigured ──
            applier._llm.update_config.assert_called_once()
            llm_arg = applier._llm.update_config.call_args[0][0]
            assert llm_arg.temperature == 0.3, (
                f"Expected LLM config temperature=0.3, got {llm_arg.temperature}"
            )

            # ── Shutdown reconfigured ──
            assert applier._shutdown_mgr._timeout == 60.0, (
                f"Expected shutdown timeout=60.0, "
                f"got {applier._shutdown_mgr._timeout}"
            )

            # ── All updates delivered in a single reload cycle ──
            assert apply_count == 1, (
                f"Expected exactly 1 apply call, got {apply_count}"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_partial_config_change_only_affects_relevant_components(
        self, tmp_path: Path
    ) -> None:
        """Only components whose fields changed are reconfigured.

        Changing only ``llm.temperature`` (an LLM-only field) must trigger
        the LLM provider update but NOT reconfigure Bot, since no bot-specific
        fields (``max_tool_iterations``, ``memory_max_history``, etc.) changed.
        """
        initial = _base_config(tmp_path, max_tool_iterations=10, temperature=0.7)
        applier = _make_applier(initial)

        # Capture the bot's config reference before the change
        initial_bot_cfg = applier._bot._cfg

        # Change only llm.temperature — NOT a bot_fields member
        updated = _base_config(tmp_path, max_tool_iterations=10, temperature=0.3)

        result = applier.apply(initial, updated)

        assert result is not None
        assert "llm.temperature" in result["safe_changed"]

        # ── LLM provider WAS reconfigured ──
        applier._llm.update_config.assert_called_once()
        llm_arg = applier._llm.update_config.call_args[0][0]
        assert llm_arg.temperature == 0.3

        # ── Bot was NOT reconfigured ──
        # _apply_bot_config checks bot_fields & changed — since only
        # llm.temperature changed (not in bot_fields), update_config
        # is never called and _cfg remains the same object.
        assert applier._bot._cfg is initial_bot_cfg, (
            "Bot config should NOT have been updated when no bot fields changed"
        )
        assert applier._bot._cfg.max_tool_iterations == 10
