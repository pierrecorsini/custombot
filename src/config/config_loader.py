"""
config_loader.py — Configuration loading, saving, and construction.

Handles JSON file I/O, dict → dataclass construction, environment variable
overrides, and runtime type validation. Depends on the data model in
config_schema_defs.py and validation helpers in config_validation.py.

Public API: load_config, save_config.
Internal helpers: _from_dict, _load_and_validate_file, _apply_env_overrides,
_validate_config_type.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, fields
from dataclasses import MISSING as dataclasses_MISSING
from typing import Any, Dict, Type, TypeVar, get_type_hints, TYPE_CHECKING

from src.config.config_schema_defs import (
    CONFIG_PATH,
    Config,
    add_schema_version,
    format_validation_errors,
    validate_config_dict,
)
from src.config.config_validation import (
    _check_deprecated_options,
    _check_unknown_keys,
    _log_default_values_used,
    _log_effective_config,
    _log_validation_errors,
)
from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Dict → dataclass construction
# ─────────────────────────────────────────────────────────────────────────────


def _from_dict(cls: Type[T], data: dict[str, Any]) -> T:
    """Recursively instantiate a dataclass from a plain dict."""
    if not isinstance(data, dict):
        cls_name = getattr(cls, "__name__", cls)
        log.warning(
            "Expected dict for %s but got %s — malformed config section",
            cls_name,
            type(data).__name__,
        )
        raise ConfigurationError(
            f"Expected dict for {cls_name} but got {type(data).__name__}",
            config_key=cls_name,
        )
    # Resolve string annotations caused by `from __future__ import annotations`
    try:
        hints = get_type_hints(cls)
    except Exception:
        log_noncritical(
            NonCriticalCategory.TYPE_RESOLUTION,
            f"Failed to resolve type hints for {getattr(cls, '__name__', cls)} during deserialization",
            logger=log,
        )
        hints = {}
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        val = data.get(f.name)
        if val is None:
            # Use the field's default / default_factory
            if f.default_factory is not dataclasses_MISSING:
                kwargs[f.name] = f.default_factory()
            elif f.default is not dataclasses_MISSING:
                kwargs[f.name] = f.default
            # else left as missing → dataclass will raise, which is intentional
        else:
            # Recurse if the resolved type is itself a dataclass
            ftype = hints.get(f.name)
            if isinstance(ftype, type) and hasattr(ftype, "__dataclass_fields__"):
                kwargs[f.name] = _from_dict(ftype, val)
            else:
                kwargs[f.name] = val
    return cls(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# File I/O helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_and_validate_file(path: Path) -> dict[str, Any]:
    """Load, validate, and return config dict from a JSON file."""
    log.debug("Reading config file: %s", path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        log.debug("Successfully parsed JSON from %s", path)
    except json.JSONDecodeError as exc:
        log.error("Failed to parse JSON from %s: %s", path, exc)
        raise

    deprecation_warnings = _check_deprecated_options(data, path)
    if deprecation_warnings:
        log.warning("Found %d deprecated option(s) in %s", len(deprecation_warnings), path)

    _log_default_values_used(data, path)

    validation_result = validate_config_dict(data)
    if not validation_result["valid"]:
        _log_validation_errors(validation_result["errors"], path)
        log.error(
            "Full validation error report:\n%s",
            format_validation_errors(validation_result["errors"]),
        )
        from src.exceptions import ConfigurationError

        raise ConfigurationError(
            f"Invalid configuration in {path}",
            errors=validation_result["errors"],
            error_count=len(validation_result["errors"]),
        )

    return data


def _apply_env_overrides(config: Config) -> None:
    """Apply environment variable overrides to a Config in-place."""
    env_api_key = os.environ.get("OPENAI_API_KEY")
    if env_api_key:
        config.llm.api_key = env_api_key
        log.debug("Using OPENAI_API_KEY from environment variable")

    env_base_url = os.environ.get("OPENAI_BASE_URL")
    if env_base_url:
        config.llm.base_url = env_base_url
        log.debug("Using OPENAI_BASE_URL from environment variable")


def _validate_config_type(config: Config, path: Path) -> None:
    """Run runtime type validation on a Config object."""
    log.debug("Performing runtime type validation")
    from src.utils.type_guards import is_valid_config

    if not is_valid_config(config):
        log.error("Runtime type validation failed for config from %s", path)
        raise ValueError(f"Invalid configuration loaded from {path}")
    log.debug("Runtime type validation passed")


# ─────────────────────────────────────────────────────────────────────────────
# Public API: load / save
# ─────────────────────────────────────────────────────────────────────────────


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load config from JSON; missing keys fall back to defaults.

    Validates the loaded JSON against the schema before constructing
    the Config object. Raises ConfigurationError for invalid config.

    Args:
        path: Path to the config file (default: config.json).

    Returns:
        Config object with validated values.

    Raises:
        ConfigurationError: If the config file fails schema validation.
        json.JSONDecodeError: If the file contains invalid JSON.
    """
    log.debug("Loading configuration from %s", path)

    # Collect raw dict: empty when file missing, parsed JSON otherwise
    data: dict[str, Any] = {}

    if not path.exists():
        log.info("Config file %s not found, using defaults", path)
    else:
        data = _load_and_validate_file(path)

    # Warn about unknown keys before construction
    if data:
        _check_unknown_keys(data, path)

    # Unified construction via _from_dict for both paths
    log.debug("Constructing Config object from %s", "file" if data else "defaults")
    config = _from_dict(Config, data)

    # Environment variable overrides (always applied)
    _apply_env_overrides(config)

    # Runtime type validation
    _validate_config_type(config, path)

    _log_effective_config(config, path)
    log.debug("Configuration loaded successfully from %s", path)
    return config


def save_config(config: Config, path: Path = CONFIG_PATH) -> None:
    """Save config to JSON file with schema validation.

    Validates the config against the JSON schema before saving.
    The saved file includes a $schema field for version identification.

    Args:
        config: Config object to save.
        path: Path to save the config file (default: config.json).

    Raises:
        ConfigurationError: If the config fails schema validation.
    """
    log.info("Saving configuration to %s", path)

    data = asdict(config)

    # Validate before saving
    log.debug("Validating configuration before save")
    validation_result = validate_config_dict(data)
    if not validation_result["valid"]:
        log.error("Cannot save invalid configuration to %s", path)
        _log_validation_errors(validation_result["errors"], path)

        from src.exceptions import ConfigurationError

        raise ConfigurationError(
            "Cannot save invalid configuration",
            errors=validation_result["errors"],
            error_count=len(validation_result["errors"]),
        )

    log.debug("Configuration validation passed for save")

    # Add schema version for future compatibility
    data_with_schema = add_schema_version(data)

    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: temp file → rename prevents corruption on crash
    log.debug("Writing configuration to %s", path)
    from src.utils.async_file import sync_atomic_write

    sync_atomic_write(path, json.dumps(data_with_schema, indent=2, ensure_ascii=False))

    log.info("Configuration saved successfully to %s", path)
