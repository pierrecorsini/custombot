"""
test_incoming_message_hypothesis.py — Property-based tests for IncomingMessage
validation boundary conditions using Hypothesis.

Generates adversarial ``chat_id``, ``sender_name``, ``correlation_id``, and
``timestamp`` values to verify the validation layer rejects injection attempts
without raising unexpected exceptions.
"""

from __future__ import annotations

import math
import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.channels.base import IncomingMessage
from src.channels.message_validator import MessageValidator
from src.constants import (
    MAX_CORRELATION_ID_LENGTH,
    MAX_SENDER_NAME_LENGTH,
    TIMESTAMP_MAX,
    TIMESTAMP_MIN,
)
from src.utils.validation import _CHAT_ID_RE


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Characters accepted by the ID regex: ASCII alphanumeric, dash, underscore, dot, @.
_ID_CHARS: str = string.ascii_letters + string.digits + "-_.@"
_ID_ALPHABET = st.sampled_from(_ID_CHARS)

# Characters always rejected by the ID regex — anything NOT in _ID_CHARS.
# We use blacklist_characters to exclude the allowed set.
_ID_REJECTED = st.characters(
    blacklist_characters=_ID_CHARS,
    min_codepoint=1,
    max_codepoint=0x10FFFF,
)

# Dangerous printable characters for sender_name: / \ < > " | ? *
_DANGEROUS_PRINTABLE = "/\\<>\"|?*"

# Alphabet for safe sender_name: no control chars, no dangerous printables.
_SAFE_NAME_ALPHABET = st.characters(
    blacklist_categories=("Cc",),  # All control characters
    blacklist_characters=_DANGEROUS_PRINTABLE,
    min_codepoint=1,
    max_codepoint=0x10FFFF,
)

# Single control character (C0 range 0x00–0x1F or DEL 0x7F).
_CONTROL_CHAR = st.characters(min_codepoint=0, max_codepoint=0x1F) | st.just("\x7f")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ===========================================================================
# chat_id — property-based boundary conditions
# ===========================================================================


class TestChatIdProperties:
    """Property-based tests for chat_id validation."""

    @given(chat_id=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=200))
    @settings(max_examples=300)
    def test_valid_alphabet_always_accepted(self, chat_id: str) -> None:
        """Any string composed solely of allowed ID characters passes validation."""
        msg = _make_msg(chat_id=chat_id)
        assert msg.chat_id == chat_id

    @given(chat_id=st.text(alphabet=_ID_REJECTED, min_size=1, max_size=50))
    @settings(max_examples=300)
    def test_rejected_alphabet_always_raises_value_error(self, chat_id: str) -> None:
        """Any string containing only rejected characters raises ValueError."""
        with pytest.raises(ValueError):
            _make_msg(chat_id=chat_id)

    @given(chat_id=st.one_of(st.integers(), st.floats(), st.booleans(), st.none(), st.lists(st.text())))
    @settings(max_examples=50)
    def test_non_string_always_raises_type_error(self, chat_id: object) -> None:
        """Any non-string type raises TypeError."""
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(chat_id=chat_id)  # type: ignore[arg-type]

    @given(chat_id=st.text(min_size=201, max_size=500))
    @settings(max_examples=100)
    def test_oversized_strings_always_rejected(self, chat_id: str) -> None:
        """Strings exceeding MAX_CHAT_ID_LENGTH are always rejected."""
        assume(len(chat_id) > 200)
        with pytest.raises((ValueError, TypeError)):
            _make_msg(chat_id=chat_id)

    @given(chat_id=st.text(min_size=1, max_size=200))
    @settings(max_examples=300)
    def test_arbitrary_text_only_raises_expected_exceptions(self, chat_id: str) -> None:
        """Any string input only raises TypeError or ValueError — never unexpected exceptions."""
        try:
            _make_msg(chat_id=chat_id)
        except (TypeError, ValueError):
            pass

    @given(
        prefix=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=50),
        inj=st.text(min_size=1, max_size=10),
    )
    @settings(max_examples=200)
    def test_injection_in_valid_prefix_rejected(self, prefix: str, inj: str) -> None:
        """Injection characters embedded in an otherwise-valid prefix are rejected."""
        assume(not _CHAT_ID_RE.match(inj))
        chat_id = prefix + inj
        assume(len(chat_id) <= 200)
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(chat_id=chat_id)


# ===========================================================================
# sender_name — property-based boundary conditions
# ===========================================================================


class TestSenderNameProperties:
    """Property-based tests for sender_name validation."""

    @given(name=st.text(alphabet=_SAFE_NAME_ALPHABET, min_size=0, max_size=200))
    @settings(max_examples=300)
    def test_safe_unicode_always_accepted(self, name: str) -> None:
        """Unicode names without control chars or dangerous printables are accepted."""
        msg = _make_msg(sender_name=name)
        assert msg.sender_name == name[:MAX_SENDER_NAME_LENGTH]

    @given(name=st.text(min_size=0, max_size=200))
    @settings(max_examples=300)
    def test_only_raises_expected_exceptions(self, name: str) -> None:
        """Any sender_name only raises TypeError or ValueError — never unexpected exceptions."""
        try:
            _make_msg(sender_name=name)
        except (TypeError, ValueError):
            pass

    @given(name=st.one_of(st.integers(), st.floats(), st.none(), st.lists(st.text()), st.tuples(st.text())))
    @settings(max_examples=50)
    def test_non_string_always_raises_type_error(self, name: object) -> None:
        """Non-string types always raise TypeError."""
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(sender_name=name)  # type: ignore[arg-type]

    @given(char=_CONTROL_CHAR)
    @settings(max_examples=50)
    def test_control_chars_always_stripped(self, char: str) -> None:
        """Control characters are stripped, never cause unhandled exceptions."""
        name = f"before{char}after"
        msg = _make_msg(sender_name=name)
        assert char not in msg.sender_name
        assert msg.sender_name == "beforeafter"

    @given(name=st.text(alphabet=st.sampled_from(_DANGEROUS_PRINTABLE), min_size=1, max_size=50))
    @settings(max_examples=200)
    def test_dangerous_printable_only_strings_rejected(self, name: str) -> None:
        """Strings composed solely of dangerous printable characters are rejected."""
        with pytest.raises(ValueError, match="unsafe characters"):
            _make_msg(sender_name=name)

    @given(name=st.text(min_size=201, max_size=2000))
    @settings(max_examples=100)
    def test_oversized_name_only_raises_expected(self, name: str) -> None:
        """Names exceeding max length are truncated or rejected — never cause unexpected exceptions."""
        try:
            msg = _make_msg(sender_name=name)
            # If accepted, must be within max length
            assert len(msg.sender_name) <= MAX_SENDER_NAME_LENGTH
        except (TypeError, ValueError):
            pass

    @given(name=st.text(min_size=0, max_size=100))
    @settings(max_examples=200)
    def test_sanitization_is_idempotent(self, name: str) -> None:
        """Applying sender_name validation twice produces the same result."""
        try:
            msg = _make_msg(sender_name=name)
        except (TypeError, ValueError):
            return
        # Second round: the already-sanitized name should pass unchanged
        result = MessageValidator._validate_sender_name(msg.sender_name)
        assert result == msg.sender_name


# ===========================================================================
# correlation_id — property-based boundary conditions
# ===========================================================================


class TestCorrelationIdProperties:
    """Property-based tests for correlation_id validation."""

    @given(correlation_id=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=200))
    @settings(max_examples=300)
    def test_valid_alphabet_always_accepted(self, correlation_id: str) -> None:
        """Any string composed of allowed ID characters passes validation."""
        msg = _make_msg(correlation_id=correlation_id)
        assert msg.correlation_id == correlation_id

    @given(correlation_id=st.text(alphabet=_ID_REJECTED, min_size=1, max_size=50))
    @settings(max_examples=300)
    def test_rejected_alphabet_always_raises_value_error(self, correlation_id: str) -> None:
        """Any string containing only rejected characters raises ValueError."""
        with pytest.raises(ValueError):
            _make_msg(correlation_id=correlation_id)

    @given(correlation_id=st.one_of(st.integers(), st.floats(), st.booleans(), st.none(), st.lists(st.text())))
    @settings(max_examples=50)
    def test_non_string_non_none_always_raises_type_error(self, correlation_id: object) -> None:
        """Non-string, non-None types always raise TypeError."""
        if correlation_id is None:
            return
        with pytest.raises(TypeError, match="must be a str"):
            _make_msg(correlation_id=correlation_id)  # type: ignore[arg-type]

    def test_none_is_always_accepted(self) -> None:
        """correlation_id=None is always accepted (Optional field)."""
        msg = _make_msg(correlation_id=None)
        assert msg.correlation_id is None

    def test_empty_string_always_rejected(self) -> None:
        """Empty string is always rejected (must not be empty if provided)."""
        with pytest.raises(ValueError, match="must not be empty"):
            _make_msg(correlation_id="")

    @given(correlation_id=st.text(min_size=201, max_size=500))
    @settings(max_examples=100)
    def test_oversized_strings_always_rejected(self, correlation_id: str) -> None:
        """Strings exceeding MAX_CORRELATION_ID_LENGTH are always rejected."""
        assume(len(correlation_id) > MAX_CORRELATION_ID_LENGTH)
        with pytest.raises((ValueError, TypeError)):
            _make_msg(correlation_id=correlation_id)

    @given(correlation_id=st.text(min_size=1, max_size=200))
    @settings(max_examples=300)
    def test_arbitrary_text_only_raises_expected_exceptions(self, correlation_id: str) -> None:
        """Any string input only raises TypeError or ValueError."""
        try:
            _make_msg(correlation_id=correlation_id)
        except (TypeError, ValueError):
            pass

    @given(
        prefix=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=50),
        inj=st.text(min_size=1, max_size=10),
    )
    @settings(max_examples=200)
    def test_injection_in_valid_prefix_rejected(self, prefix: str, inj: str) -> None:
        """Injection characters in an otherwise-valid correlation_id are rejected."""
        assume(not _CHAT_ID_RE.match(inj))
        correlation_id = prefix + inj
        assume(len(correlation_id) <= MAX_CORRELATION_ID_LENGTH)
        with pytest.raises(ValueError, match="invalid characters"):
            _make_msg(correlation_id=correlation_id)


# ===========================================================================
# timestamp — property-based boundary conditions
# ===========================================================================


class TestTimestampProperties:
    """Property-based tests for timestamp validation."""

    @given(
        timestamp=st.floats(
            min_value=TIMESTAMP_MIN,
            max_value=TIMESTAMP_MAX,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    @settings(max_examples=300)
    def test_in_range_always_accepted(self, timestamp: float) -> None:
        """Any float in [TIMESTAMP_MIN, TIMESTAMP_MAX] passes validation."""
        msg = _make_msg(timestamp=timestamp)
        assert msg.timestamp == timestamp

    @given(timestamp=st.integers(min_value=int(TIMESTAMP_MIN), max_value=int(TIMESTAMP_MAX)))
    @settings(max_examples=200)
    def test_integer_timestamps_accepted(self, timestamp: int) -> None:
        """Integer timestamps in valid range pass validation."""
        msg = _make_msg(timestamp=timestamp)
        assert msg.timestamp == timestamp

    @given(timestamp=st.one_of(st.text(), st.none(), st.lists(st.integers()), st.binary()))
    @settings(max_examples=50)
    def test_non_numeric_always_raises_type_error(self, timestamp: object) -> None:
        """Non-numeric types always raise TypeError."""
        with pytest.raises(TypeError, match="must be a number"):
            _make_msg(timestamp=timestamp)  # type: ignore[arg-type]

    @given(
        timestamp=st.floats(
            min_value=TIMESTAMP_MAX + 1,
            max_value=1e18,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    @settings(max_examples=100)
    def test_far_future_always_rejected(self, timestamp: float) -> None:
        """Timestamps above TIMESTAMP_MAX are always rejected."""
        assume(timestamp > TIMESTAMP_MAX)
        with pytest.raises(ValueError, match="too large"):
            _make_msg(timestamp=timestamp)

    @given(timestamp=st.floats(max_value=TIMESTAMP_MIN - 1, allow_nan=False, allow_infinity=False))
    @settings(max_examples=100)
    def test_negative_always_rejected(self, timestamp: float) -> None:
        """Negative timestamps are always rejected."""
        assume(timestamp < TIMESTAMP_MIN)
        with pytest.raises(ValueError, match="too small"):
            _make_msg(timestamp=timestamp)

    def test_nan_always_rejected(self) -> None:
        """NaN is always rejected."""
        with pytest.raises(ValueError, match="must not be NaN"):
            _make_msg(timestamp=float("nan"))

    def test_positive_inf_always_rejected(self) -> None:
        """Positive infinity is always rejected."""
        with pytest.raises(ValueError, match="must not be Inf"):
            _make_msg(timestamp=float("inf"))

    def test_negative_inf_always_rejected(self) -> None:
        """Negative infinity is always rejected."""
        with pytest.raises(ValueError, match="must not be Inf"):
            _make_msg(timestamp=float("-inf"))

    @given(timestamp=st.floats(allow_nan=True, allow_infinity=True))
    @settings(max_examples=200)
    def test_any_float_only_raises_expected_exceptions(self, timestamp: float) -> None:
        """Any float input only raises TypeError or ValueError — never unexpected exceptions."""
        try:
            _make_msg(timestamp=timestamp)
        except (TypeError, ValueError):
            pass


# ===========================================================================
# Cross-field — adversarial combined validation
# ===========================================================================


class TestCrossFieldProperties:
    """Property-based tests that exercise multiple fields simultaneously."""

    @given(
        chat_id=st.text(min_size=1, max_size=200),
        sender_name=st.text(min_size=0, max_size=200),
        correlation_id=st.text(min_size=1, max_size=200),
        timestamp=st.floats(allow_nan=True, allow_infinity=True),
    )
    @settings(max_examples=300)
    def test_all_adversarial_fields_only_raise_expected_exceptions(
        self,
        chat_id: str,
        sender_name: str,
        correlation_id: str,
        timestamp: float,
    ) -> None:
        """Adversarial values across all four fields never cause unexpected exceptions.

        This is the core property: the validation layer is a security boundary.
        No combination of adversarial inputs should escape TypeError/ValueError.
        """
        try:
            _make_msg(
                chat_id=chat_id,
                sender_name=sender_name,
                correlation_id=correlation_id,
                timestamp=timestamp,
            )
        except (TypeError, ValueError):
            pass

    @given(
        chat_id=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=200),
        sender_name=st.text(alphabet=_SAFE_NAME_ALPHABET, min_size=1, max_size=200),
        correlation_id=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=200),
        timestamp=st.floats(
            min_value=TIMESTAMP_MIN,
            max_value=TIMESTAMP_MAX,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @settings(max_examples=200)
    def test_all_valid_fields_always_accepted(
        self,
        chat_id: str,
        sender_name: str,
        correlation_id: str,
        timestamp: float,
    ) -> None:
        """When all four fields use valid strategies, the message is always created successfully."""
        msg = _make_msg(
            chat_id=chat_id,
            sender_name=sender_name,
            correlation_id=correlation_id,
            timestamp=timestamp,
        )
        assert msg.chat_id == chat_id
        assert msg.sender_name == sender_name[:MAX_SENDER_NAME_LENGTH]
        assert msg.correlation_id == correlation_id
        assert msg.timestamp == timestamp
