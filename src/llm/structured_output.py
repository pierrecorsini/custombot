"""
src/llm/structured_output.py — Structured output mode for JSON tool responses.

Validates LLM tool responses against expected JSON schemas and retries
(with error context) on validation failures.  Eliminates parsing errors
in skills that require structured data.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm import LLMProvider

log = logging.getLogger(__name__)

_MAX_RETRIES = 2
_FIXUP_PROMPT = (
    "The previous response failed schema validation:\n"
    "```\n{errors}\n```\n\n"
    "Expected schema:\n```json\n{schema}\n```\n\n"
    "Return ONLY valid JSON matching the schema."
)


class StructuredOutputManager:
    """Validates and retries LLM responses against JSON schemas."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._enabled = enabled
        self._stats: dict[str, int] = {
            "validated": 0,
            "retries": 0,
            "failures": 0,
        }

    async def validate_and_fix(
        self,
        raw_response: str,
        schema: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Parse *raw_response* as JSON and validate against *schema*.

        Returns the parsed dict on success, or ``None`` after exhausting
        retries.  When ``enabled`` is ``False``, returns the parsed dict
        without schema validation.
        """
        if not self._enabled:
            return self._parse_json(raw_response)

        parsed = self._parse_json(raw_response)
        if parsed is None:
            return None

        errors = self._validate(parsed, schema)
        if not errors:
            self._stats["validated"] += 1
            return parsed

        if self._llm is None:
            self._stats["failures"] += 1
            return parsed  # Return best-effort

        return await self._retry_fix(raw_response, schema, errors)

    async def _retry_fix(
        self,
        original: str,
        schema: dict[str, Any],
        initial_errors: list[str],
    ) -> dict[str, Any] | None:
        """Retry with an error-fixing prompt up to _MAX_RETRIES times."""
        last_response = original
        errors = initial_errors

        for attempt in range(_MAX_RETRIES):
            self._stats["retries"] += 1
            prompt = _FIXUP_PROMPT.format(
                errors="\n".join(errors),
                schema=json.dumps(schema, indent=2)[:2000],
            )
            try:
                completion = await self._llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    timeout=15.0,
                )
                last_response = completion.choices[0].message.content or ""
            except Exception:
                log.warning("Structured output retry %d failed", attempt + 1)
                continue

            parsed = self._parse_json(last_response)
            if parsed is None:
                continue

            errors = self._validate(parsed, schema)
            if not errors:
                self._stats["validated"] += 1
                return parsed

        self._stats["failures"] += 1
        log.warning("Structured output validation failed after %d retries", _MAX_RETRIES)
        return self._parse_json(original)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        """Extract and parse JSON from text, tolerating markdown fences."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            lines = lines[1:]  # Remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines)
        try:
            result = json.loads(stripped)
            return result if isinstance(result, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _validate(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
        """Validate *data* against a simple JSON schema. Returns error list."""
        errors: list[str] = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        for key in required:
            if key not in data:
                errors.append(f"Missing required field: {key!r}")

        for key, value in data.items():
            prop_schema = properties.get(key)
            if prop_schema is None:
                continue
            expected_type = prop_schema.get("type")
            if expected_type and not _type_matches(value, expected_type):
                errors.append(
                    f"Field {key!r}: expected {expected_type}, got {type(value).__name__}"
                )

        return errors

    def get_metrics(self) -> dict[str, int]:
        return dict(self._stats)


def _type_matches(value: Any, expected: str) -> bool:
    """Check if *value* matches the JSON-schema *expected* type."""
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    expected_type = type_map.get(expected)
    if expected_type is None:
        return True
    return isinstance(value, expected_type)
