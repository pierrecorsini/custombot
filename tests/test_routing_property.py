"""
test_routing_property.py — Property-based tests for the routing engine.

Uses Hypothesis to generate arbitrary message/rule combinations and verify:
  - Routing never crashes (no unhandled exceptions)
  - Routing is deterministic (same input → same output)
  - Priority ordering is respected (lower priority matches first)
  - No match returns None gracefully
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.routing import (
    MatchingContext,
    RoutingEngine,
    RoutingRule,
)


# ── Strategies ──────────────────────────────────────────────────────────────

_simple_id = st.text("abcdefghijklmnopqrstuvwxyz0123456789_.-", min_size=1, max_size=20)

_pattern = st.one_of(
    st.just("*"),
    st.text("abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=20),
)

_context = st.builds(
    MatchingContext,
    sender_id=_simple_id,
    chat_id=_simple_id,
    channel_type=st.sampled_from(["whatsapp", "cli", "telegram", ""]),
    text=st.text(min_size=0, max_size=200),
    fromMe=st.booleans(),
    toMe=st.booleans(),
)

_rule = st.builds(
    RoutingRule,
    id=st.text("abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10),
    priority=st.integers(min_value=0, max_value=100),
    sender=_pattern,
    recipient=_pattern,
    channel=_pattern,
    content_regex=_pattern,
    instruction=st.sampled_from(["chat.md", "admin.md", "tools.md", "fallback.md"]),
    enabled=st.booleans(),
    fromMe=st.one_of(st.none(), st.booleans()),
    toMe=st.one_of(st.none(), st.booleans()),
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_engine(*rules: RoutingRule) -> RoutingEngine:
    """Create a RoutingEngine with pre-loaded rules, bypassing filesystem."""
    engine = RoutingEngine.__new__(RoutingEngine)
    engine._match_cache = {}
    engine._instructions_dir = MagicMock()
    sorted_rules = sorted(rules, key=lambda r: r.priority)
    engine._rules = sorted_rules
    return engine


# ── Properties ──────────────────────────────────────────────────────────────


class TestRoutingNeverCrashes:
    """Routing never raises unhandled exceptions for any valid input."""

    @given(ctx=_context, rule=_rule)
    @settings(max_examples=100)
    def test_single_rule_never_crashes(self, ctx: MatchingContext, rule: RoutingRule):
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)
        # Must return a tuple, never raise
        assert isinstance(matched_rule, (RoutingRule, type(None)))
        assert isinstance(instruction, (str, type(None)))

    @given(
        ctx=_context,
        rules=st.lists(_rule, min_size=0, max_size=10),
    )
    @settings(max_examples=50)
    def test_many_rules_never_crashes(self, ctx: MatchingContext, rules: list[RoutingRule]):
        engine = _make_engine(*rules)
        matched_rule, instruction = engine._match_impl(ctx)
        assert isinstance(matched_rule, (RoutingRule, type(None)))
        assert isinstance(instruction, (str, type(None)))

    @given(ctx=_context)
    @settings(max_examples=20)
    def test_no_rules_returns_none(self, ctx: MatchingContext):
        engine = _make_engine()
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is None
        assert instruction is None


class TestRoutingDeterministic:
    """Same input always produces the same output."""

    @given(ctx=_context, rule=_rule)
    @settings(max_examples=50)
    def test_deterministic_single_rule(self, ctx: MatchingContext, rule: RoutingRule):
        engine = _make_engine(rule)
        result1 = engine._match_impl(ctx)
        result2 = engine._match_impl(ctx)
        assert result1 == result2

    @given(ctx=_context, rules=st.lists(_rule, min_size=1, max_size=5))
    @settings(max_examples=30)
    def test_deterministic_multiple_rules(self, ctx: MatchingContext, rules: list[RoutingRule]):
        engine = _make_engine(*rules)
        result1 = engine._match_impl(ctx)
        result2 = engine._match_impl(ctx)
        assert result1 == result2


class TestPriorityOrdering:
    """Lower priority rules are evaluated first and match first."""

    @given(
        ctx=_context,
        low_pri=st.integers(min_value=0, max_value=49),
        high_pri=st.integers(min_value=50, max_value=100),
    )
    @settings(max_examples=50)
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
        engine = _make_engine(rule_high, rule_low)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is rule_low
        assert instruction == "low.md"

    @given(
        ctx=_context,
        priorities=st.lists(st.integers(min_value=0, max_value=100), min_size=3, max_size=5),
    )
    @settings(max_examples=30)
    def test_minimum_priority_always_wins(
        self, ctx: MatchingContext, priorities: list[int]
    ):
        assume(len(set(priorities)) == len(priorities))
        rules = [
            RoutingRule(
                id=f"rule_{p}", priority=p, sender="*", recipient="*",
                channel="*", content_regex="*", instruction=f"f_{p}.md",
            )
            for p in priorities
        ]
        engine = _make_engine(*rules)
        matched_rule, instruction = engine._match_impl(ctx)
        min_p = min(priorities)
        assert matched_rule is not None
        assert matched_rule.priority == min_p


class TestNoMatchReturnsNone:
    """When no rule matches, the engine returns (None, None) gracefully."""

    @given(ctx=_context)
    @settings(max_examples=30)
    def test_disabled_rules_produce_no_match(self, ctx: MatchingContext):
        rule = RoutingRule(
            id="off", priority=0, sender="*", recipient="*",
            channel="*", content_regex="*", instruction="off.md",
            enabled=False,
        )
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is None
        assert instruction is None

    @given(ctx=_context, sender=st.text("xyz", min_size=1, max_size=5))
    @settings(max_examples=30)
    def test_specific_sender_no_match(self, ctx: MatchingContext, sender: str):
        assume(ctx.sender_id != sender)
        rule = RoutingRule(
            id="specific", priority=0, sender=sender, recipient="*",
            channel="*", content_regex="*", instruction="spec.md",
        )
        engine = _make_engine(rule)
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is None
        assert instruction is None

    @given(ctx=_context)
    @settings(max_examples=20)
    def test_empty_engine_returns_none(self, ctx: MatchingContext):
        engine = _make_engine()
        matched_rule, instruction = engine._match_impl(ctx)
        assert matched_rule is None
        assert instruction is None
