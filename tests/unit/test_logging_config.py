"""
Unit tests for correlation ID sanitization in logging_config.
"""

import pytest

from src.logging.logging_config import (
    _MAX_CORR_ID_LENGTH,
    _sanitize_correlation_id,
    clear_correlation_id,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)


class TestSanitizeCorrelationId:
    """Verify _sanitize_correlation_id strips control chars and truncates."""

    def test_normal_id_unchanged(self) -> None:
        assert _sanitize_correlation_id("abc123") == "abc123"

    def test_strips_newline(self) -> None:
        result = _sanitize_correlation_id("abc\nFAKE LOG LINE")
        assert "\n" not in result
        assert result == "abcFAKE LOG LINE"

    def test_strips_carriage_return(self) -> None:
        result = _sanitize_correlation_id("abc\rFAKE")
        assert "\r" not in result
        assert result == "abcFAKE"

    def test_strips_tab(self) -> None:
        result = _sanitize_correlation_id("abc\tdef")
        assert "\t" not in result
        assert result == "abcdef"

    def test_strips_null_byte(self) -> None:
        result = _sanitize_correlation_id("abc\x00def")
        assert "\x00" not in result
        assert result == "abcdef"

    def test_strips_c0_control_range(self) -> None:
        result = _sanitize_correlation_id("A\x01\x02\x1fB")
        assert result == "AB"

    def test_strips_c1_control_range(self) -> None:
        result = _sanitize_correlation_id("A\x80\x9fB")
        assert result == "AB"

    def test_strips_ansi_escape_sequences(self) -> None:
        result = _sanitize_correlation_id("abc\x1b[31mRED\x1b[0m")
        assert "\x1b" not in result
        assert result == "abcRED"

    def test_strips_ansi_clear_screen(self) -> None:
        result = _sanitize_correlation_id("abc\x1b[2Jdef")
        assert "\x1b" not in result
        assert result == "abcdef"

    def test_strips_leading_trailing_whitespace(self) -> None:
        result = _sanitize_correlation_id("  abc123  ")
        assert result == "abc123"

    def test_truncates_to_max_length(self) -> None:
        long_id = "a" * 200
        result = _sanitize_correlation_id(long_id)
        assert len(result) == _MAX_CORR_ID_LENGTH

    def test_empty_after_sanitization_generates_new_id(self) -> None:
        result = _sanitize_correlation_id("\x00\x01\x02")
        assert result  # not empty
        assert len(result) == 8  # new_correlation_id length

    def test_whitespace_only_generates_new_id(self) -> None:
        result = _sanitize_correlation_id("   \t\n  ")
        assert result  # not empty

    def test_preserves_underscores_and_hyphens(self) -> None:
        assert _sanitize_correlation_id("sched_1234_abcd") == "sched_1234_abcd"

    def test_complex_injection_attempt(self) -> None:
        """Simulate a log injection attempt with newlines and ANSI."""
        malicious = 'valid_id\nERROR [CRITICAL] Database deleted\x1b[0m'
        result = _sanitize_correlation_id(malicious)
        assert "\n" not in result
        assert "\x1b" not in result
        assert "valid_id" in result


class TestSetCorrelationIdSanitization:
    """Verify set_correlation_id applies sanitization."""

    def setup_method(self) -> None:
        clear_correlation_id()

    def teardown_method(self) -> None:
        clear_correlation_id()

    def test_none_generates_new_id(self) -> None:
        result = set_correlation_id(None)
        assert result
        assert get_correlation_id() == result

    def test_normal_id_set_directly(self) -> None:
        result = set_correlation_id("test_corr_123")
        assert result == "test_corr_123"
        assert get_correlation_id() == "test_corr_123"

    def test_newline_sanitized(self) -> None:
        result = set_correlation_id("abc\nINJECTED")
        assert "\n" not in result
        assert "\n" not in get_correlation_id()

    def test_truncation_applied(self) -> None:
        long_id = "x" * 200
        result = set_correlation_id(long_id)
        assert len(result) == _MAX_CORR_ID_LENGTH
        assert get_correlation_id() == result

    def test_returns_sanitized_value(self) -> None:
        result = set_correlation_id("valid\x00hidden")
        assert result == "validhidden"
        assert get_correlation_id() == "validhidden"

    def test_auto_generated_id_not_sanitized(self) -> None:
        """When None is passed, the auto-generated ID doesn't go through sanitization."""
        result = set_correlation_id(None)
        assert len(result) == 8  # UUID[:8]
