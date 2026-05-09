"""
channels/message_validator.py — Boundary validation for IncomingMessage.

Consolidates all defense-in-depth field checks into a single cohesive class,
providing a unified ``validate()`` entry point that replaces the scattered
standalone ``_validate_*()`` functions previously in channels/base.py.

All validation functions and regex patterns live as static methods and class
attributes on ``MessageValidator``, giving the class a single cohesive
responsibility with one public entry point.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from src.constants import (
    MAX_CORRELATION_ID_LENGTH,
    MAX_MESSAGE_ID_LENGTH,
    MAX_RAW_PAYLOAD_SIZE,
    MAX_SENDER_ID_LENGTH,
    MAX_SENDER_NAME_LENGTH,
    TIMESTAMP_MAX,
    TIMESTAMP_MIN,
)
from src.utils.validation import _CHAT_ID_RE, _validate_chat_id

if TYPE_CHECKING:
    from src.channels.base import IncomingMessage

log = logging.getLogger(__name__)


# ── Validator class ─────────────────────────────────────────────────────


class MessageValidator:
    """Cohesive boundary validator for IncomingMessage fields.

    Provides a single ``validate()`` entry point that runs all defense-in-depth
    checks (type, length, character-safety, range) on every message field.
    All regex patterns and per-field validators are static methods / class
    attributes so the class is self-contained.
    """

    # ── Regex patterns (class attributes) ───────────────────────────────

    # Same pattern as _CHAT_ID_RE: alphanumeric, dash, underscore, dot, @.
    # Real-world values: WhatsApp "3EB0XXXXXX", CLI UUID "12345678-1234-...".
    _MESSAGE_ID_RE: re.Pattern[str] = _CHAT_ID_RE
    _SENDER_ID_RE: re.Pattern[str] = _CHAT_ID_RE

    # Characters allowed in sender_name.
    # Broader than the ID regex: allows spaces, Unicode letters, common punctuation.
    # Rejects path separators and other dangerous printable chars.
    _SENDER_NAME_RE: re.Pattern[str] = re.compile(r"^[^\x00-\x1f\x7f/\\<>\"|?*]+$")

    # Control characters stripped from sender_name before validation.
    _CONTROL_CHARS_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f\x7f]")

    # Alphanumeric + underscore for unknown-but-safe channel identifiers.
    _CHANNEL_TYPE_RE: re.Pattern[str] = re.compile(r"^[a-z0-9_]+$")

    # ── Per-field validators (static methods) ───────────────────────────

    @staticmethod
    def _validate_message_id(message_id: object, *, max_length: int = 200) -> None:
        """Validate ``message_id`` at the IncomingMessage boundary.

        Raises:
            TypeError: If ``message_id`` is not a string.
            ValueError: If empty, too long, or contains unsafe characters.
        """
        if not isinstance(message_id, str):
            raise TypeError(
                f"IncomingMessage.message_id must be a str, got {type(message_id).__name__}"
            )
        if not message_id:
            raise ValueError("IncomingMessage.message_id must not be empty")
        effective_max = max_length or MAX_MESSAGE_ID_LENGTH
        if len(message_id) > effective_max:
            raise ValueError(
                f"IncomingMessage.message_id exceeds maximum length "
                f"({len(message_id)} > {effective_max}): {message_id[:40]!r}..."
            )
        if not MessageValidator._MESSAGE_ID_RE.match(message_id):
            raise ValueError(
                f"IncomingMessage.message_id contains invalid characters: {message_id!r}. "
                "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
            )

    @staticmethod
    def _validate_sender_id(sender_id: object, *, max_length: int = 200) -> None:
        """Validate ``sender_id`` at the IncomingMessage boundary.

        Raises:
            TypeError: If ``sender_id`` is not a string.
            ValueError: If empty, too long, or contains unsafe characters.
        """
        if not isinstance(sender_id, str):
            raise TypeError(
                f"IncomingMessage.sender_id must be a str, got {type(sender_id).__name__}"
            )
        if not sender_id:
            raise ValueError("IncomingMessage.sender_id must not be empty")
        effective_max = max_length or MAX_SENDER_ID_LENGTH
        if len(sender_id) > effective_max:
            raise ValueError(
                f"IncomingMessage.sender_id exceeds maximum length "
                f"({len(sender_id)} > {effective_max}): {sender_id[:40]!r}..."
            )
        if not MessageValidator._SENDER_ID_RE.match(sender_id):
            raise ValueError(
                f"IncomingMessage.sender_id contains invalid characters: {sender_id!r}. "
                "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
            )

    @staticmethod
    def _validate_sender_name(sender_name: object, *, max_length: int = 200) -> str:
        """Sanitize and validate ``sender_name`` at the IncomingMessage boundary.

        Strips control characters, truncates to max length, and rejects dangerous
        printable characters. Empty string is allowed.

        Returns:
            The sanitized sender_name string.
        """
        if not isinstance(sender_name, str):
            raise TypeError(
                f"IncomingMessage.sender_name must be a str, got {type(sender_name).__name__}"
            )
        sanitized = MessageValidator._CONTROL_CHARS_RE.sub("", sender_name)
        effective_max = max_length or MAX_SENDER_NAME_LENGTH
        if len(sanitized) > effective_max:
            original_len = len(sanitized)
            sanitized = sanitized[:effective_max]
            log.warning(
                "Truncated sender_name: %d → %d chars (max: %d)",
                original_len,
                len(sanitized),
                effective_max,
            )
        if sanitized and not MessageValidator._SENDER_NAME_RE.match(sanitized):
            raise ValueError(
                f"IncomingMessage.sender_name contains unsafe characters: {sanitized!r}. "
                "Path separators and shell metacharacters are not allowed."
            )
        return sanitized

    @staticmethod
    def _validate_timestamp(timestamp: object) -> None:
        """Validate ``timestamp`` at the IncomingMessage boundary.

        Rejects non-numeric types, NaN, Infinity, and out-of-range values.
        """
        if not isinstance(timestamp, (int, float)):
            raise TypeError(
                f"IncomingMessage.timestamp must be a number, got {type(timestamp).__name__}"
            )
        if isinstance(timestamp, float) and (timestamp != timestamp):  # NaN check
            raise ValueError("IncomingMessage.timestamp must not be NaN")
        if isinstance(timestamp, float):
            import math

            if math.isinf(timestamp):
                raise ValueError("IncomingMessage.timestamp must not be Inf")
        if timestamp < TIMESTAMP_MIN:
            raise ValueError(f"IncomingMessage.timestamp is too small ({timestamp} < {TIMESTAMP_MIN})")
        if timestamp > TIMESTAMP_MAX:
            raise ValueError(f"IncomingMessage.timestamp is too large ({timestamp} > {TIMESTAMP_MAX})")

    @staticmethod
    def _validate_correlation_id(correlation_id: object, *, max_length: int = 200) -> None:
        """Validate ``correlation_id`` at the IncomingMessage boundary.

        Only called when ``correlation_id`` is not None.
        """
        if not isinstance(correlation_id, str):
            raise TypeError(
                f"IncomingMessage.correlation_id must be a str, got {type(correlation_id).__name__}"
            )
        if not correlation_id:
            raise ValueError("IncomingMessage.correlation_id must not be empty")
        effective_max = max_length or MAX_CORRELATION_ID_LENGTH
        if len(correlation_id) > effective_max:
            raise ValueError(
                f"IncomingMessage.correlation_id exceeds maximum length "
                f"({len(correlation_id)} > {effective_max}): {correlation_id[:40]!r}..."
            )
        if not _CHAT_ID_RE.match(correlation_id):
            raise ValueError(
                f"IncomingMessage.correlation_id contains invalid characters: {correlation_id!r}. "
                "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
            )

    @staticmethod
    def _validate_raw(
        raw: dict[str, Any] | None, *, max_bytes: int = 0
    ) -> dict[str, Any] | None:
        """Validate ``raw`` payload size at the IncomingMessage boundary.

        Serializes the raw dict to JSON bytes to measure its true wire-size.
        When the serialized form exceeds *max_bytes*, returns ``None`` and
        emits a warning — matching the truncation-not-rejection pattern used
        by ``_validate_sender_name``.

        Returns:
            The original *raw* dict if within budget, or ``None`` when stripped.
        """
        if raw is None:
            return None
        import json

        effective_max = max_bytes or MAX_RAW_PAYLOAD_SIZE
        try:
            serialized = json.dumps(raw, default=str, ensure_ascii=False)
            size_bytes = len(serialized.encode("utf-8"))
        except (TypeError, ValueError, OverflowError):
            log.warning(
                "Stripped IncomingMessage.raw: failed to serialize for size check"
            )
            return None
        if size_bytes > effective_max:
            log.warning(
                "Stripped IncomingMessage.raw: serialized size %d > max %d bytes",
                size_bytes,
                effective_max,
            )
            return None
        return raw

    # ── Public entry point ──────────────────────────────────────────────

    def validate(self, message: IncomingMessage) -> None:
        """Validate all fields of an IncomingMessage instance.

        Performs type, length, character-safety, and range checks on all
        message fields. Sanitizes ``sender_name`` in-place (frozen dataclass
        setattr workaround).

        Raises:
            TypeError: For incorrect field types.
            ValueError: For empty, out-of-range, or unsafe field values.
        """
        from src.channels.base import VALID_CHANNEL_TYPES

        _validate_chat_id(message.chat_id)
        self._validate_message_id(message.message_id)
        self._validate_sender_id(message.sender_id)

        sanitized_name = self._validate_sender_name(message.sender_name)
        if sanitized_name != message.sender_name:
            object.__setattr__(message, "sender_name", sanitized_name)

        self._validate_timestamp(message.timestamp)

        if message.correlation_id is not None:
            self._validate_correlation_id(message.correlation_id)

        validated_raw = self._validate_raw(message.raw)
        if validated_raw is not message.raw:
            object.__setattr__(message, "raw", validated_raw)

        if message.channel_type in VALID_CHANNEL_TYPES:
            return
        if not isinstance(message.channel_type, str):
            raise TypeError(
                f"IncomingMessage.channel_type must be a str, got {type(message.channel_type).__name__}"
            )
        if not self._CHANNEL_TYPE_RE.match(message.channel_type):
            raise ValueError(
                f"IncomingMessage.channel_type contains invalid characters: {message.channel_type!r}. "
                f"Must be one of {sorted(VALID_CHANNEL_TYPES - {''})!r} or a lowercase alphanumeric string."
            )


# Module-level singleton used by IncomingMessage.__post_init__.
_validator = MessageValidator()
