"""
test_incoming_message_channel_type.py — Tests for IncomingMessage.channel_type validation.

Verifies that __post_init__ accepts known channel types and safe alphanumeric
strings, while rejecting values containing special characters or non-string types.
"""

from __future__ import annotations

import pytest

from src.channels.base import VALID_CHANNEL_TYPES, IncomingMessage


def _make_msg(channel_type: str = "", **overrides: object) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    defaults: dict = {
        "message_id": "msg_001",
        "chat_id": "chat_001",
        "sender_id": "sender_001",
        "sender_name": "Test User",
        "text": "hello",
        "timestamp": 1700000000.0,
        "channel_type": channel_type,
    }
    defaults.update(overrides)
    return IncomingMessage(**defaults)


class TestValidChannelTypes:
    """Known whitelist values should be accepted without error."""

    @pytest.mark.parametrize("channel_type", sorted(VALID_CHANNEL_TYPES - {""}))
    def test_known_channel_types_accepted(self, channel_type: str) -> None:
        msg = _make_msg(channel_type=channel_type)
        assert msg.channel_type == channel_type

    def test_empty_string_default_accepted(self) -> None:
        msg = _make_msg()
        assert msg.channel_type == ""


class TestAlphanumericChannelTypes:
    """Unknown but safe alphanumeric identifiers should also be accepted."""

    @pytest.mark.parametrize(
        "channel_type",
        [
            "telegram",
            "discord",
            "slack",
            "web",
            "channel_01",
            "a",
            "test_channel_123",
        ],
    )
    def test_alphanumeric_accepted(self, channel_type: str) -> None:
        msg = _make_msg(channel_type=channel_type)
        assert msg.channel_type == channel_type


class TestInvalidChannelTypes:
    """Strings with special characters should be rejected."""

    @pytest.mark.parametrize(
        "channel_type",
        [
            "whatsapp/../../etc/passwd",  # path traversal
            "channel; DROP TABLE--",  # SQL injection attempt
            "channel with spaces",
            "channel\nnewline",
            "UPPERCASE",
            "CamelCase",
            "channel!",
            "channel@home",
            "channel#1",
            "channel$money",
            "channel%20encoded",
            "channel&more",
            "channel*",
            "channel+",
            "channel=",
            "channel?",
            "channel[0]",
            "../channel",
            "channel\x00null",  # null byte
        ],
    )
    def test_special_characters_rejected(self, channel_type: str) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(channel_type=channel_type)

    def test_non_string_type_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(channel_type=123)  # type: ignore[arg-type]

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(channel_type=None)  # type: ignore[arg-type]


class TestConstantExported:
    """VALID_CHANNEL_TYPES should be importable from the package."""

    def test_importable_from_package(self) -> None:
        from src.channels.base import VALID_CHANNEL_TYPES as vt

        assert isinstance(vt, frozenset)
        assert "whatsapp" in vt
        assert "cli" in vt
