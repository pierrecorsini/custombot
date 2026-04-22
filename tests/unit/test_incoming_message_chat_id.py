"""
test_incoming_message_chat_id.py — Tests for IncomingMessage.chat_id validation.

Verifies that __post_init__ accepts real-world chat IDs (WhatsApp JIDs, CLI UUIDs)
while rejecting values that could exploit filesystem paths, logs, or metric labels.
"""

from __future__ import annotations

import pytest

from src.channels.base import IncomingMessage, _CHAT_ID_RE, _validate_chat_id


def _make_msg(chat_id: str = "chat_001", **overrides: object) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    defaults: dict = {
        "message_id": "msg_001",
        "chat_id": chat_id,
        "sender_id": "sender_001",
        "sender_name": "Test User",
        "text": "hello",
        "timestamp": 1700000000.0,
        "channel_type": "whatsapp",
    }
    defaults.update(overrides)
    return IncomingMessage(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Valid chat IDs
# ─────────────────────────────────────────────────────────────────────────────


class TestValidChatIds:
    """Real-world chat ID formats should be accepted without error."""

    @pytest.mark.parametrize(
        "chat_id",
        [
            "1234567890@s.whatsapp.net",  # WhatsApp private chat
            "120363abcdefghij@g.us",  # WhatsApp group
            "12345678-1234-1234-1234-123456789012",  # CLI UUID
            "chat_123",  # simple test ID
            "user-42",  # dash-separated
            "a",  # single char
            "A",  # single uppercase
            "123",  # numeric
        ],
    )
    def test_real_world_chat_ids_accepted(self, chat_id: str) -> None:
        msg = _make_msg(chat_id=chat_id)
        assert msg.chat_id == chat_id


# ─────────────────────────────────────────────────────────────────────────────
# Invalid chat IDs — empty / non-string
# ─────────────────────────────────────────────────────────────────────────────


class TestInvalidChatIdTypes:
    """Non-string and empty values should be rejected."""

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(chat_id=None)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(chat_id=123)  # type: ignore[arg-type]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _make_msg(chat_id="")


# ─────────────────────────────────────────────────────────────────────────────
# Invalid chat IDs — dangerous characters
# ─────────────────────────────────────────────────────────────────────────────


class TestDangerousChatIds:
    """Strings containing path separators, control chars, etc. should be rejected."""

    @pytest.mark.parametrize(
        "chat_id",
        [
            "../../etc/passwd",  # path traversal
            "..\\..\\windows",  # Windows path traversal
            "../",  # traversal suffix
            "\\..",  # Windows traversal
            "chat/id",  # forward slash
            "chat\\id",  # backslash
            "chat id",  # space
            "chat\tid",  # tab
            "chat\nid",  # newline
            "chat\x00id",  # null byte
            "chat; DROP TABLE--",  # SQL injection attempt
            "chat<alert>",  # HTML injection
            "chat|pipe",  # pipe
            "chat?query",  # question mark
            "chat*wild",  # asterisk
            "chat&amp",  # ampersand
            "chat#hash",  # hash
            "chat!bang",  # exclamation
            "chat$dollar",  # dollar
            "chat%percent",  # percent
            "chat(colon)",  # parentheses
            "chat[colon]",  # brackets
            "chat{curly}",  # curly braces
            "chat+plus",  # plus
            "chat=equals",  # equals
            'chat"quote',  # double quote
            "chat'apos",  # single quote
        ],
    )
    def test_dangerous_characters_rejected(self, chat_id: str) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(chat_id=chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Length validation
# ─────────────────────────────────────────────────────────────────────────────


class TestChatIdLength:
    """chat_id exceeding maximum length should be rejected."""

    def test_exactly_at_limit_accepted(self) -> None:
        chat_id = "a" * 200
        msg = _make_msg(chat_id=chat_id)
        assert msg.chat_id == chat_id

    def test_one_over_limit_rejected(self) -> None:
        chat_id = "a" * 201
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(chat_id=chat_id)

    def test_way_over_limit_rejected(self) -> None:
        chat_id = "a" * 10_000
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(chat_id=chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone _validate_chat_id function
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateChatIdFunction:
    """The _validate_chat_id helper can be called directly."""

    def test_valid_passes(self) -> None:
        _validate_chat_id("1234567890@s.whatsapp.net")

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_chat_id("../../etc")

    def test_custom_max_length(self) -> None:
        # 50 chars is fine with default max_length=200
        _validate_chat_id("a" * 50)
        # but rejected with max_length=10
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _validate_chat_id("a" * 50, max_length=10)


# ─────────────────────────────────────────────────────────────────────────────
# Regex pattern coverage
# ─────────────────────────────────────────────────────────────────────────────


class TestChatIdRegex:
    """Verify the _CHAT_ID_RE pattern matches expected character classes."""

    @pytest.mark.parametrize(
        "char",
        list("abcdefghijklmnopqrstuvwxyz")
        + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        + list("0123456789")
        + list("-_@."),
    )
    def test_allowed_characters(self, char: str) -> None:
        assert _CHAT_ID_RE.match(char), f"Character {char!r} should be allowed"

    @pytest.mark.parametrize(
        "char",
        list(" \t\n\r/\\<>:\"|?*!#$%^&()[]{}+=;',"),
    )
    def test_disallowed_characters(self, char: str) -> None:
        assert not _CHAT_ID_RE.match(char), f"Character {char!r} should be rejected"
