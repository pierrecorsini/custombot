"""
test_incoming_message_id_validation.py — Tests for IncomingMessage.message_id
and sender_id validation.

Verifies that __post_init__ accepts real-world message/sender IDs while
rejecting values that could corrupt dedup indexes, logs, or crash-recovery paths.
"""

from __future__ import annotations

import pytest

from src.channels.base import IncomingMessage
from src.channels.message_validator import MessageValidator


def _make_msg(
    message_id: str = "msg_001",
    sender_id: str = "sender_001",
    **overrides: object,
) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    defaults: dict = {
        "message_id": message_id,
        "chat_id": "chat_001",
        "sender_id": sender_id,
        "sender_name": "Test User",
        "text": "hello",
        "timestamp": 1700000000.0,
        "channel_type": "whatsapp",
    }
    defaults.update(overrides)
    return IncomingMessage(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Valid message_id values
# ─────────────────────────────────────────────────────────────────────────────


class TestValidMessageIds:
    """Real-world message ID formats should be accepted without error."""

    @pytest.mark.parametrize(
        "message_id",
        [
            "3EB0123456789ABCDEF",  # WhatsApp-style hex ID
            "12345678-1234-1234-1234-123456789012",  # CLI UUID
            "msg_001",  # simple test ID
            "msg-42",  # dash-separated
            "a",  # single char
            "1234567890@s.whatsapp.net",  # WhatsApp JID-style
            "A" * 200,  # exactly at max length
        ],
    )
    def test_real_world_message_ids_accepted(self, message_id: str) -> None:
        msg = _make_msg(message_id=message_id)
        assert msg.message_id == message_id


# ─────────────────────────────────────────────────────────────────────────────
# Invalid message_id — type and emptiness
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidMessageIdTypes:
    """Non-string and empty values should be rejected."""

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(message_id=None)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(message_id=123)  # type: ignore[arg-type]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _make_msg(message_id="")


# ─────────────────────────────────────────────────────────────────────────────
# Invalid message_id — dangerous characters
# ─────────────────────────────────────────────────────────────────────────────


class TestDangerousMessageIds:
    """Strings containing path separators, control chars, etc. should be rejected."""

    @pytest.mark.parametrize(
        "message_id",
        [
            "../../etc/passwd",  # path traversal
            "..\\..\\windows",  # Windows path traversal
            "msg/id",  # forward slash
            "msg\\id",  # backslash
            "msg id",  # space
            "msg\tid",  # tab
            "msg\nid",  # newline
            "msg\x00id",  # null byte
            "msg; DROP TABLE--",  # SQL injection attempt
            "msg|pipe",  # pipe
            "msg?query",  # question mark
            "msg*wild",  # asterisk
            'msg"quote',  # double quote
            "msg'apos",  # single quote
        ],
    )
    def test_dangerous_characters_rejected(self, message_id: str) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(message_id=message_id)


# ─────────────────────────────────────────────────────────────────────────────
# message_id length validation
# ─────────────────────────────────────────────────────────────────────────────


class TestMessageIdLength:
    """message_id exceeding maximum length should be rejected."""

    def test_exactly_at_limit_accepted(self) -> None:
        message_id = "a" * 200
        msg = _make_msg(message_id=message_id)
        assert msg.message_id == message_id

    def test_one_over_limit_rejected(self) -> None:
        message_id = "a" * 201
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(message_id=message_id)

    def test_way_over_limit_rejected(self) -> None:
        message_id = "a" * 10_000
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(message_id=message_id)


# ─────────────────────────────────────────────────────────────────────────────
# Valid sender_id values
# ─────────────────────────────────────────────────────────────────────────────


class TestValidSenderIds:
    """Real-world sender ID formats should be accepted without error."""

    @pytest.mark.parametrize(
        "sender_id",
        [
            "1234567890",  # WhatsApp phone number
            "1234567890@s.whatsapp.net",  # WhatsApp JID
            "cli-abc123",  # CLI-style ID
            "sender_001",  # simple test ID
            "user-42",  # dash-separated
            "a",  # single char
            "A" * 200,  # exactly at max length
        ],
    )
    def test_real_world_sender_ids_accepted(self, sender_id: str) -> None:
        msg = _make_msg(sender_id=sender_id)
        assert msg.sender_id == sender_id


# ─────────────────────────────────────────────────────────────────────────────
# Invalid sender_id — type and emptiness
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidSenderIdTypes:
    """Non-string and empty values should be rejected."""

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_id=None)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_id=123)  # type: ignore[arg-type]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _make_msg(sender_id="")


# ─────────────────────────────────────────────────────────────────────────────
# Invalid sender_id — dangerous characters
# ─────────────────────────────────────────────────────────────────────────────


class TestDangerousSenderIds:
    """Strings containing path separators, control chars, etc. should be rejected."""

    @pytest.mark.parametrize(
        "sender_id",
        [
            "../../etc/passwd",
            "..\\..\\windows",
            "user/id",
            "user\\id",
            "user id",
            "user\tid",
            "user\nid",
            "user\x00id",
            "user; DROP TABLE--",
            "user|pipe",
            "user?query",
            "user*wild",
            'user"quote',
            "user'apos",
        ],
    )
    def test_dangerous_characters_rejected(self, sender_id: str) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(sender_id=sender_id)


# ─────────────────────────────────────────────────────────────────────────────
# sender_id length validation
# ─────────────────────────────────────────────────────────────────────────────


class TestSenderIdLength:
    """sender_id exceeding maximum length should be rejected."""

    def test_exactly_at_limit_accepted(self) -> None:
        sender_id = "a" * 200
        msg = _make_msg(sender_id=sender_id)
        assert msg.sender_id == sender_id

    def test_one_over_limit_rejected(self) -> None:
        sender_id = "a" * 201
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(sender_id=sender_id)

    def test_way_over_limit_rejected(self) -> None:
        sender_id = "a" * 10_000
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(sender_id=sender_id)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone validation functions
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateMessageIdFunction:
    """The _validate_message_id helper can be called directly."""

    def test_valid_passes(self) -> None:
        MessageValidator._validate_message_id("3EB0123456789ABCDEF")

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            MessageValidator._validate_message_id("../../etc")

    def test_custom_max_length(self) -> None:
        MessageValidator._validate_message_id("a" * 50)
        with pytest.raises(ValueError, match="exceeds maximum length"):
            MessageValidator._validate_message_id("a" * 50, max_length=10)


class TestValidateSenderIdFunction:
    """The _validate_sender_id helper can be called directly."""

    def test_valid_passes(self) -> None:
        MessageValidator._validate_sender_id("1234567890")

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            MessageValidator._validate_sender_id("../../etc")

    def test_custom_max_length(self) -> None:
        MessageValidator._validate_sender_id("a" * 50)
        with pytest.raises(ValueError, match="exceeds maximum length"):
            MessageValidator._validate_sender_id("a" * 50, max_length=10)


# ─────────────────────────────────────────────────────────────────────────────
# Regex pattern coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestMessageIdRegex:
    """Verify the _MESSAGE_ID_RE pattern matches expected character classes."""

    @pytest.mark.parametrize(
        "char",
        list("abcdefghijklmnopqrstuvwxyz")
        + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + list("0123456789")
        + list("-_@."),
    )
    def test_allowed_characters(self, char: str) -> None:
        assert MessageValidator._MESSAGE_ID_RE.match(char), f"Character {char!r} should be allowed"

    @pytest.mark.parametrize(
        "char",
        list(" \t\n\r/\\<>:\"|?*!#$%^&()[]{}+=;',"),
    )
    def test_disallowed_characters(self, char: str) -> None:
        assert not MessageValidator._MESSAGE_ID_RE.match(char), f"Character {char!r} should be rejected"


class TestSenderIdRegex:
    """Verify the _SENDER_ID_RE pattern matches expected character classes."""

    @pytest.mark.parametrize(
        "char",
        list("abcdefghijklmnopqrstuvwxyz")
        + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + list("0123456789")
        + list("-_@."),
    )
    def test_allowed_characters(self, char: str) -> None:
        assert MessageValidator._SENDER_ID_RE.match(char), f"Character {char!r} should be allowed"

    @pytest.mark.parametrize(
        "char",
        list(" \t\n\r/\\<>:\"|?*!#$%^&()[]{}+=;',"),
    )
    def test_disallowed_characters(self, char: str) -> None:
        assert not MessageValidator._SENDER_ID_RE.match(char), f"Character {char!r} should be rejected"
