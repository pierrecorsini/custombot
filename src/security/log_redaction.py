"""
src/security/log_redaction.py — PII redaction filter for log output.

Automatically redacts phone numbers, API keys, tokens, emails, credit
cards, and other PII from all log records using configurable regex
patterns.

Usage:
    from src.security.log_redaction import PIIRedactingFilter

    handler.addFilter(PIIRedactingFilter())

The filter is also registered globally during logging setup via
:func:`install_pii_filter`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Configurable PII Redaction Patterns
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (pattern, name).  All matches are replaced with [REDACTED].
PII_PATTERNS: list[tuple[str, str]] = [
    # Phone numbers — E.164 (+1234567890) and common formats
    (r"\+\d{7,15}", "phone_e164"),
    (r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", "phone_formatted"),
    # API keys — OpenAI, Anthropic, generic sk-*
    (r"\bsk-[a-zA-Z0-9]{20,}\b", "openai_api_key"),
    (r"\bsk-proj-[a-zA-Z0-9_-]{20,}\b", "openai_project_key"),
    (r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b", "anthropic_api_key"),
    # Bearer / Basic auth tokens
    (r"(?i)\bBearer\s+[a-zA-Z0-9._-]{20,}", "bearer_token"),
    (r"(?i)\bBasic\s+[a-zA-Z0-9._-]{20,}", "basic_auth"),
    # Email addresses
    (r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", "email"),
    # Credit card numbers (groups of 4 digits separated by spaces/dashes)
    (r"\b(?:\d{4}[-\s]?){3}\d{4}\b", "credit_card"),
    # Generic secret patterns (key=, token=, password=)
    (r"(?i)(?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*['\"]?[^\s'\"]{8,}", "secret_value"),
    # GitHub, GitLab, Slack tokens
    (r"\bghp_[a-zA-Z0-9]{36}\b", "github_token"),
    (r"\bglpat-[a-zA-Z0-9_-]{20,}\b", "gitlab_token"),
    (r"\bxox[bpsa]-[a-zA-Z0-9-]{10,}\b", "slack_token"),
    # AWS access keys
    (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "aws_access_key"),
    # JWT tokens (three base64 segments joined by dots)
    (r"\beyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*\b", "jwt_token"),
    # Private keys
    (r"-----BEGIN(?:\s+(?:RSA\s+|EC\s+|DSA\s+))?PRIVATE KEY-----", "private_key"),
]

# Replacement text for all matches
REDACTED: str = "[REDACTED]"


def _compile_patterns(
    patterns: list[tuple[str, str]],
) -> list[tuple[re.Pattern[str], str]]:
    """Compile regex patterns, skipping invalid ones."""
    compiled: list[tuple[re.Pattern[str], str]] = []
    for pattern, name in patterns:
        try:
            compiled.append((re.compile(pattern), name))
        except re.error:
            pass
    return compiled


class PIIRedactingFilter(logging.Filter):
    """Logging filter that redacts PII from log messages and args.

    Scans ``record.msg`` and ``record.args`` for known PII patterns
    and replaces matches with ``[REDACTED]``.  Patterns are configurable
    via the *patterns* parameter.
    """

    def __init__(
        self,
        patterns: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        source = patterns if patterns is not None else PII_PATTERNS
        self._patterns = _compile_patterns(source)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._redact_value(v) for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._redact_value(v) for v in record.args
                )

        return True

    def _redact(self, text: str) -> str:
        for pattern, _name in self._patterns:
            text = pattern.sub(REDACTED, text)
        return text

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        return value


def install_pii_filter(
    logger: logging.Logger | None = None,
    patterns: list[tuple[str, str]] | None = None,
) -> PIIRedactingFilter:
    """Install the PII redaction filter on *logger* (defaults to root).

    Idempotent: skips installation if a ``PIIRedactingFilter`` is already
    present on any handler of the target logger.

    Returns the installed filter instance.
    """
    target = logger or logging.getLogger()
    filt = PIIRedactingFilter(patterns=patterns)

    for handler in target.handlers:
        if any(isinstance(f, PIIRedactingFilter) for f in handler.filters):
            return filt

    for handler in target.handlers:
        handler.addFilter(filt)

    return filt


__all__ = [
    "PIIRedactingFilter",
    "PII_PATTERNS",
    "REDACTED",
    "install_pii_filter",
]
