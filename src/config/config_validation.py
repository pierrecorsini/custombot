"""
config_validation.py — Configuration validation and logging helpers.

Pure validation functions and logging helpers that operate on config dicts
and Config objects. No file I/O — that lives in config_loader.py.

Exported helpers: _check_deprecated_options, _collect_known_field_names,
_check_unknown_keys, _get_default_values, _log_default_values_used,
_redact_secrets, _log_effective_config, _get_suggestion_for_error,
_log_validation_errors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple, Type, get_type_hints

from src.config.config_schema_defs import (
    DEPRECATED_OPTIONS,
    RENAMED_OPTIONS,
    Config,
)
from src.constants import DEFAULT_LLM_TIMEOUT, MAX_TOOL_ITERATIONS, WORKSPACE_DIR
from src.core.errors import NonCriticalCategory, log_noncritical

log = logging.getLogger(__name__)

__all__ = [
    "_check_deprecated_options",
    "_check_unknown_keys",
    "_collect_known_field_names",
    "_get_default_values",
    "_get_suggestion_for_error",
    "_log_default_values_used",
    "_log_effective_config",
    "_log_validation_errors",
    "_redact_secrets",
]


# ─────────────────────────────────────────────────────────────────────────────
# Deprecated option checks
# ─────────────────────────────────────────────────────────────────────────────


def _check_deprecated_options(data: Dict[str, Any], file_path: Path) -> List[str]:
    """Check for deprecated options in config data.

    Args:
        data: Configuration dictionary to check.
        file_path: Path to the config file (for logging).

    Returns:
        List of deprecation warning messages.
    """
    warnings: List[str] = []

    def check_recursive(obj: Any, path: str = "") -> None:
        if not isinstance(obj, dict):
            return

        for key, value in obj.items():
            full_path = f"{path}.{key}" if path else key

            # Check for deprecated options
            if full_path in DEPRECATED_OPTIONS:
                removal_version, suggestion = DEPRECATED_OPTIONS[full_path]
                msg = (
                    f"Option '{full_path}' in {file_path} is deprecated "
                    f"and will be removed in version {removal_version}. "
                    f"Suggestion: {suggestion}"
                )
                warnings.append(msg)
                log.warning(msg)

            # Check for renamed options
            if full_path in RENAMED_OPTIONS:
                new_path = RENAMED_OPTIONS[full_path]
                msg = (
                    f"Option '{full_path}' in {file_path} has been renamed to '{new_path}'. "
                    f"Please update your configuration file."
                )
                warnings.append(msg)
                log.warning(msg)

            # Recurse into nested objects
            if isinstance(value, dict):
                check_recursive(value, full_path)

    check_recursive(data)
    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Unknown-key detection
# ─────────────────────────────────────────────────────────────────────────────


def _collect_known_field_names(cls: Type) -> Dict[str, List[str]]:
    """Recursively collect known field names for a dataclass hierarchy.

    Returns:
        Mapping of dot-path → list of valid field names at that level.
        The root level uses the empty string as key.
    """
    result: Dict[str, List[str]] = {}

    def _collect(dc: Type, path: str = "") -> None:
        try:
            hints = get_type_hints(dc)
        except Exception:
            log_noncritical(
                NonCriticalCategory.TYPE_RESOLUTION,
                "Failed to resolve type hints for %s, falling back to empty hints",
                getattr(dc, "__name__", dc),
                logger=log,
            )
            hints = {}

        names = [f.name for f in fields(dc)]  # type: ignore[arg-type]
        result[path] = names

        for f in fields(dc):  # type: ignore[arg-type]
            ftype = hints.get(f.name)
            if isinstance(ftype, type) and hasattr(ftype, "__dataclass_fields__"):
                child = f"{path}.{f.name}" if path else f.name
                _collect(ftype, child)

    _collect(cls)
    return result


def _check_unknown_keys(data: dict, file_path: Path) -> None:
    """Warn about unknown keys in *data* by comparing against known dataclass fields.

    Uses :func:`difflib.get_close_matches` to suggest likely typos.
    """
    from difflib import get_close_matches

    known = _collect_known_field_names(Config)

    def _check(obj: dict, parent: str = "") -> None:
        valid = known.get(parent, [])
        for key in list(obj):
            if key.startswith("$"):  # schema metadata
                continue
            if key not in valid:
                full = f"{parent}.{key}" if parent else key
                matches = get_close_matches(key, valid, n=1, cutoff=0.6)
                if matches:
                    log.warning(
                        "Unknown config key '%s' in %s — did you mean '%s'?",
                        full,
                        file_path,
                        matches[0],
                    )
                else:
                    log.warning(
                        "Unknown config key '%s' in %s — not recognised, will be ignored",
                        full,
                        file_path,
                    )
            elif isinstance(obj[key], dict):
                child = f"{parent}.{key}" if parent else key
                if child in known:
                    _check(obj[key], child)

    _check(data)


# ─────────────────────────────────────────────────────────────────────────────
# Default value helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_default_values() -> Dict[str, Any]:
    """Get all default configuration values.

    Returns:
        Dictionary mapping option paths to their default values.
    """
    return {
        "llm.model": "gpt-4o",
        "llm.base_url": "https://api.openai.com/v1",
        "llm.temperature": 0.7,
        "llm.timeout": DEFAULT_LLM_TIMEOUT,
        "llm.max_tool_iterations": MAX_TOOL_ITERATIONS,
        "llm.embedding_model": "text-embedding-3-small",
        "llm.embedding_dimensions": 1536,
        "llm.embedding_base_url": "",
        "llm.embedding_api_key": "",
        "whatsapp.provider": "neonize",
        "whatsapp.neonize.db_path": f"{WORKSPACE_DIR}/whatsapp_session.db",
        "load_history": False,
        "skills_user_directory": f"{WORKSPACE_DIR}/skills",
        "log_incoming_messages": True,
        "log_routing_info": False,
        "shutdown_timeout": 4.0,
        "log_format": "text",
        "log_file": f"{WORKSPACE_DIR}/logs/custombot.log",
        "log_max_bytes": 10 * 1024 * 1024,
        "log_backup_count": 5,
        "log_verbosity": "normal",
        "log_llm": False,
    }


def _log_default_values_used(data: Dict[str, Any], file_path: Path) -> None:
    """Log which default values are being used for missing options."""
    defaults = _get_default_values()

    def get_nested_value(obj: Dict[str, Any], path: str) -> Tuple[bool, Any]:
        """Get a nested value from a dict using dot notation."""
        parts = path.split(".")
        current = obj
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    used_defaults: List[str] = []

    for option_path, default_value in defaults.items():
        exists, _ = get_nested_value(data, option_path)
        if not exists:
            used_defaults.append(f"  {option_path} = {default_value!r}")

    if used_defaults:
        log.debug(
            "Using default values for %d options not specified in %s",
            len(used_defaults),
            file_path,
        )
        for default_line in used_defaults:
            log.debug("Default: %s", default_line)


# ─────────────────────────────────────────────────────────────────────────────
# Secret redaction
# ─────────────────────────────────────────────────────────────────────────────


def _redact_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a copy of config data with secrets redacted for safe logging."""
    _SECRET_SUBSTRINGS: Tuple[str, ...] = (
        "api_key",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "auth_token",
        "api_token",
        "credential",
    )

    def _is_secret_key(key: str) -> bool:
        k = key.lower()
        return any(s in k for s in _SECRET_SUBSTRINGS)

    def redact_recursive(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                key: "***REDACTED***" if _is_secret_key(key) else redact_recursive(value)
                for key, value in obj.items()
            }
        elif isinstance(obj, list):
            return [redact_recursive(item) for item in obj]
        else:
            return obj

    return redact_recursive(data)


# ─────────────────────────────────────────────────────────────────────────────
# Effective config logging
# ─────────────────────────────────────────────────────────────────────────────


def _log_effective_config(config: Config, file_path: Path) -> None:
    """Log the effective configuration with secrets redacted."""
    config_dict = asdict(config)
    redacted = _redact_secrets(config_dict)

    log.debug("Configuration loaded from %s", file_path)
    log.debug("  LLM: model=%s, base_url=%s", config.llm.model, config.llm.base_url)
    log.debug(
        "  LLM: temperature=%.2f, max_tokens=%s, timeout=%.1fs",
        config.llm.temperature,
        config.llm.max_tokens if config.llm.max_tokens else "auto",
        config.llm.timeout,
    )
    log.debug("  WhatsApp: provider=%s", config.whatsapp.provider)
    log.debug(
        "  WhatsApp: db_path=%s",
        config.whatsapp.neonize.db_path,
    )
    log.debug(
        "  General: workspace=%s, memory_max_history=%d",
        WORKSPACE_DIR,
        config.memory_max_history,
    )
    log.debug(
        "  Skills: auto_load=%s, user_directory=%s",
        config.skills_auto_load,
        config.skills_user_directory,
    )
    log.debug(
        "  Logging: format=%s, verbosity=%s, incoming=%s, routing=%s",
        config.log_format,
        config.log_verbosity,
        config.log_incoming_messages,
        config.log_routing_info,
    )
    log.debug(
        "  Thread Pool: max_workers=%s",
        config.max_thread_pool_workers if config.max_thread_pool_workers else "default",
    )
    if config.log_file:
        log.debug(
            "  Log rotation: file=%s, max_bytes=%d, backup_count=%d",
            config.log_file,
            config.log_max_bytes,
            config.log_backup_count,
        )
    log.debug("Full redacted config: %s", json.dumps(redacted, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Validation error logging
# ─────────────────────────────────────────────────────────────────────────────


def _get_suggestion_for_error(error_path: str, error_message: str) -> str:
    """Get a helpful suggestion for a validation error."""
    suggestions: Dict[str, str] = {
        "llm.model": "Use a valid model name like 'gpt-4o', 'gpt-4-turbo', or 'gpt-3.5-turbo'",
        "llm.base_url": "Ensure the URL is a valid HTTP/HTTPS URL (e.g., 'https://api.openai.com/v1')",
        "llm.temperature": "Set temperature between 0 and 2 (default: 0.7)",
        "llm.timeout": "Set timeout between 1 and 600 seconds (default: 120)",
        "whatsapp.provider": "Only 'neonize' provider is supported",
        "whatsapp.neonize.db_path": "Use a path like 'workspace/whatsapp_session.db'",
        "log_format": "Use 'text' for human-readable logs or 'json' for structured logs",
    }

    if error_path in suggestions:
        return suggestions[error_path]

    for path_prefix, suggestion in suggestions.items():
        if error_path.startswith(path_prefix):
            return suggestion

    return ""


def _log_validation_errors(errors: List[Dict[str, Any]], file_path: Path) -> None:
    """Log validation errors with helpful suggestions."""
    log.error(
        "Configuration validation failed for %s with %d error(s)",
        file_path,
        len(errors),
    )

    for i, error in enumerate(errors, 1):
        path = error.get("path", "unknown")
        message = error.get("message", "Unknown error")
        value = error.get("value")

        log.error("  Error %d: [%s] %s", i, path, message)
        if value is not None:
            val_str = str(value)
            if len(val_str) > 50:
                val_str = val_str[:47] + "..."
            log.error("           Value: %s", val_str)

        suggestion = _get_suggestion_for_error(path, message)
        if suggestion:
            log.error("           Suggestion: %s", suggestion)
