"""
src/security/template.py — Prompt template engine with injection prevention.

Safe prompt assembly that prevents template injection by using Python's
``string.Template`` (safe_substitute) instead of f-strings, auto-escaping
user input, and validating template variables before substitution.

Usage:
    from src.security.template import TemplateEngine

    engine = TemplateEngine()
    result = engine.render(
        "You are $bot_name. User says: $user_input",
        {"bot_name": "Assistant", "user_input": user_text},
    )
"""

from __future__ import annotations

import logging
import re
import string
from typing import Any

from src.security.prompt_injection import sanitize_user_input

log = logging.getLogger(__name__)

# Characters that could break out of template context or inject instructions
_DANGEROUS_CHARS = re.compile(r"[$\\{}]")

# Pattern to find all $variable references in a template
_TEMPLATE_VAR = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

# Pattern to find ${variable} references
_TEMPLATE_BRACED_VAR = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TemplateError(Exception):
    """Raised when template rendering fails validation."""


class TemplateEngine:
    """Safe prompt template engine with injection prevention.

    Uses ``string.Template.safe_substitute`` to avoid ``KeyError`` on
    missing variables.  Before substitution, user-supplied values are
    auto-escaped and sanitised via :func:`sanitize_user_input`.

    Steps:
      1. Extract variable names from the template.
      2. Validate that all required variables are provided.
      3. Auto-escape special characters in user-supplied values.
      4. Sanitise user input through the injection detector.
      5. Substitute using ``safe_substitute`` (no format-string tricks).
    """

    def __init__(
        self,
        *,
        user_keys: set[str] | None = None,
        strict: bool = False,
    ) -> None:
        """Initialise the template engine.

        Args:
            user_keys: Variable names considered user-supplied (auto-escaped
                and sanitised).  Defaults to common names: ``user_input``,
                ``user_message``, ``user_name``, ``query``, ``content``.
            strict: When ``True``, raise :class:`TemplateError` on missing
                variables instead of leaving ``$name`` in the output.
        """
        self._user_keys = user_keys or {
            "user_input",
            "user_message",
            "user_name",
            "query",
            "content",
            "message",
            "input",
        }
        self._strict = strict

    def render(self, template: str, variables: dict[str, Any]) -> str:
        """Render a prompt template with safe variable substitution.

        Args:
            template: Prompt template using ``$variable`` or ``${variable}``
                placeholders (``string.Template`` syntax).
            variables: Mapping of variable names to values.  String values
                in ``user_keys`` are auto-escaped and sanitised.

        Returns:
            Rendered prompt string.

        Raises:
            TemplateError: When *strict* is True and required variables are
                missing.
        """
        self._validate_variables(template, variables)
        safe_vars = self._prepare_variables(variables)

        tpl = string.Template(template)
        result = tpl.safe_substitute(safe_vars)

        if self._strict:
            leftover = _TEMPLATE_VAR.findall(result) + _TEMPLATE_BRACED_VAR.findall(result)
            if leftover:
                raise TemplateError(
                    f"Unresolved template variables after substitution: {sorted(set(leftover))}"
                )

        return result

    def _validate_variables(self, template: str, variables: dict[str, Any]) -> None:
        """Check that all referenced variables have corresponding values."""
        names = set(_TEMPLATE_VAR.findall(template)) | set(
            _TEMPLATE_BRACED_VAR.findall(template)
        )
        if not names:
            return

        missing = names - set(variables.keys())
        if missing and self._strict:
            raise TemplateError(
                f"Missing template variables: {sorted(missing)}"
            )

        if missing:
            log.debug(
                "Template has unresolved variables: %s",
                sorted(missing),
            )

    def _prepare_variables(self, variables: dict[str, Any]) -> dict[str, str]:
        """Convert all values to strings, escaping/sanitising user input."""
        prepared: dict[str, str] = {}
        for key, value in variables.items():
            str_value = str(value) if value is not None else ""
            if key in self._user_keys:
                str_value = _escape_template_chars(str_value)
                str_value = sanitize_user_input(str_value)
            prepared[key] = str_value
        return prepared


def _escape_template_chars(text: str) -> str:
    """Escape ``$``, ``{``, ``}``  and ``\\`` to prevent template injection."""
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("{", "\\{").replace("}", "\\}")


__all__ = [
    "TemplateEngine",
    "TemplateError",
]
