"""
json_utils.py — Safe JSON parsing utilities.

Provides robust JSON parsing with error handling, type validation,
and optional default values on failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional, TypeVar, Union

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class JsonParseResult:
    """Result of JSON parsing with error information."""

    success: bool
    data: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None  # "decode", "type", "read"


def safe_json_parse(
    data: str,
    default: Optional[T] = None,
    expected_type: type = dict,
    log_errors: bool = True,
) -> Union[dict, list, str, T]:
    """
    Safely parse JSON string with error handling and type validation.

    Args:
        data: JSON string to parse.
        default: Value to return on parse failure. Defaults to None.
        expected_type: Expected type of parsed result (dict, list, or str).
                       If parsed value doesn't match, returns default.
        log_errors: Whether to log parse errors. Defaults to True.

    Returns:
        Parsed JSON value if successful and matches expected_type.
        Otherwise returns the default value.

    Examples:
        >>> safe_json_parse('{"key": "value"}')
        {'key': 'value'}

        >>> safe_json_parse('invalid json', default={})
        {}

        >>> safe_json_parse('["a", "b"]', expected_type=list)
        ['a', 'b']

        >>> safe_json_parse('{"key": "value"}', expected_type=list, default=[])
        []
    """
    try:
        result = json.loads(data)
        if not isinstance(result, expected_type):
            if log_errors:
                log.warning(
                    "JSON type mismatch: expected %s, got %s",
                    expected_type.__name__,
                    type(result).__name__,
                )
            return default if default is not None else expected_type()
        return result
    except json.JSONDecodeError as e:
        if log_errors:
            log.warning("JSON parse error: %s", str(e)[:100])
        return default if default is not None else expected_type()
    except Exception as e:
        if log_errors:
            log.error("Unexpected error parsing JSON: %s", str(e)[:100])
        return default if default is not None else expected_type()


def safe_json_parse_with_error(
    data: str,
    expected_type: type = dict,
) -> JsonParseResult:
    """
    Parse JSON string and return detailed result with error information.

    Use this for validation scenarios where you need to know the specific
    error type and message.

    Args:
        data: JSON string to parse.
        expected_type: Expected type of parsed result.

    Returns:
        JsonParseResult with success status, data, and error details.

    Examples:
        >>> result = safe_json_parse_with_error('{"key": "value"}')
        >>> result.success
        True
        >>> result.data
        {'key': 'value'}

        >>> result = safe_json_parse_with_error('invalid')
        >>> result.success
        False
        >>> result.error_type
        'decode'
    """
    try:
        result = json.loads(data)
        if not isinstance(result, expected_type):
            return JsonParseResult(
                success=False,
                error=f"Expected {expected_type.__name__}, got {type(result).__name__}",
                error_type="type",
            )
        return JsonParseResult(success=True, data=result)
    except json.JSONDecodeError as e:
        return JsonParseResult(
            success=False,
            error=str(e),
            error_type="decode",
        )
    except Exception as e:
        return JsonParseResult(
            success=False,
            error=str(e),
            error_type="read",
        )


def safe_json_parse_line(
    line: str,
    default: Optional[T] = None,
    log_errors: bool = True,
) -> Union[dict, T]:
    """
    Parse a single JSON line (for JSONL files) with error handling.

    Convenience wrapper for parsing JSONL lines where dict is expected.

    Args:
        line: Single line containing JSON.
        default: Value to return on parse failure.
        log_errors: Whether to log parse errors.

    Returns:
        Parsed dict or default value.
    """
    line = line.strip()
    if not line:
        return default if default is not None else {}
    return safe_json_parse(
        line, default=default or {}, expected_type=dict, log_errors=log_errors
    )


__all__ = [
    "safe_json_parse",
    "safe_json_parse_with_error",
    "safe_json_parse_line",
    "JsonParseResult",
]
