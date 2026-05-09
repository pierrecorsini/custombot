"""
llm.tool_validator — Validate tool call results against expected schemas.

After tool execution, validates the result against an ``output_schema``
defined on the skill.  On validation failure, returns an error message
to the LLM instead of the raw result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_RESULT_LENGTH = 100_000


@dataclass(slots=True, frozen=True)
class ValidationResult:
    """Outcome of validating a tool result."""

    valid: bool
    error_message: str | None = None


class ToolResultValidator:
    """Validates tool call results against optional output schemas.

    Skills can define ``output_schema`` on their class to specify
    expected output constraints.  This validator checks:
      - Result is a string
      - Length is within bounds
      - If JSON, contains expected fields
    """

    def __init__(self, *, max_result_length: int = MAX_RESULT_LENGTH) -> None:
        self._max_length = max_result_length

    def validate(
        self,
        result: str,
        *,
        skill_name: str,
        output_schema: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Validate a tool execution result.

        Args:
            result: The raw string result from tool execution.
            skill_name: Name of the skill that produced the result.
            output_schema: Optional schema dict from the skill class.

        Returns:
            ``ValidationResult`` indicating success or failure with message.
        """
        if not isinstance(result, str):
            return ValidationResult(
                valid=False,
                error_message=f"Tool {skill_name} returned non-string result",
            )

        if len(result) > self._max_length:
            return ValidationResult(
                valid=False,
                error_message=(
                    f"Tool {skill_name} result exceeds maximum length "
                    f"({len(result)} > {self._max_length})"
                ),
            )

        if output_schema is None:
            return ValidationResult(valid=True)

        # Validate against schema constraints
        min_length = output_schema.get("min_length")
        if min_length is not None and len(result) < min_length:
            return ValidationResult(
                valid=False,
                error_message=(
                    f"Tool {skill_name} result too short "
                    f"({len(result)} < {min_length})"
                ),
            )

        max_length = output_schema.get("max_length")
        if max_length is not None and len(result) > max_length:
            return ValidationResult(
                valid=False,
                error_message=(
                    f"Tool {skill_name} result too long "
                    f"({len(result)} > {max_length})"
                ),
            )

        # Validate JSON structure if required
        expected_fields = output_schema.get("required_fields")
        if expected_fields:
            json_result = _try_parse_json(result)
            if json_result is None:
                return ValidationResult(
                    valid=False,
                    error_message=(
                        f"Tool {skill_name} expected JSON output but "
                        f"result is not valid JSON"
                    ),
                )
            missing = [
                f for f in expected_fields
                if f not in json_result
            ]
            if missing:
                return ValidationResult(
                    valid=False,
                    error_message=(
                        f"Tool {skill_name} result missing required fields: "
                        f"{', '.join(missing)}"
                    ),
                )

        # Validate result type if specified
        result_type = output_schema.get("type")
        if result_type == "json":
            if _try_parse_json(result) is None:
                return ValidationResult(
                    valid=False,
                    error_message=(
                        f"Tool {skill_name} expected JSON output but "
                        f"result is not valid JSON"
                    ),
                )

        return ValidationResult(valid=True)

    def format_validation_error(
        self,
        skill_name: str,
        error_message: str,
    ) -> str:
        """Format a validation error as a tool result message for the LLM."""
        log.warning("Tool validation failed for %s: %s", skill_name, error_message)
        return (
            f"⚠️ Tool {skill_name} output validation failed: {error_message}. "
            f"Please try again with adjusted parameters."
        )


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Attempt to parse text as JSON, returning None on failure."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None
