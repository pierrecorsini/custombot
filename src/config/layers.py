"""
config/layers.py — Layered configuration with documented merge priority.

Splits monolithic config.json into layered configs:
  1. defaults  — shipped with the app (lowest priority)
  2. bundled   — bundled overrides from the package
  3. user      — user overrides in workspace/ (config.json)
  4. env       — environment variable overrides (highest priority)

Layers are merged in priority order: later layers overwrite earlier ones.
The user layer supports hot-reload via the existing ConfigWatcher.

Usage::

    manager = ConfigLayerManager(workspace=Path("workspace"))
    config_dict = manager.get_effective_config()
    config = _from_dict(Config, config_dict)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.config.config_schema_defs import Config

log = logging.getLogger(__name__)

# Environment variable prefix for config overrides.
# E.g. CUSTOMBOT_LLM__MODEL=gpt-4o → {"llm": {"model": "gpt-4o"}}
_ENV_PREFIX = "CUSTOMBOT_"
_ENV_SEPARATOR = "__"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict.

    Dict values are merged recursively; non-dict values from *override*
    replace those in *base*.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _collect_env_overrides() -> dict[str, Any]:
    """Parse CUSTOMBOT_* environment variables into a nested dict.

    Double-underscore separates nesting levels:
        CUSTOMBOT_LLM__MODEL=gpt-4o → {"llm": {"model": "gpt-4o"}}
    """
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        remainder = key[len(_ENV_PREFIX):].lower()
        parts = remainder.split(_ENV_SEPARATOR)
        current = result
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
    return result


def _generate_defaults() -> dict[str, Any]:
    """Generate default config from the Config dataclass defaults."""
    return asdict(Config())


class ConfigLayerManager:
    """Manage layered configuration loading and merging.

    Layers (merge priority low → high):
      1. defaults — shipped with the app
      2. bundled  — package-level overrides
      3. user     — workspace/config.json
      4. env      — environment variable overrides
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._defaults_path = Path(__file__).parent / "defaults.json"
        self._user_path = workspace / "config.json"

    def load_all_layers(self) -> dict[str, Any]:
        """Load and merge all layers into a single dict."""
        # Layer 1: defaults (generated from dataclass defaults)
        if self._defaults_path.is_file():
            try:
                defaults = json.loads(self._defaults_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("Failed to load defaults.json — using generated defaults")
                defaults = _generate_defaults()
        else:
            defaults = _generate_defaults()

        # Layer 2: bundled overrides (if any)
        bundled_path = Path(__file__).parent / "bundled.json"
        if bundled_path.is_file():
            try:
                bundled = json.loads(bundled_path.read_text(encoding="utf-8"))
                defaults = _deep_merge(defaults, bundled)
            except (json.JSONDecodeError, OSError):
                log.debug("No bundled.json overrides or failed to load")

        # Layer 3: user overrides
        if self._user_path.is_file():
            try:
                user = json.loads(self._user_path.read_text(encoding="utf-8"))
                defaults = _deep_merge(defaults, user)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Failed to load user config %s: %s", self._user_path, exc)

        # Layer 4: env overrides (highest priority)
        env_overrides = _collect_env_overrides()
        if env_overrides:
            defaults = _deep_merge(defaults, env_overrides)
            log.debug("Applied %d env config overrides", len(env_overrides))

        return defaults

    def get_effective_config(self) -> dict[str, Any]:
        """Return the final merged config dict across all layers."""
        return self.load_all_layers()

    def generate_defaults_file(self) -> None:
        """Write defaults.json from the current Config dataclass defaults."""
        defaults = _generate_defaults()
        # Remove the schema version if present
        defaults.pop("$schema", None)
        self._defaults_path.parent.mkdir(parents=True, exist_ok=True)
        self._defaults_path.write_text(
            json.dumps(defaults, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log.info("Generated defaults.json at %s", self._defaults_path)
