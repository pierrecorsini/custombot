"""Hypothesis property-based tests for the routing engine match logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.routing import (
    MatchingContext,
    RoutingEngine,
    RoutingRule,
    _compile_pattern,
    _match_compiled,
)


# ── Strategies ──────────────────────────────────────────────────────────────

# Simple alphanumeric identifiers for sender/chat IDs
simple_id = st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20)

# Patterns: either "*" (wildcard) or a simple string/regex
pattern_strategy = st.one_of(
    st.just("*"),
    st.text("abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=20),
)

# A MatchingContext strategy
matching_context_strategy = st.builds(
    MatchingContext,
    sender_id=simple_id,
    chat_id=simple_id,
    channel_type=st.sampled_from(["whatsapp", "cli", ""]),
    text=st.text(min_size=0, max_size=200),
    fromMe=st.booleans(),
    toMe=st.booleans(),
)

# A RoutingRule strategy with valid patterns
routing_rule_strategy = st.builds(
    RoutingRule,
    id=st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10),
    priority=st.integers(min_value=0, max_value=100),
    sender=pattern_strategy,
    recipient=pattern_strategy,
    channel=pattern_strategy,
    content_regex=pattern_strategy,
    instruction=st.text("abcdefghijklmnopqrstuvwxyz0123456789.md", min_size=5, max_size=20),
    enabled=st.booleans(),
    fromMe=st.one_of(st.none(), st.booleans()),
    toMe=st.one_of(st.none(), st.booleans()),
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_engine(*rules: RoutingRule) -> RoutingEngine:
    """Create a RoutingEngine with pre-loaded rules, bypassing filesystem I/O.

    Bypasses ``__init__`` to avoid filesystem dependencies and sets up the
    minimum internal state required by ``_match_impl``.  Rules are sorted
    by priority (ascending) before being loaded, matching the behavior of
    ``load_rules()``.
    """
    engine = RoutingEngine.__new__(RoutingEngine)
    # _match_cache must exist before _rules setter calls .clear()
    engine._match_cache = {}
    engine._instructions_dir = MagicMock()
    sorted_rules = sorted(rules, key=lambda r: r.priority)
    engine._rules = sorted_rules
    return engine


class TestCompilePattern:
    """Property-based tests for _compile_pattern."""

    @given(pattern=st.just("*"))
    @settings(max_examples=10)
    def test_wildcard_returns_none(self, pattern: str):
        assert _compile_pattern(pattern) is None

    @given(pattern=st.just(""))
    @settings(max_examples=10)
    def test_empty_returns_none(self, pattern: str):
        assert _compile_pattern(pattern) is None

    @given(pattern=st.from_regex(r"[a-z0-9]+", fullparse=True))
    @settings(max_examples=20)
    def test_valid_alphanumeric_compiles(self, pattern: str):
        result = _compile_pattern(pattern)
        assert result is not None
        assert result.match(pattern) is not None


class TestMatchCompiled:
    """Property-based tests for _match_compiled."""

    @given(value=simple_id)
    @settings(max_examples=20)
    def test_wildcard_matches_anything(self, value: str):
        compiled = _compile_pattern("*")
        assert _match_compiled(compiled, "*", value) is True

    @given(value=simple_id)
    @settings(max_examples=20)
    def test_empty_only_matches_empty(self, value: str):
        compiled = _compile_pattern("")
        if value == "":
            assert _match_compiled(compiled, "", value) is True
        else:
            assert _match_compiled(compiled, "", value) is False

    @given(value=simple_id)
    @settings(max_examples=20)
    def test_exact_match_works(self, value: str):
        compiled = _compile_pattern(value)
        assert _match_compiled(compiled, value, value) is True

    @given(value1=simple_id, value2=simple_id)
    @settings(max_examples=20)
    def test_different_values_dont_match(self, value1: str, value2: str):
        assume(value1 != value2)
        compiled = _compile_pattern(value1)
        assert _match_compiled(compiled, value1, value2) is False


class TestRoutingRuleWildcard:
    """Property: a fully wildcard rule matches any context."""

    @given(ctx=matching_context_strategy)
    @settings(max_examples=50)
    def test_wildcard_rule_matches_everything(self, ctx: MatchingContext):
        rule = RoutingRule(
            id="wildcard",
            priority=0,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
            enabled=True,
        )
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is rule
        assert instruction == "test.md"


class TestDisabledRuleNeverMatches:
    """Property: a disabled rule never matches regardless of context."""

    @given(ctx=matching_context_strategy)
    @settings(max_examples=50)
    def test_disabled_rule_skipped(self, ctx: MatchingContext):
        rule = RoutingRule(
            id="disabled",
            priority=0,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
            enabled=False,
        )
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is None
        assert instruction is None


class TestPriorityOrdering:
    """Property: lower priority rules are evaluated first and match first."""

    @given(
        ctx=matching_context_strategy,
        low_pri=st.integers(min_value=0, max_value=50),
        high_pri=st.integers(min_value=51, max_value=100),
    )
    @settings(max_examples=30)
    def test_lower_priority_wins(
        self, ctx: MatchingContext, low_pri: int, high_pri: int
    ):
        rule_low = RoutingRule(
            id="low", priority=low_pri, sender="*", recipient="*",
            channel="*", content_regex="*", instruction="low.md",
        )
        rule_high = RoutingRule(
            id="high", priority=high_pri, sender="*", recipient="*",
            channel="*", content_regex="*", instruction="high.md",
        )
        # _make_engine sorts by priority, matching load_rules() behavior
        engine = _make_engine(rule_high, rule_low)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is rule_low
        assert instruction == "low.md"


class TestFromMeToMeFiltering:
    """Property: fromMe/toMe constraints correctly filter contexts."""

    @given(
        ctx=matching_context_strategy,
        fromMe_filter=st.one_of(st.none(), st.booleans()),
        toMe_filter=st.one_of(st.none(), st.booleans()),
    )
    @settings(max_examples=50)
    def test_fromMe_toMe_filtering(
        self, ctx: MatchingContext, fromMe_filter: bool | None, toMe_filter: bool | None
    ):
        rule = RoutingRule(
            id="filtered", priority=0, sender="*", recipient="*",
            channel="*", content_regex="*", instruction="test.md",
            fromMe=fromMe_filter, toMe=toMe_filter,
        )
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)

        should_match = True
        if fromMe_filter is not None and fromMe_filter != ctx.fromMe:
            should_match = False
        if toMe_filter is not None and toMe_filter != ctx.toMe:
            should_match = False

        if should_match:
            assert matched_rule is rule
        else:
            assert matched_rule is None
