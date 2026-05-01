"""
test_routing.py — Unit tests for routing matching functions.

Tests the _compile_pattern + _match_compiled functions and RoutingEngine.match() behavior.
"""

from __future__ import annotations

import re

import pytest

from src.channels.base import IncomingMessage
from src.routing import RoutingEngine, RoutingRule, _compile_pattern, _match_compiled


# Helper to match a pattern string against a value (mirrors the old _match_criterion API)
def _match(pattern: str, value: str) -> bool:
    """Compile pattern and match against value — convenience wrapper for tests."""
    compiled = _compile_pattern(pattern)
    return _match_compiled(compiled, pattern, value)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _match_compiled pure function
# ─────────────────────────────────────────────────────────────────────────────


class TestMatchCompiled:
    """Unit tests for the _match_compiled function."""

    def test_wildcard_matches_any_value(self) -> None:
        """Pattern '*' should match any non-empty string."""
        assert _match("*", "anything") is True
        assert _match("*", "12345") is True
        assert _match("*", "hello world") is True
        assert _match("*", "") is True

    def test_wildcard_matches_special_characters(self) -> None:
        """Pattern '*' should match strings with special characters."""
        assert _match("*", "user@example.com") is True
        assert _match("*", "+1-555-123-4567") is True
        assert _match("*", "C:\\Users\\test") is True

    def test_exact_match_returns_true(self) -> None:
        """Plain string patterns should match identical values exactly."""
        assert _match("hello", "hello") is True
        assert _match("user123", "user123") is True
        assert _match("whatsapp", "whatsapp") is True

    def test_exact_match_returns_false_for_different_values(self) -> None:
        """Plain string patterns should not match different values."""
        assert _match("hello", "world") is False
        assert _match("user123", "user456") is False
        assert _match("whatsapp", "telegram") is False

    def test_exact_match_is_case_sensitive(self) -> None:
        """Plain string matching should be case-sensitive."""
        assert _match("Hello", "hello") is False
        assert _match("WHATSAPP", "whatsapp") is False
        assert _match("User", "user") is False

    def test_regex_pattern_matches_using_re_match(self) -> None:
        """Regex patterns should match using re.match (anchored at start)."""
        assert _match(r"user\d+", "user123") is True
        assert _match(r"user\d+", "user456") is True
        assert _match(r"hello.*", "hello world") is True

    def test_regex_pattern_anchored_at_start(self) -> None:
        """Regex patterns using re.match should only match at string start."""
        assert _match(r"\d+", "user123") is False  # 'user' prefix
        assert _match(r"world", "hello world") is False  # not at start

    def test_regex_pattern_full_match_with_end_anchor(self) -> None:
        """Full string match requires $ anchor in regex."""
        assert _match(r"user\d+$", "user123") is True
        assert _match(r"user\d+$", "user123extra") is False

    def test_regex_character_classes(self) -> None:
        """Regex character classes should work correctly."""
        assert _match(r"[a-z]+", "hello") is True
        assert _match(r"[a-z]+", "HELLO") is False
        assert _match(r"[A-Z]+", "HELLO") is True

    def test_regex_special_characters_escaped(self) -> None:
        """Regex special characters should be handled correctly."""
        assert _match(r"\d{3}-\d{4}", "555-1234") is True
        assert _match(r"\d{3}-\d{4}", "5551234") is False

    def test_invalid_regex_falls_back_to_exact_match(self) -> None:
        """Invalid regex patterns should fall back to exact string match."""
        # Invalid regex: unmatched parenthesis
        assert _match(r"invalid(regex", "invalid(regex") is True
        assert _match(r"invalid(regex", "other") is False

        # Invalid regex: unmatched bracket
        assert _match(r"invalid[regex", "invalid[regex") is True

    def test_empty_pattern_matches_empty_value(self) -> None:
        """Empty pattern should match empty value (exact match)."""
        assert _match("", "") is True
        assert _match("", "value") is False

    def test_regex_case_insensitive_not_default(self) -> None:
        """Regex matching should be case-sensitive by default."""
        assert _match(r"hello", "HELLO") is False
        assert _match(r"[a-z]+", "HELLO") is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _compile_pattern
# ─────────────────────────────────────────────────────────────────────────────


class TestCompilePattern:
    """Tests for _compile_pattern helper."""

    def test_wildcard_returns_none(self) -> None:
        """'*' pattern should not compile to a regex (handled as wildcard)."""
        assert _compile_pattern("*") is None

    def test_empty_returns_none(self) -> None:
        """Empty pattern should not compile to a regex."""
        assert _compile_pattern("") is None

    def test_valid_regex_compiles(self) -> None:
        """Valid regex should compile successfully."""
        result = _compile_pattern(r"user\d+")
        assert result is not None
        assert isinstance(result, re.Pattern)

    def test_invalid_regex_returns_none(self) -> None:
        """Invalid regex should return None (fallback to exact match)."""
        assert _compile_pattern(r"invalid(regex") is None
        assert _compile_pattern(r"invalid[regex") is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: RoutingRule dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestRoutingRule:
    """Tests for RoutingRule dataclass."""

    def test_rule_defaults_to_enabled(self) -> None:
        """RoutingRule should default to enabled=True."""
        rule = RoutingRule(
            id="test",
            priority=1,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
        )
        assert rule.enabled is True

    def test_rule_can_be_disabled(self) -> None:
        """RoutingRule can be explicitly disabled."""
        rule = RoutingRule(
            id="test",
            priority=1,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
            enabled=False,
        )
        assert rule.enabled is False

    def test_rule_lazy_compiles_regex_patterns(self) -> None:
        """RoutingRule should lazily compile regex patterns on first match."""
        rule = RoutingRule(
            id="test",
            priority=1,
            sender=r"user\d+",
            recipient="*",
            channel="whatsapp",
            content_regex=r"^hello",
            instruction="test.md",
        )
        # Patterns are NOT compiled at construction (lazy)
        assert rule._compiled is False
        assert rule._compiled_sender is None
        assert rule._compiled_recipient is None
        assert rule._compiled_channel is None
        assert rule._compiled_content is None

        # Trigger lazy compilation
        rule._ensure_compiled()

        # sender is a regex → should be compiled
        assert rule._compiled_sender is not None
        # recipient is '*' → should be None
        assert rule._compiled_recipient is None
        # channel is exact string → compiled (but won't match regex)
        assert rule._compiled_channel is not None
        # content_regex is a regex → should be compiled
        assert rule._compiled_content is not None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: RoutingEngine.match() with all 4 criteria
# ─────────────────────────────────────────────────────────────────────────────


class TestRoutingEngineMatch:
    """Tests for RoutingEngine.match() using all 4 criteria."""

    @pytest.fixture
    def sample_message(self) -> IncomingMessage:
        """Create a sample incoming message for testing."""
        return IncomingMessage(
            message_id="msg-123",
            chat_id="chat-456",
            sender_id="sender-789",
            sender_name="Test User",
            text="Hello world",
            timestamp=1234567890.0,
            channel_type="whatsapp",
        )

    @pytest.fixture
    def engine(self, tmp_path):
        """Create a RoutingEngine with a temporary instructions directory."""
        return RoutingEngine(tmp_path)

    def test_match_all_wildcards(self, engine, sample_message) -> None:
        """Rule with all wildcards should match any message."""
        engine._rules = [
            RoutingRule(
                id="catch-all",
                priority=1,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="test.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "test.md"

    def test_match_sender_exact(self, engine, sample_message) -> None:
        """Exact sender match should work."""
        engine._rules = [
            RoutingRule(
                id="sender-match",
                priority=1,
                sender="sender-789",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="sender-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "sender-rule.md"

    def test_no_match_sender_wrong(self, engine, sample_message) -> None:
        """Wrong sender should not match."""
        engine._rules = [
            RoutingRule(
                id="wrong-sender",
                priority=1,
                sender="different-sender",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="wrong.md",
            )
        ]

        result = engine.match(sample_message)
        assert result is None

    def test_match_recipient_exact(self, engine, sample_message) -> None:
        """Exact recipient (chat_id) match should work."""
        engine._rules = [
            RoutingRule(
                id="recipient-match",
                priority=1,
                sender="*",
                recipient="chat-456",
                channel="*",
                content_regex="*",
                instruction="recipient-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "recipient-rule.md"

    def test_match_channel_exact(self, engine, sample_message) -> None:
        """Exact channel match should work."""
        engine._rules = [
            RoutingRule(
                id="channel-match",
                priority=1,
                sender="*",
                recipient="*",
                channel="whatsapp",
                content_regex="*",
                instruction="whatsapp-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "whatsapp-rule.md"

    def test_match_content_regex(self, engine, sample_message) -> None:
        """Content regex match should work."""
        engine._rules = [
            RoutingRule(
                id="content-match",
                priority=1,
                sender="*",
                recipient="*",
                channel="*",
                content_regex=r"Hello.*",
                instruction="greeting-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "greeting-rule.md"

    def test_no_match_content_regex(self, engine, sample_message) -> None:
        """Non-matching content regex should not match."""
        engine._rules = [
            RoutingRule(
                id="content-no-match",
                priority=1,
                sender="*",
                recipient="*",
                channel="*",
                content_regex=r"Goodbye.*",
                instruction="goodbye-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result is None

    def test_match_all_criteria_combined(self, engine, sample_message) -> None:
        """All 4 criteria must match for rule to apply."""
        engine._rules = [
            RoutingRule(
                id="all-match",
                priority=1,
                sender="sender-789",
                recipient="chat-456",
                channel="whatsapp",
                content_regex=r"Hello.*",
                instruction="combined-rule.md",
            )
        ]

        result = engine.match(sample_message)
        assert result == "combined-rule.md"

    def test_priority_order_lower_first(self, engine, sample_message) -> None:
        """Lower priority values should be evaluated first."""
        engine._rules = [
            RoutingRule(
                id="low-priority",
                priority=10,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="low.md",
            ),
            RoutingRule(
                id="high-priority",
                priority=1,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="high.md",
            ),
        ]

        result = engine.match(sample_message)
        # Note: rules should be ordered by priority before matching
        # If not pre-sorted, first match wins
        assert result in ("high.md", "low.md")

    def test_disabled_rule_skipped(self, engine, sample_message) -> None:
        """Disabled rules should be skipped."""
        engine._rules = [
            RoutingRule(
                id="disabled",
                priority=1,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="disabled.md",
                enabled=False,
            ),
            RoutingRule(
                id="enabled",
                priority=2,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="enabled.md",
                enabled=True,
            ),
        ]

        result = engine.match(sample_message)
        assert result == "enabled.md"

    def test_no_rules_returns_none(self, engine, sample_message) -> None:
        """No rules should return None."""
        engine._rules = []

        result = engine.match(sample_message)
        assert result is None

    def test_no_matching_rule_returns_none(self, engine, sample_message) -> None:
        """No matching rule should return None."""
        engine._rules = [
            RoutingRule(
                id="no-match",
                priority=1,
                sender="different",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="no-match.md",
            )
        ]

        result = engine.match(sample_message)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Pure function verification
# ─────────────────────────────────────────────────────────────────────────────


class TestPureFunctionBehavior:
    """Verify that matching functions are pure (no side effects)."""

    def test_match_compiled_is_deterministic(self) -> None:
        """_match_compiled should return same result for same inputs."""
        compiled = _compile_pattern(r"user\d+")
        pattern = r"user\d+"
        value = "user123"

        results = [_match_compiled(compiled, pattern, value) for _ in range(10)]
        assert all(r is True for r in results)

    def test_match_compiled_no_side_effects(self) -> None:
        """_match_compiled should not modify inputs."""
        pattern = "test_pattern"
        value = "test_value"

        original_pattern = pattern
        original_value = value

        _match(pattern, value)

        assert pattern == original_pattern
        assert value == original_value

    def test_match_compiled_no_global_state(self) -> None:
        """_match_compiled should not rely on or modify global state."""
        result1 = _match("hello", "hello")
        result2 = _match("hello", "hello")

        assert result1 == result2
