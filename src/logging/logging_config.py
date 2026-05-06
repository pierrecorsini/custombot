"""
logging_config.py — Structured logging with JSON format support.

Provides:
  - Text format (default): Human-readable logs for development
  - JSON format: Machine-parseable logs for production/aggregation
  - Correlation IDs: Request tracing across async operations
  - Backward compatibility: Drop-in replacement for basic logging

Usage:
  from src.logging_config import setup_logging
  setup_logging(level="INFO", log_format="json")  # or "text"
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class VerbosityLevel(str, Enum):
    """Logging verbosity levels."""

    QUIET = "quiet"  # Errors only
    NORMAL = "normal"  # Balanced output (default)
    VERBOSE = "verbose"  # Full debug output

    @classmethod
    def default(cls) -> "VerbosityLevel":
        return cls.NORMAL


# Global verbosity level (set during setup)
_verbosity: VerbosityLevel = VerbosityLevel.NORMAL


def get_verbosity() -> VerbosityLevel:
    """Get the current verbosity level."""
    return _verbosity


def set_verbosity(level: VerbosityLevel | str) -> None:
    """Set the global verbosity level."""
    global _verbosity
    if isinstance(level, str):
        _verbosity = VerbosityLevel(level.lower())
    else:
        _verbosity = level


# Context variable for correlation ID (thread-safe for async)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Get the current correlation ID for request tracing."""
    return _correlation_id.get()


def new_correlation_id() -> str:
    """Generate a new short correlation ID (8 chars)."""
    return str(uuid.uuid4())[:8]


# Maximum length for a correlation ID to prevent unbounded log output.
_MAX_CORR_ID_LENGTH = 64

# Control characters, newlines, carriage returns, and ANSI escape sequences
# stripped from correlation IDs to prevent log injection.
_CORR_ID_SANITIZE_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|[\x00-\x1f\x7f-\x9f]")


def _sanitize_correlation_id(corr_id: str) -> str:
    """Strip control characters, newlines, and ANSI escapes; truncate to safe length."""
    cleaned = _CORR_ID_SANITIZE_PATTERN.sub("", corr_id)
    cleaned = cleaned.strip()
    if not cleaned:
        return new_correlation_id()
    if len(cleaned) > _MAX_CORR_ID_LENGTH:
        cleaned = cleaned[:_MAX_CORR_ID_LENGTH]
    return cleaned


def set_correlation_id(corr_id: str | None = None) -> str:
    """
    Set a correlation ID for the current context.

    If no ID is provided, generates a new UUID.
    The ID is sanitized to strip control characters, newlines, and ANSI
    escape sequences to prevent log injection. It is truncated to a
    maximum of 64 characters.
    Returns the correlation ID that was set.
    """
    if corr_id is None:
        corr_id = new_correlation_id()
    else:
        corr_id = _sanitize_correlation_id(corr_id)
    _correlation_id.set(corr_id)
    return corr_id


def clear_correlation_id() -> None:
    """Clear the correlation ID from the current context."""
    _correlation_id.set(None)


# ─────────────────────────────────────────────────────────────────────────────
# Sensitive Data Redaction
# ─────────────────────────────────────────────────────────────────────────────

# Default patterns for sensitive data redaction
# Each pattern is a tuple of (name, regex_pattern, replacement_template)
DEFAULT_REDACTION_PATTERNS: list[tuple[str, str, str]] = [
    # API Keys (various formats)
    (
        "api_key_param",
        r"(api[_-]?key|apikey)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-]{8,})['\"]?",
        r"\1=[REDACTED]",
    ),
    (
        "api_key_value",
        r"(?:api[_-]?key|apikey)['\"]?\s*[:=]\s*['\"]([a-zA-Z0-9_\-]{8,})['\"]",
        r"[REDACTED]",
    ),
    # Bearer/Auth tokens
    ("bearer_token", r"(bearer|token)\s+([a-zA-Z0-9_\-\.]{10,})", r"\1 [REDACTED]"),
    (
        "auth_header",
        r"(authorization|auth)\s*[=:]\s*['\"]?(bearer\s+)?[a-zA-Z0-9_\-\.]{10,}['\"]?",
        r"\1=[REDACTED]",
    ),
    # Passwords
    (
        "password",
        r"(password|passwd|pwd)\s*[=:]\s*['\"]?([^\s'\"]{4,})['\"]?",
        r"\1=[REDACTED]",
    ),
    # Secret keys
    (
        "secret_key",
        r"(secret[_-]?key|secret)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-]{8,})['\"]?",
        r"\1=[REDACTED]",
    ),
    # Access tokens
    (
        "access_token",
        r"(access[_-]?token|access_token)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-\.]{10,})['\"]?",
        r"\1=[REDACTED]",
    ),
    # Refresh tokens
    (
        "refresh_token",
        r"(refresh[_-]?token|refresh_token)\s*[=:]\s*['\"]?([a-zA-Z0-9_\-\.]{10,})['\"]?",
        r"\1=[REDACTED]",
    ),
    # OpenAI-style API keys (sk-...)
    ("openai_key", r"sk-[a-zA-Z0-9]{20,}", "[REDACTED]"),
    # AWS-style keys
    ("aws_key", r"(AKIA|ASIA)[A-Z0-9]{16}", "[REDACTED]"),
    # JWT tokens (partial match for long base64 strings with dots)
    (
        "jwt_token",
        r"eyJ[a-zA-Z0-9_\-]*\.eyJ[a-zA-Z0-9_\-]*\.[a-zA-Z0-9_\-]*",
        "[REDACTED]",
    ),
    # HMAC-SHA256 Authorization header (HMAC-SHA256 <timestamp>:<hex-signature>)
    (
        "hmac_auth_header",
        r"(HMAC-SHA256\s+)[0-9]+(?:\.[0-9]+)?:[a-fA-F0-9]+",
        r"\1[REDACTED]",
    ),
]

# Phone number patterns (partial redaction - keep last 4 digits)
DEFAULT_PHONE_PATTERNS: list[tuple[str, str, str]] = [
    # E.164 format (+12345678901)
    ("phone_e164", r"\+(\d{7,11})(\d{4})", r"+***\2"),
    # US format with dashes (123-456-7890)
    ("phone_us_dash", r"(\d{3})-(\d{3})-(\d{4})", r"***-***-\3"),
    # US format with dots (123.456.7890)
    ("phone_us_dot", r"(\d{3})\.(\d{3})\.(\d{4})", r"***.***.\3"),
    # US format with spaces (123 456 7890)
    ("phone_us_space", r"(\d{3})\s(\d{3})\s(\d{4})", r"*** *** \3"),
    # International format with spaces (+1 234 567 8901)
    ("phone_intl", r"\+(\d{1,3})\s(\d{3})\s(\d{3})\s(\d{4})", r"+\1 *** *** \4"),
    # WhatsApp-style JID (12345678901@s.whatsapp.net)
    ("phone_jid", r"(\d{7,11})(\d{4})@s\.whatsapp\.net", r"***\2@s.whatsapp.net"),
]


class SensitiveDataRedactor:
    """
    Configurable sensitive data redactor using regex patterns.

    Provides pattern-based redaction for sensitive values like API keys,
    tokens, passwords, and phone numbers. Patterns are configurable.

    Usage:
        redactor = SensitiveDataRedactor()
        safe_text = redactor.redact("api_key=sk-abc123...")
        # Returns: "api_key=[REDACTED]"
    """

    def __init__(
        self,
        patterns: list[tuple[str, str, str]] | None = None,
        phone_patterns: list[tuple[str, str, str]] | None = None,
        enabled: bool = True,
    ) -> None:
        """
        Initialize the redactor with patterns.

        Args:
            patterns: Custom redaction patterns (name, regex, replacement).
                      If None, uses DEFAULT_REDACTION_PATTERNS.
            phone_patterns: Custom phone number patterns for partial redaction.
                           If None, uses DEFAULT_PHONE_PATTERNS.
            enabled: Whether redaction is enabled (default True).
        """
        self.enabled = enabled
        self._patterns = patterns if patterns is not None else DEFAULT_REDACTION_PATTERNS
        self._phone_patterns = (
            phone_patterns if phone_patterns is not None else DEFAULT_PHONE_PATTERNS
        )
        self._compiled_patterns: list[tuple[str, re.Pattern[str], str]] = []
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        """Compile all regex patterns for efficient matching."""
        self._compiled_patterns = []
        all_patterns = self._patterns + self._phone_patterns
        for name, pattern, replacement in all_patterns:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
                self._compiled_patterns.append((name, compiled, replacement))
            except re.error:
                # Skip invalid patterns silently
                pass

    def add_pattern(self, name: str, pattern: str, replacement: str) -> None:
        """
        Add a custom redaction pattern.

        Args:
            name: Pattern identifier for reference.
            pattern: Regex pattern to match sensitive data.
            replacement: Replacement string (can use regex groups).
        """
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
            self._compiled_patterns.append((name, compiled, replacement))
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern '{name}': {exc}") from exc

    def remove_pattern(self, name: str) -> bool:
        """
        Remove a redaction pattern by name.

        Args:
            name: Pattern identifier to remove.

        Returns:
            True if pattern was found and removed, False otherwise.
        """
        original_len = len(self._compiled_patterns)
        self._compiled_patterns = [(n, p, r) for n, p, r in self._compiled_patterns if n != name]
        return len(self._compiled_patterns) < original_len

    def redact(self, text: str) -> str:
        """
        Redact sensitive data from text.

        Args:
            text: Input text potentially containing sensitive data.

        Returns:
            Text with sensitive values replaced by [REDACTED] or partial masks.
        """
        if not self.enabled or not text:
            return text

        result = text
        for _name, pattern, replacement in self._compiled_patterns:
            result = pattern.sub(replacement, result)
        return result

    def get_pattern_names(self) -> list[str]:
        """Get list of all active pattern names."""
        return [name for name, _, _ in self._compiled_patterns]


class RedactionFilter(logging.Filter):
    """
    Logging filter that redacts sensitive data from log records.

    Applies SensitiveDataRedactor to the log message and any extra fields.
    Can be added to any handler to ensure sensitive data never appears in logs.

    Usage:
        handler = logging.StreamHandler()
        handler.addFilter(RedactionFilter())
    """

    def __init__(
        self,
        patterns: list[tuple[str, str, str]] | None = None,
        phone_patterns: list[tuple[str, str, str]] | None = None,
        redact_args: bool = True,
    ) -> None:
        """
        Initialize the redaction filter.

        Args:
            patterns: Custom redaction patterns (passed to SensitiveDataRedactor).
            phone_patterns: Custom phone patterns (passed to SensitiveDataRedactor).
            redact_args: Whether to redact log record args (default True).
        """
        super().__init__()
        self.redactor = SensitiveDataRedactor(
            patterns=patterns,
            phone_patterns=phone_patterns,
        )
        self.redact_args = redact_args

    def filter(self, record: logging.LogRecord) -> bool:
        """Apply redaction to the log record."""
        # Redact the main message
        if record.msg and isinstance(record.msg, str):
            record.msg = self.redactor.redact(record.msg)

        # Redact formatted args
        if self.redact_args and record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact_value(v) for v in record.args)

        return True

    def _redact_value(self, value: Any) -> Any:
        """Redact a value if it's a string, return unchanged otherwise."""
        if isinstance(value, str):
            return self.redactor.redact(value)
        return value


# Global redactor instance for use by formatters
_global_redactor: SensitiveDataRedactor | None = None


def get_redactor() -> SensitiveDataRedactor:
    """Get the global redactor instance, creating if needed."""
    global _global_redactor
    if _global_redactor is None:
        _global_redactor = SensitiveDataRedactor()
    return _global_redactor


def set_redactor(redactor: SensitiveDataRedactor) -> None:
    """Set the global redactor instance."""
    global _global_redactor
    _global_redactor = redactor


def redact_sensitive(text: str) -> str:
    """
    Convenience function to redact sensitive data from any text.

    Uses the global redactor instance.

    Args:
        text: Text to redact.

    Returns:
        Text with sensitive data replaced.
    """
    return get_redactor().redact(text)


def get_sensitive_patterns() -> list[str]:
    """
    Get list of all active redaction pattern names.

    Returns:
        List of pattern names currently configured.
    """
    return get_redactor().get_pattern_names()


def add_sensitive_pattern(name: str, pattern: str, replacement: str) -> None:
    """
    Add a custom redaction pattern to the global redactor.

    Args:
        name: Pattern identifier for reference.
        pattern: Regex pattern to match sensitive data.
        replacement: Replacement string (can use regex groups).
    """
    get_redactor().add_pattern(name, pattern, replacement)


def remove_sensitive_pattern(name: str) -> bool:
    """
    Remove a redaction pattern from the global redactor.

    Args:
        name: Pattern identifier to remove.

    Returns:
        True if pattern was found and removed, False otherwise.
    """
    return get_redactor().remove_pattern(name)


class JsonFormatter(logging.Formatter):
    """
    JSON log formatter for structured logging.

    Output format:
    {
      "timestamp": "2025-01-21T10:30:00.123Z",
      "level": "INFO",
      "module": "bot",
      "message": "Processing message",
      "correlation_id": "abc123",
      "extra_field": "value"
    }
    """

    def __init__(
        self,
        include_correlation_id: bool = True,
        redact_sensitive: bool = True,
    ) -> None:
        super().__init__()
        self.include_correlation_id = include_correlation_id
        self.redact_sensitive = redact_sensitive

    def format(self, record: logging.LogRecord) -> str:
        # Base fields
        message = record.getMessage()
        if self.redact_sensitive:
            message = redact_sensitive(message)

        log_obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": message,
        }

        # Add correlation ID if available
        if self.include_correlation_id:
            corr_id = get_correlation_id()
            if corr_id:
                log_obj["correlation_id"] = corr_id

        # Add extra fields from the record
        # Standard attributes to exclude from extra fields
        standard_attrs = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "message",
            "asctime",
            "taskName",
        }

        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                try:
                    # Ensure value is JSON serializable
                    json.dumps(value)
                    # Redact string values
                    if self.redact_sensitive and isinstance(value, str):
                        value = redact_sensitive(value)
                    log_obj[key] = value
                except (TypeError, ValueError):
                    str_value = str(value)
                    if self.redact_sensitive:
                        str_value = redact_sensitive(str_value)
                    log_obj[key] = str_value

        # Add exception info if present
        if record.exc_info:
            exception_text = self.formatException(record.exc_info)
            if self.redact_sensitive:
                exception_text = redact_sensitive(exception_text)
            log_obj["exception"] = exception_text

        return json.dumps(log_obj, default=str)


class TextFormatter(logging.Formatter):
    """
    Human-readable text formatter with optional correlation ID.

    Output format:
      10:30:00 [INFO] module: message [corr_id: abc123]
    """

    def __init__(
        self,
        include_correlation_id: bool = True,
        redact_sensitive: bool = True,
        verbosity: VerbosityLevel | None = None,
    ) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.include_correlation_id = include_correlation_id
        self.redact_sensitive = redact_sensitive
        self.verbosity = verbosity

    def format(self, record: logging.LogRecord) -> str:
        # Get base formatted message
        formatted = super().format(record)

        # Apply redaction if enabled
        if self.redact_sensitive:
            formatted = redact_sensitive(formatted)

        # Append correlation ID only in verbose mode
        verbosity = self.verbosity or get_verbosity()
        if self.include_correlation_id and verbosity == VerbosityLevel.VERBOSE:
            corr_id = get_correlation_id()
            if corr_id:
                formatted = f"{formatted} [corr: {corr_id}]"

        return formatted


class SensitiveFormatter(TextFormatter):
    """
    Text formatter that always redacts sensitive data.

    This is an alias for TextFormatter with redaction enabled.
    Provided for backward compatibility.
    """

    def __init__(
        self,
        include_correlation_id: bool = True,
        verbosity: VerbosityLevel | None = None,
    ) -> None:
        super().__init__(
            include_correlation_id=include_correlation_id,
            redact_sensitive=True,
            verbosity=verbosity,
        )


class CorrelationIdFilter(logging.Filter):
    """Logging filter that injects correlation_id into LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def setup_logging(
    level: int | str = logging.INFO,
    log_format: str = "text",
    include_correlation_id: bool = True,
    suppress_noisy: bool = True,
    redact_sensitive: bool = True,
    custom_redaction_patterns: list[tuple[str, str, str]] | None = None,
    log_file: str | None = None,
    log_max_bytes: int = 10 * 1024 * 1024,
    log_backup_count: int = 5,
    verbosity: VerbosityLevel | str = VerbosityLevel.NORMAL,
) -> None:
    """
    Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR) or logging constant.
        log_format: "text" for human-readable, "json" for structured logs.
        include_correlation_id: Whether to include correlation IDs in logs.
        suppress_noisy: Whether to suppress verbose third-party library logs.
        redact_sensitive: Whether to redact sensitive data (API keys, tokens, etc.).
        custom_redaction_patterns: Additional redaction patterns (name, regex, replacement).
        log_file: Path to log file for file output (None = no file logging).
        log_max_bytes: Maximum log file size in bytes before rotation (default: 10MB).
        log_backup_count: Number of backup log files to keep (default: 5).
        verbosity: Logging verbosity level ("quiet", "normal", "verbose").

    Examples:
        # Development (default)
        setup_logging(level=logging.DEBUG, log_format="text")

        # Production with file logging
        setup_logging(level=logging.INFO, log_format="json", log_file="logs/app.log")

        # With custom redaction patterns
        setup_logging(
            custom_redaction_patterns=[
                ("custom_secret", r"my_secret=([^\s]+)", r"my_secret=[REDACTED]"),
            ]
        )
    """
    # Convert string level to int if needed
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    # Set global verbosity
    global _verbosity
    if isinstance(verbosity, str):
        _verbosity = VerbosityLevel(verbosity.lower())
    else:
        _verbosity = verbosity

    # Adjust log level based on verbosity
    if _verbosity == VerbosityLevel.QUIET:
        level = logging.WARNING
    elif _verbosity == VerbosityLevel.VERBOSE:
        level = logging.DEBUG

    # Configure global redactor with custom patterns if provided
    if custom_redaction_patterns:
        redactor = SensitiveDataRedactor()
        for name, pattern, replacement in custom_redaction_patterns:
            redactor.add_pattern(name, pattern, replacement)
        set_redactor(redactor)

    # Select formatter based on format type
    if log_format.lower() == "json":
        formatter: logging.Formatter = JsonFormatter(
            include_correlation_id=include_correlation_id,
            redact_sensitive=redact_sensitive,
        )
    else:
        formatter = TextFormatter(
            include_correlation_id=include_correlation_id,
            redact_sensitive=redact_sensitive,
            verbosity=_verbosity,
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicate logs
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler with selected formatter
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # Add correlation ID filter
    if include_correlation_id:
        console_handler.addFilter(CorrelationIdFilter())

    # Add redaction filter for extra safety (catches args and edge cases)
    if redact_sensitive:
        console_handler.addFilter(RedactionFilter())

    root_logger.addHandler(console_handler)

    # Add file handler with rotation if log_file is specified
    if log_file:
        try:
            # Ensure parent directory exists
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # Create rotating file handler
            file_handler = RotatingFileHandler(
                filename=log_file,
                maxBytes=log_max_bytes,
                backupCount=log_backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)

            # Add filters to file handler
            if include_correlation_id:
                file_handler.addFilter(CorrelationIdFilter())
            if redact_sensitive:
                file_handler.addFilter(RedactionFilter())

            root_logger.addHandler(file_handler)
            root_logger.debug(
                "Log file rotation configured: %s (max_bytes=%d, backup_count=%d)",
                log_file,
                log_max_bytes,
                log_backup_count,
            )
        except (OSError, IOError) as exc:
            # Log warning but don't fail - console logging will still work
            root_logger.warning(
                "Failed to configure log file %s: %s. Falling back to console only.",
                log_file,
                exc,
            )

    # Suppress noisy third-party libraries
    if suppress_noisy:
        for noisy_lib in ("httpx", "httpcore", "urllib3", "asyncio", "primp"):
            logging.getLogger(noisy_lib).setLevel(logging.WARNING)


# Convenience function for backward compatibility
def basicConfig(**kwargs: Any) -> None:
    """
    Drop-in replacement for logging.basicConfig().

    Supports same arguments plus 'log_format' for JSON/Text selection,
    'redact_sensitive' for sensitive data redaction, log rotation options,
    and 'verbosity' for logging verbosity level.
    """
    log_format = kwargs.pop("log_format", "text")
    level = kwargs.pop("level", logging.INFO)
    suppress_noisy = kwargs.pop("suppress_noisy", True)
    redact_sensitive = kwargs.pop("redact_sensitive", True)
    custom_redaction_patterns = kwargs.pop("custom_redaction_patterns", None)
    log_file = kwargs.pop("log_file", None)
    log_max_bytes = kwargs.pop("log_max_bytes", 10 * 1024 * 1024)
    log_backup_count = kwargs.pop("log_backup_count", 5)
    verbosity = kwargs.pop("verbosity", VerbosityLevel.NORMAL)

    setup_logging(
        level=level,
        log_format=log_format,
        suppress_noisy=suppress_noisy,
        redact_sensitive=redact_sensitive,
        custom_redaction_patterns=custom_redaction_patterns,
        log_file=log_file,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        verbosity=verbosity,
    )
