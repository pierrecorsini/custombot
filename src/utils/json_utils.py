"""
json_utils.py — Fast JSON utilities with orjson acceleration and msgpack backend.

Provides high-performance JSON serialization/deserialization using orjson
when available, with transparent stdlib json fallback. All hot-path JSON
operations (database, message queue, index) should use json_dumps/json_loads
from this module.

Also provides a msgpack binary backend for non-human-readable persistence
where binary encoding is smaller and faster than JSON.  The msgpack backend
is opt-in — callers explicitly call ``msgpack_dumps`` / ``msgpack_loads``.

Evaluation: msgpack vs orjson for tool-call result payloads
-----------------------------------------------------------
Benchmarked on Python 3.13 / Windows (1000 iterations each).

Direct serialization (dict → bytes):
  small   (~80B):   orjson 0.3µs vs msgpack 5.2µs  — orjson 17x faster
  medium  (~1.5KB): orjson 0.8µs vs msgpack 5.6µs  — orjson  7x faster
  large   (~45KB):  orjson 23µs  vs msgpack 21µs    — msgpack 7% faster
  text-heavy (~45KB): orjson 40µs vs msgpack 20µs   — msgpack 2x faster

Full pipeline (serialize → load → final JSON — the actual data flow):
  small:   orjson 0.7µs vs msgpack 6.5µs  — orjson 9x faster
  medium:  orjson 1.9µs vs msgpack 7.4µs  — orjson 4x faster
  large:   orjson 44µs  vs msgpack 51µs   — orjson 16% faster
  text-heavy: orjson 66µs vs msgpack 67µs  — tied

Conclusion: orjson wins decisively in the real pipeline because tool results
must end up as JSON (for LLM API calls and JSONL persistence), making msgpack's
double-serialization overhead a net negative.  msgpack is not adopted for this
path.  See ``tests/unit/bench_serialization.py`` for the full benchmark.

Also provides a unified safe_json_parse() with mode-based error handling:
- LENIENT: Returns parsed value or default on failure (default mode).
- STRICT: Returns JsonParseResult with detailed error information.
- LINE: Like LENIENT but strips whitespace and skips empty lines (for JSONL).
"""

from __future__ import annotations

import enum
import json as _stdlib_json
import logging
from dataclasses import dataclass
from typing import Any, Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# ── Fast JSON backend: orjson with stdlib fallback ──────────────────────

try:
    import orjson as _orjson

    _HAS_ORJSON = True

    def json_dumps(
        obj: Any,
        *,
        ensure_ascii: bool = False,
        indent: int | None = None,
        default: Any = None,
    ) -> str:
        """Serialize obj to a JSON string using orjson for speed.

        Falls back to stdlib json if orjson fails on unsupported types.
        The ``default`` callable is forwarded to both backends for custom
        serialization of otherwise unsupported types.
        """
        opt = 0
        if not ensure_ascii:
            pass  # no special flags needed
        try:
            if indent:
                opt |= _orjson.OPT_INDENT_2
            return _orjson.dumps(obj, option=opt, default=default).decode("utf-8")
        except (TypeError, _orjson.JSONEncodeError):
            return _stdlib_json.dumps(
                obj, ensure_ascii=ensure_ascii, indent=indent, default=default
            )

    def json_loads(s: str | bytes) -> Any:
        """Deserialize a JSON string using orjson for speed.

        Falls back to stdlib json if orjson fails.
        """
        try:
            return _orjson.loads(s)
        except (ValueError, _orjson.JSONDecodeError):
            return _stdlib_json.loads(s)

except ImportError:
    _HAS_ORJSON = False

    def json_dumps(
        obj: Any,
        *,
        ensure_ascii: bool = False,
        indent: int | None = None,
        default: Any = None,
    ) -> str:
        """Serialize obj to a JSON string using stdlib json."""
        return _stdlib_json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=default)

    def json_loads(s: str | bytes) -> Any:
        """Deserialize a JSON string using stdlib json."""
        return _stdlib_json.loads(s)


# Re-export json.JSONDecodeError so callers can catch it without knowing the backend.
JSONDecodeError = _stdlib_json.JSONDecodeError


# ── Binary backend: msgpack for non-human-readable persistence ────────────

try:
    import msgpack as _msgpack

    _HAS_MSGPACK = True

    def msgpack_dumps(obj: Any) -> bytes:
        """Serialize *obj* to msgpack bytes.

        Uses ``use_bin_type=True`` so bytes values round-trip correctly.
        """
        return _msgpack.packb(obj, use_bin_type=True)

    def msgpack_loads(data: bytes) -> Any:
        """Deserialize msgpack *data* back to Python objects.

        Uses ``raw=False`` so strings come back as ``str``, not ``bytes``.
        """
        return _msgpack.unpackb(data, raw=False)

except ImportError:
    _HAS_MSGPACK = False

    def msgpack_dumps(obj: Any) -> bytes:  # type: ignore[misc]
        """Fallback: serialize to JSON bytes when msgpack is not installed."""
        return json_dumps(obj).encode("utf-8")

    def msgpack_loads(data: bytes) -> Any:  # type: ignore[misc]
        """Fallback: deserialize from JSON bytes when msgpack is not installed."""
        return json_loads(data)


# ── Parse mode enum ────────────────────────────────────────────────────


class JsonParseMode(enum.Enum):
    """Modes for safe JSON parsing.

    LENIENT: Returns parsed value or default on failure. Logs errors.
    STRICT: Returns JsonParseResult with success/error details.
    LINE: Like LENIENT but strips whitespace and skips empty lines (for JSONL).
    """

    LENIENT = "lenient"
    STRICT = "strict"
    LINE = "line"


# ── Parse result ───────────────────────────────────────────────────────


@dataclass
class JsonParseResult:
    """Result of JSON parsing with error information."""

    success: bool
    data: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None  # "decode", "type", "read"


# ── Unified safe parser ────────────────────────────────────────────────


def safe_json_parse(
    data: str,
    default: Optional[T] = None,
    expected_type: type = dict,
    log_errors: bool = True,
    mode: str | JsonParseMode = "lenient",
) -> Any:
    """Safely parse JSON with configurable error handling via mode.

    Args:
        data: JSON string to parse.
        default: Value to return on parse failure (LENIENT/LINE modes only).
        expected_type: Expected type of parsed result.
        log_errors: Whether to log parse errors (LENIENT/LINE modes only).
        mode: Parse mode — "lenient" (default), "strict", or "line".
            Accepts str or JsonParseMode enum.

    Returns:
        LENIENT/LINE: Parsed JSON value or default on failure.
        STRICT: JsonParseResult with success status and error details.

    Examples:
        >>> safe_json_parse('{"key": "value"}')
        {'key': 'value'}

        >>> safe_json_parse('invalid json', default={})
        {}

        >>> safe_json_parse('["a", "b"]', expected_type=list)
        ['a', 'b']

        >>> result = safe_json_parse('{"key": "val"}', mode="strict")
        >>> result.success
        True

        >>> safe_json_parse('  {"key": "val"}  \\n', mode="line")
        {'key': 'val'}
    """
    if isinstance(mode, str):
        mode = JsonParseMode(mode)

    # LINE mode: strip whitespace and short-circuit empty lines
    if mode == JsonParseMode.LINE:
        data = data.strip()
        if not data:
            return default if default is not None else {}

    # Core parse attempt
    try:
        result = json_loads(data)
    except JSONDecodeError as e:
        return _on_parse_error(mode, e, "decode", default, expected_type, log_errors)
    except Exception as e:
        return _on_parse_error(mode, e, "read", default, expected_type, log_errors)

    # Type check
    if not isinstance(result, expected_type):
        if mode == JsonParseMode.STRICT:
            return JsonParseResult(
                success=False,
                error=f"Expected {expected_type.__name__}, got {type(result).__name__}",
                error_type="type",
            )
        if log_errors:
            log.warning(
                "JSON type mismatch: expected %s, got %s",
                expected_type.__name__,
                type(result).__name__,
            )
        return default if default is not None else expected_type()

    if mode == JsonParseMode.STRICT:
        return JsonParseResult(success=True, data=result)
    return result


def _on_parse_error(
    mode: JsonParseMode,
    error: Exception,
    error_type: str,
    default: Any,
    expected_type: type,
    log_errors: bool,
) -> Any:
    """Handle a parse error according to the active mode."""
    if mode == JsonParseMode.STRICT:
        return JsonParseResult(success=False, error=str(error), error_type=error_type)

    if log_errors:
        if error_type == "read":
            log.error("Unexpected error parsing JSON: %s", str(error)[:100])
        else:
            log.warning("JSON parse error: %s", str(error)[:100])
    return default if default is not None else expected_type()


__all__ = [
    "json_dumps",
    "json_loads",
    "JSONDecodeError",
    "_HAS_ORJSON",
    "JsonParseMode",
    "safe_json_parse",
    "JsonParseResult",
    "msgpack_dumps",
    "msgpack_loads",
    "_HAS_MSGPACK",
]
