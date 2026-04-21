"""
test_phone_normalization.py - Tests for phone number normalization in WhatsAppChannel.

Covers normalize_phone() and _is_allowed() with various international formats.
"""

from __future__ import annotations

import pytest

from src.channels.whatsapp import WhatsAppChannel
from src.utils.phone import normalize_phone
from src.config import WhatsAppConfig


# ── normalize_phone unit tests ──────────────────────────────────────────────


class TestNormalizePhone:
    """Pure-function tests for the normalize_phone helper."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # E.164 with plus
            ("+49123456789", "49123456789"),
            # 00 international prefix
            ("0049123456789", "49123456789"),
            # Leading 0 national trunk prefix
            ("0123456789", "123456789"),
            # Parentheses and dashes (US format)
            ("(123) 456-7890", "1234567890"),
            # Spaces and dashes
            ("+1 234 567 8901", "12345678901"),
            # Mixed formatting
            ("+49-123-456 789", "49123456789"),
            # Plain digits (already normalized)
            ("49123456789", "49123456789"),
            # Only zeros after 00 prefix → empty string
            ("00", ""),
        ],
    )
    def test_normalizes_common_formats(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    def test_empty_string_stays_empty(self) -> None:
        assert normalize_phone("") == ""

    def test_single_zero(self) -> None:
        assert normalize_phone("0") == ""

    def test_pure_nondigits_become_empty(self) -> None:
        assert normalize_phone("++--  ()") == ""


class TestNormalizePhoneEdgeCases:
    """Edge-case tests for normalize_phone covering JIDs, short numbers, and
    country-code disambiguation.

    See PLAN.md Phase 6 — Test Coverage.
    """

    def test_pure_alphabetic_becomes_empty(self) -> None:
        assert normalize_phone("abc") == ""

    def test_whatsapp_jid_becomes_empty(self) -> None:
        """Non-phone JIDs like 'somenonphone@s.whatsapp.net' have no digits."""
        assert normalize_phone("somenonphone@s.whatsapp.net") == ""

    def test_country_code_vs_national_dont_collide(self) -> None:
        """'+491234567890' and '0123456789' are different numbers."""
        assert normalize_phone("+491234567890") == "491234567890"
        assert normalize_phone("0123456789") == "123456789"
        assert normalize_phone("+491234567890") != normalize_phone("0123456789")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # group JID: digits extracted from the full JID string
            ("123456789-1234567890@g.us", "1234567891234567890"),
            # group JID with leading zero in local part
            ("0123456-9876543210@g.us", "1234569876543210"),
        ],
    )
    def test_group_jids(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("123", "123"),
            ("+12", "12"),
            ("012", "12"),
            ("001", "1"),
            ("1", "1"),
            ("0", ""),
        ],
    )
    def test_short_numbers(self, raw: str, expected: str) -> None:
        """Numbers with fewer than 7 digits are returned as-is (digits only)."""
        assert normalize_phone(raw) == expected


# ── _is_allowed integration with normalization ───────────────────────────────


def _make_channel(allowed_numbers: list[str]) -> WhatsAppChannel:
    """Create a WhatsAppChannel with the given allowed_numbers."""
    cfg = WhatsAppConfig(allowed_numbers=allowed_numbers)
    return WhatsAppChannel(cfg)


class TestIsAllowedNormalization:
    """Tests that _is_allowed correctly matches numbers across formats."""

    def test_e164_plus_matches_plain_digits(self) -> None:
        ch = _make_channel(["+49123456789"])
        assert ch._is_allowed("49123456789") is True

    def test_00_prefix_matches_plain_digits(self) -> None:
        ch = _make_channel(["0049123456789"])
        assert ch._is_allowed("49123456789") is True

    def test_national_zero_matches_e164_sender(self) -> None:
        """National format '0123456789' should match sender '123456789'."""
        ch = _make_channel(["0123456789"])
        assert ch._is_allowed("123456789") is True

    def test_formatted_us_number_matches(self) -> None:
        ch = _make_channel(["(123) 456-7890"])
        assert ch._is_allowed("1234567890") is True

    def test_unrecognized_number_rejected(self) -> None:
        ch = _make_channel(["+49123456789"])
        assert ch._is_allowed("99999999999") is False

    def test_empty_allowed_list_defers_to_allow_all(self) -> None:
        ch = _make_channel([])
        # allow_all defaults to True, so empty list still allows everyone
        assert ch._is_allowed("49123456789") is True

    def test_multiple_formats_for_same_number(self) -> None:
        ch = _make_channel(["+49123456789", "0049123456789"])
        # Both entries normalize to the same value; sender still matches
        assert ch._is_allowed("49123456789") is True


class TestIsAllowedCaching:
    """Tests that _is_allowed caches the normalized allowed_numbers set."""

    def test_cache_populated_on_first_call(self) -> None:
        ch = _make_channel(["+49123456789", "001234567890"])
        assert ch._normalized_allowed is None
        ch._is_allowed("49123456789")
        assert ch._normalized_allowed is not None
        assert "49123456789" in ch._normalized_allowed
        assert "1234567890" in ch._normalized_allowed

    def test_cache_reused_on_subsequent_calls(self) -> None:
        ch = _make_channel(["+49123456789"])
        ch._is_allowed("49123456789")
        cached = ch._normalized_allowed
        assert cached is not None
        # Second call should reuse the same cached frozenset object
        ch._is_allowed("99999999999")
        assert ch._normalized_allowed is cached

    def test_cache_not_set_when_allowed_numbers_empty(self) -> None:
        ch = _make_channel([])
        ch._is_allowed("49123456789")
        # Empty list takes the early-return path; cache stays None
        assert ch._normalized_allowed is None

    def test_duplicate_normalized_numbers_deduplicated_in_cache(self) -> None:
        ch = _make_channel(["+49123456789", "0049123456789"])
        ch._is_allowed("49123456789")
        # Both entries normalize to "49123456789"; frozenset deduplicates
        assert ch._normalized_allowed is not None
        assert ch._normalized_allowed == frozenset({"49123456789"})

    def test_invalidate_allowed_cache_resets(self) -> None:
        ch = _make_channel(["+49123456789"])
        ch._is_allowed("49123456789")
        assert ch._normalized_allowed is not None
        ch._invalidate_allowed_cache()
        assert ch._normalized_allowed is None
        # After invalidation, cache is rebuilt on next call
        ch._is_allowed("49123456789")
        assert ch._normalized_allowed is not None
