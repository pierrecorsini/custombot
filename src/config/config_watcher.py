"""
src/config/config_watcher.py — Polling-based configuration hot-reload watcher.

Detects changes to ``config.json`` via mtime polling (debounced) and applies
safe/non-destructive config changes at runtime without restarting the bot.

Pattern mirrors ``RoutingEngine._is_stale()`` for consistency.

Safe (hot-reloadable) fields:
    - ``whatsapp.allowed_numbers``, ``whatsapp.allow_all``
    - ``llm.max_tool_iterations``, ``llm.temperature``, ``llm.system_prompt_prefix``
    - ``memory_max_history``
    - ``log_verbosity``, ``log_incoming_messages``, ``log_routing_info``, ``log_llm``
    - ``shutdown_timeout``
    - ``shell.command_denylist``, ``shell.command_allowlist``

Destructive (requires restart) fields — logged as warnings:
    - ``llm.model``, ``llm.base_url``, ``llm.api_key``
    - ``llm.embedding_model``, ``llm.embedding_dimensions``
    - ``llm.stream_response``
    - ``whatsapp.provider``, ``whatsapp.neonize.db_path``
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol, Set, Tuple, runtime_checkable

from src.config.config import (
    Config,
    _from_dict,
    _load_and_validate_file,
    _log_effective_config,
)
from src.constants import CONFIG_WATCH_DEBOUNCE_SECONDS, CONFIG_WATCH_INTERVAL_SECONDS

if TYPE_CHECKING:
    from src.bot import Bot, BotConfig
    from src.channels.base import BaseChannel
    from src.llm_provider import LLMProvider
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Field classification
# ─────────────────────────────────────────────────────────────────────────────

# Fields that CANNOT be hot-reloaded — require a full restart.
# Stored as dot-paths into the Config dataclass hierarchy.
DESTRUCTIVE_FIELDS: frozenset[str] = frozenset(
    {
        "llm.model",
        "llm.base_url",
        "llm.api_key",
        "llm.embedding_model",
        "llm.embedding_dimensions",
        "llm.stream_response",
        "whatsapp.provider",
        "whatsapp.neonize.db_path",
    }
)

# Fields that CAN be hot-reloaded, grouped by target component.
SAFE_FIELDS: frozenset[str] = frozenset(
    {
        "whatsapp.allowed_numbers",
        "whatsapp.allow_all",
        "llm.max_tool_iterations",
        "llm.temperature",
        "llm.system_prompt_prefix",
        "llm.timeout",
        "memory_max_history",
        "log_verbosity",
        "log_incoming_messages",
        "log_routing_info",
        "log_llm",
        "shutdown_timeout",
        "shell.command_denylist",
        "shell.command_allowlist",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Change detection helpers
# ─────────────────────────────────────────────────────────────────────────────


def _flatten_config(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten a Config dataclass into ``{"llm.model": "gpt-4o", ...}``."""
    result: Dict[str, Any] = {}
    if hasattr(obj, "__dataclass_fields__"):
        for f in fields(obj):  # type: ignore[arg-type]
            val = getattr(obj, f.name)
            key = f"{prefix}.{f.name}" if prefix else f.name
            if hasattr(val, "__dataclass_fields__"):
                result.update(_flatten_config(val, key))
            else:
                result[key] = val
    return result


def _diff_configs(
    old_flat: Dict[str, Any], new_flat: Dict[str, Any]
) -> Tuple[Set[str], Set[str], Set[str]]:
    """Return (safe_changed, destructive_changed, unknown_changed) field sets."""
    safe: Set[str] = set()
    destructive: Set[str] = set()
    unknown: Set[str] = set()

    all_keys = set(old_flat) | set(new_flat)
    for key in all_keys:
        old_val = old_flat.get(key)
        new_val = new_flat.get(key)
        if old_val == new_val:
            continue
        if key in SAFE_FIELDS:
            safe.add(key)
        elif key in DESTRUCTIVE_FIELDS:
            destructive.add(key)
        else:
            unknown.add(key)

    return safe, destructive, unknown


# ─────────────────────────────────────────────────────────────────────────────
# Change applier callback protocol
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigApplier(Protocol):
    """Structural type for objects that apply config changes."""

    def apply(self, old_config: Config, new_config: Config) -> None: ...


class ConfigChangeApplier:
    """Applies hot-reloadable config changes to live components.

    Constructed by ``Application`` with references to the components whose
    config can be safely updated at runtime.
    """

    def __init__(
        self,
        *,
        app_config: Config,
        bot: Bot,
        channel: BaseChannel,
        llm: LLMProvider,
        shutdown_mgr: GracefulShutdown,
        reconfigure_logging: Callable[[Config], None],
    ) -> None:
        self._config = app_config
        self._bot = bot
        self._channel = channel
        self._llm = llm
        self._shutdown_mgr = shutdown_mgr
        self._reconfigure_logging = reconfigure_logging

    def apply(self, old_config: Config, new_config: Config) -> None:
        """Detect changes between *old_config* and *new_config* and apply safe ones."""
        old_flat = _flatten_config(old_config)
        new_flat = _flatten_config(new_config)
        safe, destructive, unknown = _diff_configs(old_flat, new_flat)

        if not safe and not destructive and not unknown:
            return

        log.info(
            "Config change detected: %d safe, %d destructive, %d unknown fields",
            len(safe),
            len(destructive),
            len(unknown),
        )

        # Warn about destructive changes
        if destructive:
            for field_path in sorted(destructive):
                log.warning(
                    "Config change to '%s' requires restart — not applied. "
                    "Restart the bot to apply this change.",
                    field_path,
                )

        # Warn about unknown fields
        if unknown:
            for field_path in sorted(unknown):
                log.warning(
                    "Config change to '%s' is not classified as safe or destructive — skipped.",
                    field_path,
                )

        # Apply safe changes
        if not safe:
            return

        self._apply_bot_config(new_config, safe)
        self._apply_channel_config(new_config, safe)
        self._apply_llm_config(new_config, safe)
        self._apply_shutdown_config(new_config, safe)
        self._apply_logging_config(old_config, new_config, safe)
        self._apply_shell_config(new_config, safe)

        # Update the application-level config reference
        self._update_app_config(new_config)

        for field_path in sorted(safe):
            log.info("Applied config change: '%s'", field_path)

    # ── Per-component appliers ────────────────────────────────────────────

    def _apply_bot_config(self, new_config: Config, changed: Set[str]) -> None:
        """Rebuild ``BotConfig`` and replace on the bot instance."""
        from src.bot import BotConfig

        bot_fields = {
            "llm.max_tool_iterations",
            "llm.system_prompt_prefix",
            "memory_max_history",
        }
        if not bot_fields & changed:
            return

        new_bot_cfg = BotConfig(
            max_tool_iterations=new_config.llm.max_tool_iterations,
            memory_max_history=new_config.memory_max_history,
            system_prompt_prefix=new_config.llm.system_prompt_prefix,
            stream_response=self._bot._cfg.stream_response,  # destructive — keep old
        )
        object.__setattr__(self._bot, "_cfg", new_bot_cfg)
        log.debug(
            "BotConfig updated: max_tool_iterations=%d, memory_max_history=%d",
            new_bot_cfg.max_tool_iterations,
            new_bot_cfg.memory_max_history,
        )

    def _apply_channel_config(self, new_config: Config, changed: Set[str]) -> None:
        """Delegate channel-specific config changes to the channel."""
        self._channel.apply_channel_config(new_config, changed)

    def _apply_llm_config(self, new_config: Config, changed: Set[str]) -> None:
        """Update LLM client config (temperature)."""
        if "llm.temperature" not in changed and "llm.timeout" not in changed:
            return

        self._llm._cfg = new_config.llm
        log.debug(
            "LLM config updated: temperature=%.2f, timeout=%.1f",
            new_config.llm.temperature,
            new_config.llm.timeout,
        )

    def _apply_shutdown_config(
        self, new_config: Config, changed: Set[str]
    ) -> None:
        """Update shutdown timeout."""
        if "shutdown_timeout" not in changed:
            return

        self._shutdown_mgr._timeout = new_config.shutdown_timeout
        log.debug("Shutdown timeout updated: %.1fs", new_config.shutdown_timeout)

    def _apply_logging_config(
        self, old_config: Config, new_config: Config, changed: Set[str]
    ) -> None:
        """Reconfigure logging when verbosity changes."""
        log_fields = {
            "log_verbosity",
            "log_incoming_messages",
            "log_routing_info",
            "log_llm",
        }
        if not log_fields & changed:
            return

        if "log_verbosity" in changed:
            self._reconfigure_logging(new_config)
            log.info(
                "Log verbosity changed: %s → %s",
                old_config.log_verbosity,
                new_config.log_verbosity,
            )

    def _apply_shell_config(self, new_config: Config, changed: Set[str]) -> None:
        """Update shell skill denylist/allowlist."""
        shell_fields = {"shell.command_denylist", "shell.command_allowlist"}
        if not shell_fields & changed:
            return

        # Shell config is referenced by the shell skill at execution time.
        # Update the application-level config so it's picked up.
        log.debug(
            "Shell config updated: denylist=%d patterns, allowlist=%d patterns",
            len(new_config.shell.command_denylist),
            len(new_config.shell.command_allowlist),
        )

    def _update_app_config(self, new_config: Config) -> None:
        """Replace the application-level config reference."""
        # Update the Config dataclass fields in-place
        self._config.llm = new_config.llm
        self._config.whatsapp = new_config.whatsapp
        self._config.shell = new_config.shell
        self._config.memory_max_history = new_config.memory_max_history
        self._config.log_verbosity = new_config.log_verbosity
        self._config.log_incoming_messages = new_config.log_incoming_messages
        self._config.log_routing_info = new_config.log_routing_info
        self._config.log_llm = new_config.log_llm
        self._config.shutdown_timeout = new_config.shutdown_timeout


# ─────────────────────────────────────────────────────────────────────────────
# ConfigWatcher — polling-based file watcher
# ─────────────────────────────────────────────────────────────────────────────


class ConfigWatcher:
    """Polling-based watcher that detects ``config.json`` changes and hot-reloads.

    Follows the ``WorkspaceMonitor`` pattern for the async lifecycle:
    ``start()`` spawns a background ``asyncio.Task``, ``stop()`` cancels it.

    Usage::

        watcher = ConfigWatcher(
            config_path=Path("workspace/config.json"),
            applier=ConfigChangeApplier(...),
        )
        watcher.start()
        # ... later ...
        await watcher.stop()
    """

    def __init__(
        self,
        config_path: Path,
        current_config: Config,
        applier: ConfigApplier,
        *,
        poll_interval: float = CONFIG_WATCH_INTERVAL_SECONDS,
        debounce: float = CONFIG_WATCH_DEBOUNCE_SECONDS,
    ) -> None:
        self._config_path = config_path
        self._current_config = current_config
        self._applier = applier
        self._poll_interval = poll_interval
        self._debounce = debounce

        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

        # Track file mtime for change detection
        self._last_mtime: float = self._read_mtime()
        self._last_check: float = 0.0

    def _read_mtime(self) -> float:
        """Read the current mtime of the config file."""
        try:
            return os.stat(self._config_path).st_mtime
        except OSError:
            return 0.0

    def _is_stale(self) -> bool:
        """Check whether config file has changed since last reload (debounced)."""
        now = time.monotonic()
        if now - self._last_check < self._debounce:
            return False
        self._last_check = now
        current_mtime = self._read_mtime()
        return current_mtime != self._last_mtime

    def _load_new_config(self) -> Optional[Config]:
        """Load and validate the new config file. Returns None on failure."""
        try:
            data = _load_and_validate_file(self._config_path)
            config = _from_dict(Config, data)

            # Apply env overrides (same as initial load)
            from src.config.config import _apply_env_overrides

            _apply_env_overrides(config)

            return config
        except Exception as exc:
            log.error(
                "Config hot-reload failed: invalid config in %s — %s: %s. "
                "Keeping current config.",
                self._config_path,
                type(exc).__name__,
                exc,
            )
            return None

    async def _watch_loop(self) -> None:
        """Background task that polls for config changes."""
        log.info(
            "Config watcher started (path=%s, interval=%.0fs, debounce=%.1fs)",
            self._config_path,
            self._poll_interval,
            self._debounce,
        )

        while self._running:
            try:
                if self._is_stale():
                    await self._reload_config()
            except Exception as exc:
                log.error("Config watcher error: %s", exc, exc_info=True)

            await asyncio.sleep(self._poll_interval)

    async def _reload_config(self) -> None:
        """Reload config from disk and apply safe changes."""
        new_config = await asyncio.to_thread(self._load_new_config)
        if new_config is None:
            # Bad config — reset mtime so we don't retry until the file changes again
            self._last_mtime = self._read_mtime()
            return

        old_config = self._current_config
        self._applier.apply(old_config, new_config)

        # Update tracked state
        self._current_config = new_config
        self._last_mtime = self._read_mtime()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the config watcher background task."""
        if self._running:
            log.warning("Config watcher already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop the config watcher."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Config watcher stopped")

    @property
    def is_running(self) -> bool:
        """Whether the watcher is actively running."""
        return self._running
