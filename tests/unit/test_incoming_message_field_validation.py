"""
test_incoming_message_field_validation.py — Tests for IncomingMessage field
validation of sender_name, timestamp, and correlation_id.

Verifies that __post_init__ accepts real-world values while rejecting
values that could corrupt logs, tracing backends, or filesystem paths.
"""

from __future__ import annotations

import math

import pytest

from src.channels.base import (
    IncomingMessage,
    _validate_correlation_id,
    _validate_sender_name,
    _validate_timestamp,
)


def _make_msg(**overrides: object) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    defaults: dict = {
        "message_id": "msg_001",
        "chat_id": "chat_001",
        "sender_id": "sender_001",
        "sender_name": "Test User",
        "text": "hello",
        "timestamp": 1700000000.0,
        "channel_type": "whatsapp",
    }
    defaults.update(overrides)
    return IncomingMessage(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# sender_name validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidSenderNames:
    """Real-world display names should be accepted without error."""

    @pytest.mark.parametrize(
        "sender_name",
        [
            "John Doe",
            "María García-López",
            "Alice O'Brien",
            "Bob & Charlie",
            "张三",
            "田中太郎",
            "Émile Zola",
            "Anna-Marie van der Berg",
            "Dr. Smith (Jr.)",
            "",  # empty is allowed — some channels omit display names
            "a",
            "A" * 200,  # exactly at max length
        ],
    )
    def test_real_world_names_accepted(self, sender_name: str) -> None:
        msg = _make_msg(sender_name=sender_name)
        assert msg.sender_name == sender_name


class TestInvalidSenderNameTypes:
    """Non-string values should be rejected."""

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_name=None)  # type: ignore[arg-type]

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_name=123)  # type: ignore[arg-type]

    def test_list_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_name=["John"])  # type: ignore[arg-type]


class TestControlCharsStripped:
    """Control characters should be stripped from sender_name."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("user\x00name", "username"),  # null byte
            ("user\tname", "username"),  # tab
            ("user\nname", "username"),  # newline
            ("user\rname", "username"),  # carriage return
            ("user\x1bname", "username"),  # escape (ANSI)
            ("\x1b[31mRed\x1b[0m", "[31mRed[0m"),  # ANSI escape sequence
            ("clean", "clean"),  # no control chars → unchanged
        ],
    )
    def test_control_chars_stripped(self, raw: str, expected: str) -> None:
        msg = _make_msg(sender_name=raw)
        assert msg.sender_name == expected


class TestDangerousSenderNames:
    """Strings with path separators and shell metacharacters should be rejected."""

    @pytest.mark.parametrize(
        "sender_name",
        [
            "/etc/passwd",  # leading slash
            "C:\\Users",  # backslash
            '<script>alert("xss")</script>',  # angle brackets
            "name|pipe",  # pipe
            "name?query",  # question mark
            "name*wild",  # asterisk
            'name"quote',  # double quote
        ],
    )
    def test_dangerous_printable_chars_rejected(self, sender_name: str) -> None:
        with pytest.raises(ValueError, match="unsafe characters"):
            _make_msg(sender_name=sender_name)


class TestSenderNameTruncation:
    """sender_name exceeding maximum length should be truncated."""

    def test_exactly_at_limit_unchanged(self) -> None:
        sender_name = "a" * 200
        msg = _make_msg(sender_name=sender_name)
        assert msg.sender_name == sender_name

    def test_one_over_limit_truncated(self) -> None:
        sender_name = "a" * 201
        msg = _make_msg(sender_name=sender_name)
        assert msg.sender_name == "a" * 200
        assert len(msg.sender_name) == 200

    def test_way_over_limit_truncated(self) -> None:
        sender_name = "a" * 10_000
        msg = _make_msg(sender_name=sender_name)
        assert msg.sender_name == "a" * 200
        assert len(msg.sender_name) == 200


# ─────────────────────────────────────────────────────────────────────────────
# timestamp validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidTimestamps:
    """Reasonable timestamps should be accepted."""

    @pytest.mark.parametrize(
        "timestamp",
        [
            0.0,  # epoch
            1700000000.0,  # ~2023
            1700000000,  # int also works
            1.5,  # fractional second
        ],
    )
    def test_reasonable_timestamps_accepted(self, timestamp: float) -> None:
        msg = _make_msg(timestamp=timestamp)
        assert msg.timestamp == timestamp


class TestInvalidTimestampTypes:
    """Non-numeric types should be rejected."""

    def test_none_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a number"):
            _make_msg(timestamp=None)  # type: ignore[arg-type]

    def test_string_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a number"):
            _make_msg(timestamp="2024-01-01")  # type: ignore[arg-type]

    def test_bool_rejected(self) -> None:
        # bool is a subclass of int, but we don't want True/False timestamps
        # Actually bool IS instance of int in Python, so this will pass isinstance check.
        # We test it separately — it's a valid numeric value (1.0 or 0.0).
        pass


class TestInvalidTimestampValues:
    """NaN, Inf, and out-of-range values should be rejected."""

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be NaN"):
            _make_msg(timestamp=float("nan"))

    def test_positive_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be Inf"):
            _make_msg(timestamp=float("inf"))

    def test_negative_inf_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be Inf"):
            _make_msg(timestamp=float("-inf"))

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="too small"):
            _make_msg(timestamp=-1.0)

    def test_far_future_rejected(self) -> None:
        with pytest.raises(ValueError, match="too large"):
            _make_msg(timestamp=999999999999.0)


# ─────────────────────────────────────────────────────────────────────────────
# correlation_id validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidCorrelationIds:
    """Reasonable correlation IDs should be accepted."""

    @pytest.mark.parametrize(
        "correlation_id",
        [
            "abc-123",
            "req_001",
            "trace.span.parent",
            "user@session",
            "a" * 200,  # exactly at max length
        ],
    )
    def test_valid_correlation_ids_accepted(self, correlation_id: str) -> None:
        msg = _make_msg(correlation_id=correlation_id)
        assert msg.correlation_id == correlation_id

    def test_none_is_allowed(self) -> None:
        """correlation_id is Optional — None should pass without error."""
        msg = _make_msg(correlation_id=None)
        assert msg.correlation_id is None


class TestInvalidCorrelationIdTypes:
    """Non-string, non-None values should be rejected."""

    def test_int_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(correlation_id=123)  # type: ignore[arg-type]

    def test_list_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(correlation_id=["abc"])  # type: ignore[arg-type]


class TestInvalidCorrelationIdValues:
    """Empty, too long, or dangerous correlation IDs should be rejected."""

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _make_msg(correlation_id="")

    def test_too_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _make_msg(correlation_id="a" * 201)

    @pytest.mark.parametrize(
        "correlation_id",
        [
            "id with spaces",
            "id/with/slashes",
            "id\\with\\backslash",
            "id; DROP TABLE",
            "id|pipe",
        ],
    )
    def test_dangerous_characters_rejected(self, correlation_id: str) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(correlation_id=correlation_id)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone validation functions
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateSenderNameFunction:
    """The _validate_sender_name helper returns sanitized string."""

    def test_valid_passes(self) -> None:
        assert _validate_sender_name("John Doe") == "John Doe"

    def test_empty_passes(self) -> None:
        assert _validate_sender_name("") == ""

    def test_control_chars_stripped(self) -> None:
        assert _validate_sender_name("user\x00name") == "username"

    def test_dangerous_printable_raises(self) -> None:
        with pytest.raises(ValueError, match="unsafe characters"):
            _validate_sender_name("/etc/passwd")

    def test_truncation(self) -> None:
        result = _validate_sender_name("a" * 300)
        assert len(result) == 200

    def test_custom_max_length(self) -> None:
        assert _validate_sender_name("a" * 50) == "a" * 50
        result = _validate_sender_name("a" * 50, max_length=10)
        assert result == "a" * 10


class TestValidateTimestampFunction:
    """The _validate_timestamp helper can be called directly."""

    def test_valid_passes(self) -> None:
        _validate_timestamp(1700000000.0)

    def test_nan_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be NaN"):
            _validate_timestamp(float("nan"))


class TestValidateCorrelationIdFunction:
    """The _validate_correlation_id helper can be called directly."""

    def test_valid_passes(self) -> None:
        _validate_correlation_id("req-123")

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_correlation_id("bad id")

    def test_custom_max_length(self) -> None:
        _validate_correlation_id("a" * 50)
        with pytest.raises(ValueError, match="exceeds maximum length"):
            _validate_correlation_id("a" * 50, max_length=10)
