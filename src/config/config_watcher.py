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
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    runtime_checkable,
)

from src.config.config_loader import _apply_env_overrides, _from_dict, _load_and_validate_file
from src.config.config_schema_defs import Config
from src.config.config_validation import _log_effective_config
from src.constants import CONFIG_WATCH_DEBOUNCE_SECONDS, CONFIG_WATCH_INTERVAL_SECONDS
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import EVENT_CONFIG_CHANGED, Event, get_event_bus
from src.exceptions import ConfigurationError
from src.security.audit import audit_log
from src.utils.background_service import BaseBackgroundService
from src.utils.type_guards import is_valid_config

if TYPE_CHECKING:
    from src.config.config_schema_defs import LLMConfig
    from pathlib import Path
    from src.bot import Bot, BotConfig
    from src.channels.base import BaseChannel
    from src.llm import LLMProvider
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
        "react_loop_timeout",
        "max_concurrent_messages",
        "scheduler_require_hmac",
    }
)

# Field names (or substrings) whose values must be redacted in audit logs.
CONFIG_SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "api_key",
        "secret",
        "password",
        "access_token",
        "refresh_token",
        "auth_token",
        "api_token",
        "credential",
    }
)

_REDACTED = "****"


def _redact_if_sensitive(field_name: str, value: Any) -> str:
    """Return a redacted string if *field_name* is sensitive."""
    name_lower = field_name.lower()
    if any(s in name_lower for s in CONFIG_SENSITIVE_FIELDS):
        return _REDACTED
    val_str = str(value)
    if len(val_str) > 200:
        val_str = val_str[:197] + "..."
    return val_str


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


def _build_diff_dict(
    old_flat: Dict[str, Any],
    new_flat: Dict[str, Any],
    safe: Set[str],
    destructive: Set[str],
    unknown: Set[str],
) -> Dict[str, Any]:
    """Build a structured diff dict suitable for event emission and audit logging."""
    changed_keys = safe | destructive | unknown
    diffs: Dict[str, Dict[str, Any]] = {}
    for key in sorted(changed_keys):
        diffs[key] = {"old": old_flat.get(key), "new": new_flat.get(key)}

    return {
        "safe_changed": sorted(safe),
        "destructive_changed": sorted(destructive),
        "unknown_changed": sorted(unknown),
        "diffs": diffs,
    }


async def _emit_config_changed_event(diff: Dict[str, Any]) -> None:
    """Emit a ``config_changed`` event with the full structured diff.

    Emission is fire-and-forget: failures are logged as non-critical and
    never break the hot-reload pipeline.
    """
    try:
        await get_event_bus().emit(
            Event(
                name=EVENT_CONFIG_CHANGED,
                data=diff,
                source="ConfigWatcher._reload_config",
            )
        )
    except Exception:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            "Failed to emit config_changed event",
            logger=log,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Change applier callback protocol
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigApplier(Protocol):
    """Structural type for objects that apply config changes."""

    def apply(self, old_config: Config, new_config: Config) -> dict[str, Any] | None: ...


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
        on_config_swap: Callable[[Config], None] | None = None,
    ) -> None:
        self._config = app_config
        self._bot = bot
        self._channel = channel
        self._llm = llm
        self._shutdown_mgr = shutdown_mgr
        self._reconfigure_logging = reconfigure_logging
        self._on_config_swap = on_config_swap

    def apply(self, old_config: Config, new_config: Config) -> dict[str, Any] | None:
        """Detect changes between *old_config* and *new_config* and apply safe ones.

        The config reference is swapped **before** component updates so that
        concurrent coroutines reading ``self._config`` never observe a state
        where components have new values but the reference still points to the
        old config.  Component updates are idempotent — if a component update
        fails, the config reference has already been swapped and the system
        remains in a consistent (new-config, partially-updated-components)
        state, which is preferable to the old (old-config, new-components)
        inconsistency.

        Returns:
            A structured diff dict with ``safe_changed``, ``destructive_changed``,
            ``unknown_changed``, and ``diffs`` keys, or ``None`` if no changes
            were detected.
        """
        old_flat = _flatten_config(old_config)
        new_flat = _flatten_config(new_config)
        safe, destructive, unknown = _diff_configs(old_flat, new_flat)

        if not safe and not destructive and not unknown:
            return None

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

        # Build structured diff for event emission
        diff = _build_diff_dict(old_flat, new_flat, safe, destructive, unknown)

        # Audit log each changed field with before/after values (redacted).
        changed_keys = safe | destructive | unknown
        for field_path in sorted(changed_keys):
            old_val = old_flat.get(field_path)
            new_val = new_flat.get(field_path)
            audit_log(
                event="config_change",
                details={
                    "field_name": field_path,
                    "old_value": _redact_if_sensitive(field_path, old_val),
                    "new_value": _redact_if_sensitive(field_path, new_val),
                    "trigger_source": "config_watcher_hot_reload",
                },
            )

        # Apply safe changes
        if not safe:
            return diff

        # Swap the config reference FIRST so that self._config always
        # reflects the latest state for concurrent readers.
        self._update_app_config(new_config)

        # Capture the old LLM config BEFORE the swap for use by
        # _apply_llm_config, which must preserve destructive fields
        # from the old config even though self._config now points to new.
        old_llm_cfg = old_config.llm

        # Then propagate to individual components — each is isolated so a
        # failure in one does not prevent others from being applied.
        failed_appliers: list[str] = []
        applier_names: list[str] = []
        applier_calls: list[tuple[str, object]] = [
            ("bot", lambda: self._apply_bot_config(new_config, safe)),
            ("channel", lambda: self._apply_channel_config(new_config, safe)),
            ("llm", lambda: self._apply_llm_config(new_config, safe, old_llm_cfg=old_llm_cfg)),
            ("shutdown", lambda: self._apply_shutdown_config(new_config, safe)),
            ("logging", lambda: self._apply_logging_config(old_config, new_config, safe)),
            ("shell", lambda: self._apply_shell_config(new_config, safe)),
        ]

        for name, apply_fn in applier_calls:
            try:
                apply_fn()
            except Exception:
                failed_appliers.append(name)
                log.exception(
                    "Failed to apply %s config changes — other components will continue",
                    name,
                )

        for field_path in sorted(safe):
            log.info("Applied config change: '%s'", field_path)

        if failed_appliers:
            log.warning(
                "Config hot-reload partially failed: %d/%d component appliers succeeded, "
                "failed: %s",
                len(applier_calls) - len(failed_appliers),
                len(applier_calls),
                ", ".join(sorted(failed_appliers)),
            )

        # Structured per-component outcome for monitoring dashboards.
        diff["applier_results"] = {
            name: ("failed" if name in failed_appliers else "ok")
            for name, _ in applier_calls
        }

        return diff

    # ── Per-component appliers ────────────────────────────────────────────

    def _apply_bot_config(self, new_config: Config, changed: Set[str]) -> None:
        """Rebuild ``BotConfig`` and apply via :meth:`Bot.update_config`."""
        from src.bot import BotConfig

        bot_fields = {
            "llm.max_tool_iterations",
            "llm.system_prompt_prefix",
            "memory_max_history",
            "per_chat_timeout",
            "react_loop_timeout",
            "max_concurrent_messages",
        }
        if not bot_fields & changed:
            return

        new_bot_cfg = BotConfig(
            max_tool_iterations=new_config.llm.max_tool_iterations,
            memory_max_history=new_config.memory_max_history,
            system_prompt_prefix=new_config.llm.system_prompt_prefix,
            stream_response=self._bot._cfg.stream_response,  # destructive — keep old
            per_chat_timeout=new_config.per_chat_timeout,
            react_loop_timeout=new_config.react_loop_timeout,
            max_concurrent_messages=new_config.max_concurrent_messages,
        )
        self._bot.update_config(new_bot_cfg)

    def _apply_channel_config(self, new_config: Config, changed: Set[str]) -> None:
        """Delegate channel-specific config changes to the channel."""
        self._channel.apply_channel_config(new_config, changed)

    def _apply_llm_config(
        self,
        new_config: Config,
        changed: Set[str],
        *,
        old_llm_cfg: LLMConfig,
    ) -> None:
        """Update LLM client config (temperature, timeout).

        Only safe LLM fields are forwarded to the provider.  Destructive
        fields (model, api_key, etc.) are preserved from the *old* config
        (via ``old_llm_cfg`` captured before the swap) so that the live
        component never sees an unintended destructive change.
        """
        if "llm.temperature" not in changed and "llm.timeout" not in changed:
            return

        from dataclasses import replace as _dc_replace

        # Use the OLD LLM config as the base and overlay only safe fields.
        # This ensures destructive fields (model, api_key, etc.) are never
        # propagated to the live LLM provider, even though self._config
        # has already been swapped to the new config.
        safe_cfg = _dc_replace(
            old_llm_cfg,
            temperature=new_config.llm.temperature,
            timeout=new_config.llm.timeout,
        )
        self._llm.update_config(safe_cfg)

    def _apply_shutdown_config(self, new_config: Config, changed: Set[str]) -> None:
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
        """Atomically replace the application-level config reference.

        Validates *new_config* via :func:`is_valid_config` before swapping as
        a defense-in-depth check.  Even though ``ConfigWatcher._load_new_config``
        validates at load time, this guard ensures that any future code path
        that bypasses the loader cannot apply an invalid config to live
        components.

        Uses a single reference swap instead of mutating individual fields so
        that concurrent coroutines never observe a partially-updated config
        (e.g. new ``llm.temperature`` but old ``llm.timeout``).  Mirrors the
        pattern in :meth:`Bot.update_config` and :meth:`LLMClient.update_config`.
        """
        if not is_valid_config(new_config):
            log.error(
                "Refusing to apply invalid config — one or more required "
                "fields are missing or have wrong types. Keeping current config."
            )
            raise ConfigurationError(
                "Config validation failed before live update",
                config_key="config_watcher",
            )

        # Atomic reference swap — single attribute assignment is safe under the
        # GIL and guarantees no coroutine sees a hybrid old+new config state.
        self._config = new_config

        # Propagate to Application._config if a swap callback was provided.
        if self._on_config_swap is not None:
            self._on_config_swap(new_config)


# ─────────────────────────────────────────────────────────────────────────────
# ConfigWatcher — polling-based file watcher
# ─────────────────────────────────────────────────────────────────────────────


class ConfigWatcher(BaseBackgroundService):
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

    _service_name = "Config watcher"

    def __init__(
        self,
        config_path: Path,
        current_config: Config,
        applier: ConfigApplier,
        *,
        poll_interval: float = CONFIG_WATCH_INTERVAL_SECONDS,
        debounce: float = CONFIG_WATCH_DEBOUNCE_SECONDS,
    ) -> None:
        super().__init__()
        self._config_path = config_path
        self._current_config = current_config
        self._applier = applier
        self._poll_interval = poll_interval
        self._debounce = debounce

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
            _apply_env_overrides(config)

            return config
        except Exception as exc:
            log.error(
                "Config hot-reload failed: invalid config in %s — %s: %s. Keeping current config.",
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
        diff = self._applier.apply(old_config, new_config)

        # Emit structured config_changed event for audit trailing.
        if diff is not None:
            await _emit_config_changed_event(diff)

        # Update tracked state
        self._current_config = new_config
        self._last_mtime = self._read_mtime()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the config watcher background task."""
        super().start()

    async def _run_loop(self) -> None:
        """Run the watch loop (required by BaseBackgroundService)."""
        await self._watch_loop()
