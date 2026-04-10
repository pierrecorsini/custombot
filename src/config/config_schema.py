"""
config_schema.py — JSON Schema validation for configuration files.

Provides schema-based validation for config.json with clear, field-level
error messages. Supports schema versioning for future compatibility.

Usage:
    from src.config.config_schema import validate_config, ConfigValidationError

    # Validate a dict before saving
    errors = validate_config(config_dict)
    if errors:
        raise ConfigValidationError(errors)

    # Validate with schema version check
    validate_config_with_version(config_dict, schema_version="1.0")
"""

from __future__ import annotations

from typing import Any, List, Optional, TypedDict

# Schema version for future compatibility
SCHEMA_VERSION = "1.0"


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
                    "maximum": 100,
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
    },
    "required": ["llm", "whatsapp"],
    "additionalProperties": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Validation Functions
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


def _validate_type(
    value: Any, expected_type: str, path: str
) -> Optional[ValidationError]:
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


def _validate_string_constraints(
    value: str, schema: dict, path: str
) -> List[ValidationError]:
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
                "number": lambda v: isinstance(v, (int, float))
                and not isinstance(v, bool),
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
    """
    Validate a configuration dictionary against the schema.

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
    """
    Validate configuration and raise exception on errors.

    Args:
        data: Configuration dictionary to validate.

    Raises:
        ConfigValidationError: If validation fails.
    """
    result = validate_config_dict(data)
    if not result["valid"]:
        raise ConfigValidationError(result["errors"], result["schema_version"])


def format_validation_errors(errors: List[ValidationError]) -> str:
    """
    Format validation errors into a human-readable string.

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


class ConfigValidationError(Exception):
    """
    Exception raised when configuration validation fails.

    Provides detailed, field-level error information for debugging.

    Attributes:
        errors: List of ValidationError dicts with path, message, value.
        schema_version: Version of the schema that was validated against.
    """

    def __init__(
        self, errors: List[ValidationError], schema_version: str = SCHEMA_VERSION
    ) -> None:
        self.errors = errors
        self.schema_version = schema_version
        self.message = format_validation_errors(errors)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def get_field_errors(self, field_path: str) -> List[ValidationError]:
        """Get all errors for a specific field path."""
        return [e for e in self.errors if e["path"] == field_path]


# ─────────────────────────────────────────────────────────────────────────────
# Schema Version Helpers
# ─────────────────────────────────────────────────────────────────────────────


def get_schema_version(data: dict[str, Any]) -> Optional[str]:
    """
    Extract schema version from config data.

    Args:
        data: Configuration dictionary.

    Returns:
        Schema version string or None if not specified.
    """
    return data.get("$schema")


def add_schema_version(data: dict[str, Any]) -> dict[str, Any]:
    """
    Add schema version to config data.

    Args:
        data: Configuration dictionary.

    Returns:
        Config data with $schema field added.
    """
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
    "CONFIG_SCHEMA",
    "SCHEMA_VERSION",
    "ConfigValidationError",
    "ValidationError",
    "ValidationResult",
    "validate_config",
    "validate_config_dict",
    "validate_config_dict_strict",
    "format_validation_errors",
    "get_schema_version",
    "add_schema_version",
]
