"""
config_schema_defs.py — Configuration schema: dataclass definitions and JSON Schema validation.

Canonical location for all config schema concerns:
  - Dataclass definitions (Config, LLMConfig, WhatsAppConfig, etc.)
  - JSON Schema dict for config file validation
  - Validation functions and ConfigValidationError exception
  - Deprecated / renamed option tracking
  - Schema version helpers

Public classes: LLMConfig, NeonizeConfig, ShellConfig, MiddlewareConfig,
WhatsAppConfig, Config, ConfigValidationError, ValidationError, ValidationResult.
Public functions: validate_config_dict, validate_config_dict_strict,
format_validation_errors, get_schema_version, add_schema_version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TypedDict
from urllib.parse import urlparse

from src.constants import (
    DEFAULT_CHAT_LOCK_CACHE_SIZE,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_LOCK_EVICTION_POLICY,
    DEFAULT_MAX_CONCURRENT_MESSAGES,
    DEFAULT_MEMORY_MAX_HISTORY,
    DEFAULT_PER_CHAT_TIMEOUT,
    DEFAULT_PER_CHAT_TOKEN_TRACKING_SIZE,
    DEFAULT_REACT_LOOP_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    MAX_TOOL_ITERATIONS,
    WORKSPACE_DIR,
)
from src.exceptions import ConfigurationError

# Default config file path — re-exported by config.py and __init__.py.
CONFIG_PATH = __import__("pathlib").Path(f"{WORKSPACE_DIR}/config.json")

# Schema version for future compatibility
SCHEMA_VERSION = "1.0"


# ─────────────────────────────────────────────────────────────────────────────
# TypedDict definitions for validation results
# ─────────────────────────────────────────────────────────────────────────────


class ValidationError(TypedDict):
    """A single validation error with field path and message."""

    path: str
    message: str
    value: Optional[Any]


class ValidationResult(TypedDict):
    """Result of schema validation."""

    valid: bool
    errors: List[ValidationError]
    schema_version: str


# ─────────────────────────────────────────────────────────────────────────────
# Deprecated / renamed option tracking
# ─────────────────────────────────────────────────────────────────────────────

# Options that are deprecated and will be removed in future versions.
# Format: option_path -> (removal_version, suggestion)
DEPRECATED_OPTIONS: Dict[str, Tuple[str, str]] = {
    # Example (not currently deprecated, shown for future reference):
    # "llm.legacy_mode": ("2.0", "Remove this option; legacy mode is no longer supported"),
}

# Options that have been renamed.
# Format: old_path -> new_path
RENAMED_OPTIONS: Dict[str, str] = {
    # Example: "whatsapp.bridge_url": "whatsapp.neonize.db_path",
}


# ─────────────────────────────────────────────────────────────────────────────
# JSON Schema Definition
# ─────────────────────────────────────────────────────────────────────────────

CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://custombot.local/schemas/config.json",
    "title": "CustomBot Configuration",
    "description": "Configuration schema for CustomBot WhatsApp assistant",
    "type": "object",
    "properties": {
        "$schema": {
            "type": "string",
            "description": "Schema version identifier for future compatibility",
        },
        "llm": {
            "type": "object",
            "description": "LLM provider configuration",
            "properties": {
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Model identifier (e.g., 'gpt-4o', 'claude-3-opus')",
                },
                "base_url": {
                    "type": "string",
                    "format": "uri",
                    "description": "API base URL for the LLM provider",
                },
                "api_key": {
                    "type": "string",
                    "description": "API key for authentication (can be empty for local models)",
                },
                "temperature": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "Sampling temperature (0-2, higher = more random)",
                },
                "max_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 128000,
                    "description": "Maximum tokens in LLM response (optional - if not set, API default is used)",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "Timeout in seconds for LLM API calls",
                },
                "system_prompt_prefix": {
                    "type": "string",
                    "description": "Optional prefix prepended before instruction files in the system prompt. Leave empty to rely entirely on .md instruction files for personality.",
                },
                "max_tool_iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Maximum tool call iterations in ReAct loop",
                },
                "embedding_model": {
                    "type": "string",
                    "minLength": 1,
                    "description": "OpenAI embedding model for vector memory (e.g., 'text-embedding-3-small')",
                },
                "embedding_dimensions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3072,
                    "description": "Dimension of the embedding vectors (must match the model output size)",
                },
                "embedding_base_url": {
                    "type": "string",
                    "description": "Separate API base URL for embedding calls (defaults to llm.base_url if empty)",
                },
                "embedding_api_key": {
                    "type": "string",
                    "description": "Separate API key for embedding calls (defaults to llm.api_key if empty)",
                },
                "stream_response": {
                    "type": "boolean",
                    "description": "Stream LLM responses token-by-token to reduce perceived latency (default: false)",
                },
            },
            "required": ["model"],
            "additionalProperties": False,
        },
        "whatsapp": {
            "type": "object",
            "description": "WhatsApp channel configuration",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["neonize"],
                    "description": "WhatsApp provider (only 'neonize' supported)",
                },
                "neonize": {
                    "type": "object",
                    "description": "Neonize WhatsApp client configuration",
                    "properties": {
                        "db_path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Path to SQLite session database",
                        },
                    },
                    "additionalProperties": False,
                },
                "allowed_numbers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": r"^\d+$",
                    },
                    "description": "Allowed phone numbers (E164 format, no +)",
                },
                "allow_all": {
                    "type": "boolean",
                    "description": "Allow all senders (no ACL filtering)",
                },
            },
            "required": ["provider", "neonize"],
            "additionalProperties": False,
        },
        "memory_max_history": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "Maximum messages to include in LLM context",
        },
        "load_history": {
            "type": "boolean",
            "description": "Whether to process historical/offline messages that arrived before the bot connected (default: false)",
        },
        "skills_auto_load": {
            "type": "boolean",
            "description": "Auto-load skills from user directory on startup",
        },
        "skills_user_directory": {
            "type": "string",
            "minLength": 1,
            "description": "Directory for user-authored skill files",
        },
        "log_incoming_messages": {
            "type": "boolean",
            "description": "Log incoming messages to console",
        },
        "log_routing_info": {
            "type": "boolean",
            "description": "Log routing rule matching details",
        },
        "shutdown_timeout": {
            "type": "number",
            "minimum": 1,
            "maximum": 300,
            "description": "Graceful shutdown timeout in seconds",
        },
        "log_format": {
            "type": "string",
            "enum": ["text", "json"],
            "description": "Logging format: text (human-readable) or json (structured)",
        },
        "log_file": {
            "type": "string",
            "description": "Path to log file for file logging (empty = no file logging)",
        },
        "log_max_bytes": {
            "type": "integer",
            "minimum": 1024,
            "maximum": 1073741824,
            "description": "Maximum log file size in bytes before rotation (default: 10MB)",
        },
        "log_backup_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Number of backup log files to keep (default: 5)",
        },
        "log_verbosity": {
            "type": "string",
            "enum": ["quiet", "normal", "verbose"],
            "description": "Logging verbosity: quiet (errors only), normal (balanced), verbose (debug)",
        },
        "log_llm": {
            "type": "boolean",
            "description": "Enable per-file logging of each LLM request and response (default: false)",
        },
        "max_chat_lock_cache_size": {
            "type": "integer",
            "minimum": 10,
            "maximum": 100000,
            "description": "Maximum per-chat lock cache entries before LRU eviction (default: 1000). Raise for deployments with >1000 concurrent chats.",
        },
        "max_chat_lock_eviction_policy": {
            "type": "string",
            "enum": ["grow", "reject_on_full"],
            "description": "Eviction policy when the per-chat lock cache is full and all entries are in-use. 'grow' (default) allows unbounded growth with a warning; 'reject_on_full' raises RuntimeError to prevent memory bloat.",
        },
        "shell": {
            "type": "object",
            "description": "Shell skill security configuration — command allowlist/denylist",
            "properties": {
                "command_denylist": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": "Additional command patterns to block beyond built-in denylist (regex patterns)",
                },
                "command_allowlist": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": "Command patterns that bypass the denylist (regex patterns, allowlist takes precedence)",
                },
            },
            "additionalProperties": False,
        },
        "middleware": {
            "type": "object",
            "description": "Middleware pipeline configuration",
            "properties": {
                "middleware_order": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": "Ordered list of built-in middleware names (empty uses default order)",
                },
                "extra_middleware_paths": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "description": "Dotted import paths for custom middleware factories",
                },
            },
            "additionalProperties": False,
        },
        "max_thread_pool_workers": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 256,
            "description": "Maximum worker threads for asyncio ThreadPoolExecutor (null uses system default)",
        },
        "max_concurrent_messages": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "Maximum messages processed concurrently by _on_message() (default: 10). Caps memory usage and LLM rate-limit pressure under load.",
        },
        "per_chat_timeout": {
            "type": "number",
            "minimum": 0,
            "maximum": 3600,
            "description": "Per-chat processing timeout in seconds (default: 300). Wraps the entire message processing pipeline (context assembly + ReAct loop + response delivery) in asyncio.wait_for(). Set to 0 to disable.",
        },
        "per_chat_token_tracking_size": {
            "type": "integer",
            "minimum": 10,
            "maximum": 100000,
            "description": "Maximum distinct chat_ids tracked in TokenUsage per-chat LRU cache before eviction (default: 1000). Uses half-eviction: oldest 50% are removed when full. Raise for high-volume deployments.",
        },
        "react_loop_timeout": {
            "type": "number",
            "minimum": 0,
            "maximum": 3600,
            "description": "Wall-clock timeout in seconds for the full ReAct loop (default: 180). Checked between iterations; when exceeded the loop terminates gracefully. Set to 0 to disable. Must be less than per_chat_timeout.",
        },
    },
    "required": ["llm", "whatsapp"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass definitions
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
    embedding_base_url: str = ""
    embedding_api_key: str = ""
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
    # Maximum number of per-chat locks retained in the LRU cache.
    # Controls how many concurrent chats can have cached locks before eviction.
    # Raise for deployments with >1000 concurrent active chats.
    max_chat_lock_cache_size: int = DEFAULT_CHAT_LOCK_CACHE_SIZE
    # Eviction policy when the per-chat lock cache is full and all entries are in-use.
    # "grow" allows unbounded growth with a warning (default, safe for correctness).
    # "reject_on_full" raises RuntimeError to prevent memory bloat.
    max_chat_lock_eviction_policy: str = DEFAULT_LOCK_EVICTION_POLICY.value
    # Maximum number of messages processed concurrently by _on_message().
    # Caps memory usage and LLM rate-limit pressure under load without blocking
    # the event loop — excess messages await a free slot via asyncio.Semaphore.
    max_concurrent_messages: int = DEFAULT_MAX_CONCURRENT_MESSAGES
    # Per-chat processing timeout (seconds).  Wraps the entire _process() call
    # (context assembly + ReAct loop + response delivery) inside
    # asyncio.wait_for().  When exceeded, the stuck turn is cancelled and the
    # per-chat lock is released.  Set to 0 to disable the timeout.
    per_chat_timeout: float = DEFAULT_PER_CHAT_TIMEOUT
    # Maximum distinct chat_ids tracked in TokenUsage._per_chat before LRU eviction.
    # Uses BoundedOrderedDict with half-eviction.  Raise for high-volume deployments.
    per_chat_token_tracking_size: int = DEFAULT_PER_CHAT_TOKEN_TRACKING_SIZE
    # Wall-clock timeout (seconds) for the full ReAct loop — a single stuck LLM
    # call or infinite tool loop can block the event loop indefinitely.  The
    # deadline is checked between iterations; when exceeded the loop terminates
    # gracefully with a user-facing message.  Set to 0 to disable.  Must be
    # less than per_chat_timeout so the outer asyncio.wait_for() is the last
    # resort, not the primary guard.
    react_loop_timeout: float = DEFAULT_REACT_LOOP_TIMEOUT

    def __repr__(self) -> str:
        from dataclasses import asdict

        from src.config.config_validation import _redact_secrets

        return f"Config({_redact_secrets(asdict(self))})"


# ─────────────────────────────────────────────────────────────────────────────
# JSON Schema Validation Functions
# ─────────────────────────────────────────────────────────────────────────────


def _format_path(path: List[str | int]) -> str:
    """Format a JSON path list into a dot-notation string."""
    if not path:
        return "root"
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            if result:
                result += f".{part}"
            else:
                result = part
    return result


def _validate_type(value: Any, expected_type: str, path: str) -> Optional[ValidationError]:
    """Validate that a value matches the expected type."""
    type_checks = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }

    checker = type_checks.get(expected_type)
    if checker and not checker(value):
        return {
            "path": path,
            "message": f"Expected type '{expected_type}', got '{type(value).__name__}'",
            "value": value,
        }
    return None


def _is_valid_uri(value: str) -> bool:
    """Check whether a string is a valid RFC 3986 URI with a scheme and host."""
    try:
        result = urlparse(value)
        return bool(result.scheme and result.netloc)
    except Exception:
        return False


def _validate_string_constraints(value: str, schema: dict, path: str) -> List[ValidationError]:
    """Validate string-specific constraints."""
    errors: List[ValidationError] = []

    if "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(
            {
                "path": path,
                "message": f"String length {len(value)} is less than minimum {schema['minLength']}",
                "value": value,
            }
        )

    if "pattern" in schema:
        import re

        if not re.match(schema["pattern"], value):
            errors.append(
                {
                    "path": path,
                    "message": f"String does not match pattern '{schema['pattern']}'",
                    "value": value,
                }
            )

    fmt = schema.get("format")
    if fmt == "uri" and value and not _is_valid_uri(value):
        errors.append(
            {
                "path": path,
                "message": f"String is not a valid URI (must include scheme and host, e.g. 'https://api.example.com')",
                "value": value,
            }
        )

    return errors


def _validate_number_constraints(
    value: int | float, schema: dict, path: str
) -> List[ValidationError]:
    """Validate number-specific constraints."""
    errors: List[ValidationError] = []

    if "minimum" in schema and value < schema["minimum"]:
        errors.append(
            {
                "path": path,
                "message": f"Value {value} is less than minimum {schema['minimum']}",
                "value": value,
            }
        )

    if "maximum" in schema and value > schema["maximum"]:
        errors.append(
            {
                "path": path,
                "message": f"Value {value} is greater than maximum {schema['maximum']}",
                "value": value,
            }
        )

    return errors


def _validate_against_schema(
    data: Any, schema: dict, path: List[str | int], errors: List[ValidationError]
) -> None:
    """Recursively validate data against a JSON Schema subset."""
    current_path = _format_path(path)

    # Type validation
    if "type" in schema:
        expected_type = schema["type"]

        # Handle union types (e.g., ["integer", "null"])
        if isinstance(expected_type, list):
            type_checks = {
                "string": lambda v: isinstance(v, str),
                "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                "boolean": lambda v: isinstance(v, bool),
                "array": lambda v: isinstance(v, list),
                "object": lambda v: isinstance(v, dict),
                "null": lambda v: v is None,
            }

            type_valid = False
            for t in expected_type:
                if t in type_checks and type_checks[t](data):
                    type_valid = True
                    break

            if not type_valid:
                errors.append(
                    {
                        "path": current_path,
                        "message": f"Value does not match any of the expected types: {expected_type}",
                        "value": data,
                    }
                )
                return  # Don't validate further if type is wrong
        else:
            type_error = _validate_type(data, expected_type, current_path)
            if type_error:
                errors.append(type_error)
                return  # Don't validate further if type is wrong

    # Handle different types
    if isinstance(data, dict) and schema.get("type") == "object":
        # Check required properties
        required = schema.get("required", [])
        for req in required:
            if req not in data:
                errors.append(
                    {
                        "path": f"{current_path}.{req}",
                        "message": f"Required property '{req}' is missing",
                        "value": None,
                    }
                )

        # Validate properties
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key.startswith("$"):
                continue  # Skip schema metadata
            if key in properties:
                _validate_against_schema(value, properties[key], path + [key], errors)
            elif not schema.get("additionalProperties", True):
                errors.append(
                    {
                        "path": f"{current_path}.{key}",
                        "message": f"Unknown property '{key}' (additional properties not allowed)",
                        "value": value,
                    }
                )

    elif isinstance(data, list) and schema.get("type") == "array":
        items_schema = schema.get("items", {})
        for i, item in enumerate(data):
            _validate_against_schema(item, items_schema, path + [i], errors)

    elif isinstance(data, str) and schema.get("type") == "string":
        errors.extend(_validate_string_constraints(data, schema, current_path))

    elif isinstance(data, (int, float)) and schema.get("type") in ("number", "integer"):
        errors.extend(_validate_number_constraints(data, schema, current_path))

    # Enum validation
    if "enum" in schema and data not in schema["enum"]:
        errors.append(
            {
                "path": current_path,
                "message": f"Value must be one of: {schema['enum']}",
                "value": data,
            }
        )


def validate_config_dict(data: dict[str, Any]) -> ValidationResult:
    """Validate a configuration dictionary against the schema.

    Args:
        data: Configuration dictionary to validate.

    Returns:
        ValidationResult with valid flag, errors list, and schema version.
    """
    errors: List[ValidationError] = []
    _validate_against_schema(data, CONFIG_SCHEMA, [], errors)

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "schema_version": SCHEMA_VERSION,
    }


def validate_config_dict_strict(data: dict[str, Any]) -> None:
    """Validate configuration and raise exception on errors.

    Args:
        data: Configuration dictionary to validate.

    Raises:
        ConfigValidationError: If validation fails.
    """
    result = validate_config_dict(data)
    if not result["valid"]:
        raise ConfigValidationError(result["errors"], result["schema_version"])


def format_validation_errors(errors: List[ValidationError]) -> str:
    """Format validation errors into a human-readable string.

    Args:
        errors: List of validation errors.

    Returns:
        Formatted error message string.
    """
    if not errors:
        return "No validation errors"

    lines = ["Configuration validation failed:"]
    for i, error in enumerate(errors, 1):
        lines.append(f"  {i}. [{error['path']}] {error['message']}")
        if error["value"] is not None:
            # Truncate long values
            val_str = str(error["value"])
            if len(val_str) > 50:
                val_str = val_str[:47] + "..."
            lines.append(f"     Value: {val_str}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Exception
# ─────────────────────────────────────────────────────────────────────────────


class ConfigValidationError(ConfigurationError):
    """Exception raised when configuration validation fails.

    Provides detailed, field-level error information for debugging.
    Inherits from ConfigurationError for consistent hierarchy.

    Attributes:
        errors: List of ValidationError dicts with path, message, value.
        schema_version: Version of the schema that was validated against.
    """

    def __init__(self, errors: List[ValidationError], schema_version: str = SCHEMA_VERSION) -> None:
        self.errors = errors
        self.schema_version = schema_version
        self.message = format_validation_errors(errors)
        super().__init__(message=self.message)

    def __str__(self) -> str:
        return self.message

    def get_field_errors(self, field_path: str) -> List[ValidationError]:
        """Get all errors for a specific field path."""
        return [e for e in self.errors if e["path"] == field_path]


# ─────────────────────────────────────────────────────────────────────────────
# Schema Version Helpers
# ─────────────────────────────────────────────────────────────────────────────


def get_schema_version(data: dict[str, Any]) -> Optional[str]:
    """Extract schema version from config data."""
    return data.get("$schema")


def add_schema_version(data: dict[str, Any]) -> dict[str, Any]:
    """Add schema version to config data."""
    return {
        "$schema": f"https://custombot.local/schemas/config-{SCHEMA_VERSION}.json",
        **data,
    }


# Alias for backward compatibility
validate_config = validate_config_dict

# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Dataclass definitions
    "Config",
    "LLMConfig",
    "NeonizeConfig",
    "ShellConfig",
    "MiddlewareConfig",
    "WhatsAppConfig",
    # Constants
    "CONFIG_PATH",
    "CONFIG_SCHEMA",
    "SCHEMA_VERSION",
    "DEPRECATED_OPTIONS",
    "RENAMED_OPTIONS",
    # Validation types
    "ValidationError",
    "ValidationResult",
    # Validation functions
    "validate_config",
    "validate_config_dict",
    "validate_config_dict_strict",
    "format_validation_errors",
    "get_schema_version",
    "add_schema_version",
    # Exception
    "ConfigValidationError",
]
