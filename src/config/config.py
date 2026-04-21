"""
config.py — Configuration dataclasses with JSON loading/saving.

Uses stdlib dataclasses (no extra deps). Nested structs are mapped
automatically so the full config.json is cleanly round-tripped.

Schema validation ensures config files are valid before loading/saving.

Logging:
    - Logs configuration load with source file
    - Logs each validation step
    - Logs warnings for deprecated options
    - Logs effective configuration (with secrets redacted)
    - Logs validation errors with suggestions
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from dataclasses import MISSING as dataclasses_MISSING
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type, TypeVar, get_type_hints

from src.config.config_schema import (
    add_schema_version,
    format_validation_errors,
    validate_config_dict,
)

# Logger for configuration validation
log = logging.getLogger(__name__)
from src.constants import (
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MEMORY_MAX_HISTORY,
    DEFAULT_SHUTDOWN_TIMEOUT,
    MAX_TOOL_ITERATIONS,
    WORKSPACE_DIR,
)

CONFIG_PATH = Path(f"{WORKSPACE_DIR}/config.json")

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Deprecated Options Tracking
# ─────────────────────────────────────────────────────────────────────────────

# Options that are deprecated and will be removed in future versions
# Format: option_path -> (removal_version, suggestion)
DEPRECATED_OPTIONS: Dict[str, Tuple[str, str]] = {
    # Example (not currently deprecated, shown for future reference):
    # "llm.legacy_mode": ("2.0", "Remove this option; legacy mode is no longer supported"),
}

# Options that have been renamed
# Format: old_path -> new_path
RENAMED_OPTIONS: Dict[str, str] = {
    # Example: "whatsapp.bridge_url": "whatsapp.neonize.db_path",
}


def _check_deprecated_options(data: Dict[str, Any], file_path: Path) -> List[str]:
    """
    Check for deprecated options in config data.

    Args:
        data: Configuration dictionary to check.
        file_path: Path to the config file (for logging).

    Returns:
        List of deprecation warning messages.
    """
    warnings = []

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


def _get_default_values() -> Dict[str, Any]:
    """
    Get all default configuration values.

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
        "whatsapp.provider": "neonize",
        "whatsapp.neonize.db_path": f"{WORKSPACE_DIR}/whatsapp_session.db",
        "load_history": False,
        "skills_user_directory": f"{WORKSPACE_DIR}/skills",
        "log_incoming_messages": True,
        "log_routing_info": False,
        "shutdown_timeout": DEFAULT_SHUTDOWN_TIMEOUT,
        "log_format": "text",
        "log_file": f"{WORKSPACE_DIR}/logs/custombot.log",
        "log_max_bytes": 10 * 1024 * 1024,
        "log_backup_count": 5,
        "log_verbosity": "normal",
        "log_llm": False,
    }


def _log_default_values_used(data: Dict[str, Any], file_path: Path) -> None:
    """
    Log which default values are being used for missing options.

    Args:
        data: Configuration dictionary loaded from file.
        file_path: Path to the config file (for logging).
    """
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
        exists, value = get_nested_value(data, option_path)
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


def _redact_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a copy of config data with secrets redacted for safe logging.

    Args:
        data: Configuration dictionary.

    Returns:
        Copy of data with sensitive values redacted.
    """
    # Fields that should be redacted
    SECRET_FIELDS: Set[str] = {"api_key", "password", "secret", "token", "credential"}

    def redact_recursive(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                key: "***REDACTED***" if key in SECRET_FIELDS else redact_recursive(value)
                for key, value in obj.items()
            }
        elif isinstance(obj, list):
            return [redact_recursive(item) for item in obj]
        else:
            return obj

    return redact_recursive(data)


def _log_effective_config(config: "Config", file_path: Path) -> None:
    """
    Log the effective configuration with secrets redacted.

    Args:
        config: The loaded Config object.
        file_path: Path to the config file (for logging).
    """
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


def _get_suggestion_for_error(error_path: str, error_message: str) -> str:
    """
    Get a helpful suggestion for a validation error.

    Args:
        error_path: The path to the invalid field.
        error_message: The validation error message.

    Returns:
        A suggestion string, or empty string if no specific suggestion.
    """
    suggestions: Dict[str, str] = {
        "llm.model": "Use a valid model name like 'gpt-4o', 'gpt-4-turbo', or 'gpt-3.5-turbo'",
        "llm.base_url": "Ensure the URL is a valid HTTP/HTTPS URL (e.g., 'https://api.openai.com/v1')",
        "llm.temperature": "Set temperature between 0 and 2 (default: 0.7)",
        "llm.timeout": "Set timeout between 1 and 600 seconds (default: 120)",
        "whatsapp.provider": "Only 'neonize' provider is supported",
        "whatsapp.neonize.db_path": "Use a path like 'workspace/whatsapp_session.db'",
        "log_format": "Use 'text' for human-readable logs or 'json' for structured logs",
    }

    # Check for exact match first
    if error_path in suggestions:
        return suggestions[error_path]

    # Check for partial matches
    for path_prefix, suggestion in suggestions.items():
        if error_path.startswith(path_prefix):
            return suggestion

    return ""


def _log_validation_errors(errors: List[Dict[str, Any]], file_path: Path) -> None:
    """
    Log validation errors with helpful suggestions.

    Args:
        errors: List of validation error dictionaries.
        file_path: Path to the config file (for logging).
    """
    log.error(
        "Configuration validation failed for %s with %d error(s)",
        file_path,
        len(errors),
    )

    for i, error in enumerate(errors, 1):
        path = error.get("path", "unknown")
        message = error.get("message", "Unknown error")
        value = error.get("value")

        # Log the error details
        log.error("  Error %d: [%s] %s", i, path, message)
        if value is not None:
            # Truncate long values
            val_str = str(value)
            if len(val_str) > 50:
                val_str = val_str[:47] + "..."
            log.error("           Value: %s", val_str)

        # Add suggestion if available
        suggestion = _get_suggestion_for_error(path, message)
        if suggestion:
            log.error("           Suggestion: %s", suggestion)


def _from_dict(cls: Type[T], data: dict) -> T:
    """Recursively instantiate a dataclass from a plain dict."""
    if not isinstance(data, dict):
        return cls()
    # Resolve string annotations caused by `from __future__ import annotations`
    try:
        hints = get_type_hints(cls)
    except Exception:
        hints = {}
    kwargs: dict = {}
    for f in fields(cls):  # type: ignore[arg-type]
        val = data.get(f.name)
        if val is None:
            # Use the field's default / default_factory
            if f.default_factory is not dataclasses_MISSING:  # type: ignore[attr-defined]
                kwargs[f.name] = f.default_factory()
            elif f.default is not dataclasses_MISSING:  # type: ignore[attr-defined]
                kwargs[f.name] = f.default
            # else left as missing → dataclass will raise, which is intentional
        else:
            # Recurse if the resolved type is itself a dataclass
            ftype = hints.get(f.name)
            if isinstance(ftype, type) and hasattr(ftype, "__dataclass_fields__"):
                kwargs[f.name] = _from_dict(ftype, val)
            else:
                kwargs[f.name] = val
    return cls(**kwargs)  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────────────
# Sub-configs
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LLMConfig:
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: Optional[int] = None  # Optional: only sent to API if set
    timeout: float = DEFAULT_LLM_TIMEOUT  # Default timeout in seconds for LLM calls
    system_prompt_prefix: str = ""
    max_tool_iterations: int = MAX_TOOL_ITERATIONS
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    # When True, LLM responses are streamed token-by-token to reduce perceived
    # latency.  Falls back to non-streaming for tool-call turns.  Not all
    # providers support streaming — disable if the provider returns errors.
    stream_response: bool = False

    def __repr__(self) -> str:
        if self.api_key:
            key_masked = f"***({len(self.api_key)} chars)"
        else:
            key_masked = "NOT_SET"
        return f"LLMConfig(model={self.model!r}, base_url={self.base_url!r}, api_key={key_masked!r}, temp={self.temperature})"


@dataclass
class NeonizeConfig:
    """Neonize — native Python WhatsApp client via whatsmeow (Go)."""

    db_path: str = f"{WORKSPACE_DIR}/whatsapp_session.db"

    def __repr__(self) -> str:
        return f"NeonizeConfig(db_path={self.db_path!r})"


@dataclass
class ShellConfig:
    """Shell skill security configuration — command allowlist/denylist."""

    # Additional command patterns to block beyond the built-in denylist.
    # Each entry is a regex pattern matched against the full command string.
    command_denylist: List[str] = field(default_factory=list)
    # Command patterns that bypass the denylist (allowlist takes precedence).
    # If a command matches any allowlist pattern, it is allowed even if it
    # would otherwise be blocked by the denylist.
    command_allowlist: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"ShellConfig(denylist={len(self.command_denylist)} patterns, "
            f"allowlist={len(self.command_allowlist)} patterns)"
        )


@dataclass
class MiddlewareConfig:
    """Middleware pipeline configuration.

    Allows customizing the message-processing middleware chain without
    editing source code.  Built-in middleware names are referenced by
    string; custom middleware can be added via dotted import paths.

    Built-in names:
        operation_tracker, metrics, inbound_logging, preflight,
        typing, error_handler, handle_message
    """

    # Ordered list of built-in middleware names to include.
    # When empty (default), the full built-in order is used.
    middleware_order: List[str] = field(default_factory=list)
    # Dotted import paths for custom middleware factories
    # (e.g. ``"my_package.middleware:my_factory"``).
    extra_middleware_paths: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"MiddlewareConfig(order={self.middleware_order or 'default'}, "
            f"extra={len(self.extra_middleware_paths)})"
        )


@dataclass
class WhatsAppConfig:
    provider: str = "neonize"
    neonize: NeonizeConfig = field(default_factory=NeonizeConfig)
    # If non-empty, only these numbers (e164, no +) will be answered
    allowed_numbers: List[str] = field(default_factory=list)
    # Must be explicitly set to False to reject messages when allowed_numbers is empty.
    # Defaults to True for backward compatibility (original behavior: accept all when list is empty).
    allow_all: bool = True

    def __repr__(self) -> str:
        nums = f"{len(self.allowed_numbers)} numbers" if self.allowed_numbers else "all"
        return f"WhatsAppConfig(provider={self.provider!r}, allowed={nums}, allow_all={self.allow_all}, neonize={self.neonize!r})"


# ─────────────────────────────────────────────────────────────────────────────
# Root config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    middleware: MiddlewareConfig = field(default_factory=MiddlewareConfig)
    # Whether to process historical/offline messages that arrived before the bot connected
    load_history: bool = False
    # How many past messages to include in LLM context
    memory_max_history: int = DEFAULT_MEMORY_MAX_HISTORY
    # Whether to auto-load skills from skills_user_directory on startup
    skills_auto_load: bool = True
    # Directory for user-authored skill files (Python or skill.md)
    skills_user_directory: str = field(default_factory=lambda: f"{WORKSPACE_DIR}/skills")
    # Logging options
    log_incoming_messages: bool = True  # Log incoming messages to console
    log_routing_info: bool = False  # Log routing rule matching details
    # Graceful shutdown timeout (seconds) - force quit after this
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT
    # Logging format: "text" (human-readable) or "json" (structured for aggregation)
    log_format: str = "text"
    # Log rotation configuration - defaults to workspace/logs/custombot.log
    log_file: str = field(default_factory=lambda: f"{WORKSPACE_DIR}/logs/custombot.log")
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB max file size before rotation
    log_backup_count: int = 5  # Number of backup log files to keep
    # Logging verbosity: "quiet" (errors only), "normal" (balanced), "verbose" (debug)
    log_verbosity: str = "normal"
    # LLM request/response logging: one JSON file per request and per response
    log_llm: bool = False
    # Maximum worker threads for the asyncio ThreadPoolExecutor.
    # Controls concurrency for asyncio.to_thread() calls (DB, file I/O, vector
    # memory).  None means use DEFAULT_THREAD_POOL_WORKERS from constants.
    max_thread_pool_workers: Optional[int] = None

    def __repr__(self) -> str:
        return (
            f"Config(llm={self.llm!r}, whatsapp={self.whatsapp!r}, "
            f"shell={self.shell!r}, middleware={self.middleware!r}, "
            f"memory_max_history={self.memory_max_history})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Load / Save helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_and_validate_file(path: Path) -> dict:
    """Load, validate, and return config dict from a JSON file."""
    log.debug("Reading config file: %s", path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        log.debug("Successfully parsed JSON from %s", path)
    except json.JSONDecodeError as e:
        log.error("Failed to parse JSON from %s: %s", path, e)
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


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Load config from JSON; missing keys fall back to defaults.

    Validates the loaded JSON against the schema before constructing
    the Config object. Raises ConfigurationError for invalid config.

    Logging includes:
        - Configuration load with source file
        - Each validation step
        - Warnings for deprecated options
        - Effective configuration (with secrets redacted)
        - Validation errors with suggestions

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
    data: dict = {}

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

    Logging includes:
        - Save operation attempt
        - Validation status
        - Success/failure result

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

    # Write to file
    log.debug("Writing configuration to %s", path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data_with_schema, fh, indent=2)

    log.info("Configuration saved successfully to %s", path)
