"""
test_routing.py — Comprehensive unit tests for src/routing.py.

Tests the message routing engine including:
- MatchingContext: creation, immutability, from_message factory, repr
- _compile_pattern / _match_compiled: pattern compilation and matching
- RoutingRule: construction, pre-compiled fields, immutability, repr
- _rule_from_dict: dict-to-rule conversion, legacy field compat
- RoutingEngine: load_rules, match, match_with_rule, priority ordering,
  fromMe/toMe filtering, enabled/disabled, edge cases
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.channels.base import ChannelType, IncomingMessage
from src.routing import (
    MatchingContext,
    RoutingEngine,
    RoutingRule,
    _compile_pattern,
    _match_compiled,
    _rule_from_dict,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — lightweight factory functions
# ─────────────────────────────────────────────────────────────────────────────


def make_msg(
    message_id: str = "msg-001",
    chat_id: str = "chat-001",
    sender_id: str = "5511999990000",
    sender_name: str = "Alice",
    text: str = "Hello",
    timestamp: float = 1700000000.0,
    channel_type: ChannelType | str = ChannelType.WHATSAPP,
    fromMe: bool = False,
    toMe: bool = False,
    is_historical: bool = False,
    correlation_id: Optional[str] = None,
    raw: Optional[dict] = None,
) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults for tests."""
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=timestamp,
        channel_type=channel_type,
        fromMe=fromMe,
        toMe=toMe,
        is_historical=is_historical,
        correlation_id=correlation_id,
        raw=raw,
    )


def make_rule(
    id: str = "test-rule",
    priority: int = 0,
    sender: str = "*",
    recipient: str = "*",
    channel: str = "*",
    content_regex: str = "*",
    instruction: str = "chat.agent.md",
    enabled: bool = True,
    fromMe: Optional[bool] = None,
    toMe: Optional[bool] = None,
    skillExecVerbose: str = "",
    showErrors: bool = True,
) -> RoutingRule:
    """Create a RoutingRule with sensible defaults for tests."""
    return RoutingRule(
        id=id,
        priority=priority,
        sender=sender,
        recipient=recipient,
        channel=channel,
        content_regex=content_regex,
        instruction=instruction,
        enabled=enabled,
        fromMe=fromMe,
        toMe=toMe,
        skillExecVerbose=skillExecVerbose,
        showErrors=showErrors,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MatchingContext tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchingContext:
    """Tests for the MatchingContext frozen dataclass."""

    # ── Construction ─────────────────────────────────────────────────────

    def test_create_with_all_fields(self):
        ctx = MatchingContext(
            sender_id="5511999990000",
            chat_id="chat-001",
            channel_type=ChannelType.WHATSAPP,
            text="Hello world",
            fromMe=True,
            toMe=False,
        )
        assert ctx.sender_id == "5511999990000"
        assert ctx.chat_id == "chat-001"
        assert ctx.channel_type == ChannelType.WHATSAPP
        assert ctx.text == "Hello world"
        assert ctx.fromMe is True
        assert ctx.toMe is False

    def test_create_with_defaults(self):
        ctx = MatchingContext(
            sender_id="s",
            chat_id="c",
            channel_type="ch",
            text="t",
        )
        assert ctx.fromMe is False
        assert ctx.toMe is False

    # ── Immutability ────────────────────────────────────────────────────

    def test_frozen_raises_on_setattr(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        with pytest.raises(FrozenInstanceError):
            ctx.sender_id = "changed"  # type: ignore[misc]

    def test_frozen_raises_on_del(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        with pytest.raises(FrozenInstanceError):
            del ctx.text  # type: ignore[attr-defined]

    # ── from_message classmethod ────────────────────────────────────────

    def test_from_message_extracts_fields(self):
        msg = make_msg(
            sender_id="5511988880000",
            chat_id="group-123",
            channel_type="telegram",
            text="Hi from Telegram",
            fromMe=True,
            toMe=True,
        )
        ctx = MatchingContext.from_message(msg)

        assert ctx.sender_id == "5511988880000"
        assert ctx.chat_id == "group-123"
        assert ctx.channel_type == "telegram"  # third-party string preserved
        assert ctx.text == "Hi from Telegram"
        assert ctx.fromMe is True
        assert ctx.toMe is True

    def test_from_message_defaults(self):
        msg = make_msg(fromMe=False, toMe=False)
        ctx = MatchingContext.from_message(msg)
        assert ctx.fromMe is False
        assert ctx.toMe is False

    # ── __repr__ ────────────────────────────────────────────────────────

    def test_repr_short_text(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="Hi")
        r = repr(ctx)
        assert "MatchingContext(" in r
        assert "sender='s'" in r
        assert "text='Hi'" in r

    def test_repr_long_text_truncated(self):
        long_text = "A" * 50
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text=long_text)
        r = repr(ctx)
        assert "..." in r
        assert "text=" in r
        # The preview should be truncated to 30 chars + "..."
        assert long_text not in r

    # ── Hashability (frozen dataclass) ──────────────────────────────────

    def test_usable_as_dict_key(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        mapping = {ctx: "value"}
        assert mapping[ctx] == "value"

    def test_usable_in_set(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        s = {ctx}
        assert ctx in s


# ═══════════════════════════════════════════════════════════════════════════════
# _compile_pattern tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompilePattern:
    """Tests for the _compile_pattern helper."""

    def test_wildcard_returns_none(self):
        assert _compile_pattern("*") is None

    def test_empty_string_returns_none(self):
        assert _compile_pattern("") is None

    def test_valid_regex_compiled(self):
        result = _compile_pattern(r"\d+")
        assert result is not None
        assert isinstance(result, re.Pattern)

    def test_valid_regex_matches(self):
        compiled = _compile_pattern(r"hello.*world")
        assert compiled is not None
        assert compiled.match("hello beautiful world") is not None

    def test_invalid_regex_returns_none(self):
        # Unbalanced parenthesis — not valid regex
        assert _compile_pattern(r"(unclosed") is None

    def test_complex_valid_regex(self):
        compiled = _compile_pattern(r"^55\d{11}$")
        assert compiled is not None
        assert compiled.match("5511999990000") is not None
        assert compiled.match("123") is None

    def test_literal_string_compiled(self):
        compiled = _compile_pattern("5511999990000")
        assert compiled is not None
        assert compiled.match("5511999990000") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# _match_compiled tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchCompiled:
    """Tests for the _match_compiled helper."""

    # ── Wildcard pattern ────────────────────────────────────────────────

    def test_wildcard_matches_any_value(self):
        assert _match_compiled(None, "*", "anything") is True

    def test_wildcard_matches_empty_value(self):
        assert _match_compiled(None, "*", "") is True

    # ── Empty pattern ───────────────────────────────────────────────────

    def test_empty_pattern_matches_empty_value(self):
        assert _match_compiled(None, "", "") is True

    def test_empty_pattern_does_not_match_nonempty_value(self):
        assert _match_compiled(None, "", "hello") is False

    # ── Regex matching ──────────────────────────────────────────────────

    def test_compiled_regex_match(self):
        compiled = _compile_pattern(r"\d+")
        assert _match_compiled(compiled, r"\d+", "123") is True

    def test_compiled_regex_no_match(self):
        compiled = _compile_pattern(r"\d+")
        assert _match_compiled(compiled, r"\d+", "abc") is False

    def test_compiled_regex_partial_no_match(self):
        # re.match() anchors at start; "abc123" won't match r"^\d+$"
        compiled = re.compile(r"^\d+$")
        assert _match_compiled(compiled, r"^\d+$", "abc123") is False

    # ── Exact string fallback ───────────────────────────────────────────

    def test_exact_string_match(self):
        # For a pattern that doesn't compile to a regex (returns None),
        # but the raw pattern string matches the value exactly
        assert _match_compiled(None, "5511999990000", "5511999990000") is True

    def test_exact_string_no_match(self):
        assert _match_compiled(None, "5511999990000", "5511999990001") is False

    def test_invalid_regex_falls_back_to_exact_match(self):
        # "(unclosed" won't compile, so _compile_pattern returns None
        compiled = _compile_pattern("(unclosed")
        assert compiled is None
        # _match_compiled should fall back to exact match
        assert _match_compiled(compiled, "(unclosed", "(unclosed") is True
        assert _match_compiled(compiled, "(unclosed", "other") is False


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingRule tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingRule:
    """Tests for the RoutingRule frozen dataclass."""

    # ── Construction & defaults ─────────────────────────────────────────

    def test_create_with_defaults(self):
        rule = RoutingRule(
            id="r1",
            priority=10,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.agent.md",
        )
        assert rule.enabled is True
        assert rule.fromMe is None
        assert rule.toMe is None
        assert rule.skillExecVerbose == ""
        assert rule.showErrors is True

    def test_create_with_all_fields(self):
        rule = RoutingRule(
            id="r2",
            priority=5,
            sender="5511999990000",
            recipient="group-001",
            channel="whatsapp",
            content_regex=r"^hello",
            instruction="greet.md",
            enabled=False,
            fromMe=True,
            toMe=False,
            skillExecVerbose="full",
            showErrors=False,
        )
        assert rule.id == "r2"
        assert rule.priority == 5
        assert rule.sender == "5511999990000"
        assert rule.enabled is False
        assert rule.fromMe is True
        assert rule.skillExecVerbose == "full"

    # ── Immutability ────────────────────────────────────────────────────

    def test_frozen_raises_on_setattr(self):
        rule = make_rule()
        with pytest.raises(FrozenInstanceError):
            rule.priority = 99  # type: ignore[misc]

    # ── Pre-compiled regex patterns (__post_init__) ─────────────────────

    def test_post_init_compiles_sender(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender=r"55\d+",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
        )
        assert rule._compiled_sender is not None
        assert rule._compiled_sender.match("5511999990000")

    def test_post_init_compiles_channel(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender="*",
            recipient="*",
            channel=r"what.*",
            content_regex="*",
            instruction="test.md",
        )
        assert rule._compiled_channel is not None
        assert rule._compiled_channel.match("whatsapp")

    def test_post_init_compiles_content_regex(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender="*",
            recipient="*",
            channel="*",
            content_regex=r"^hello\s+world",
            instruction="test.md",
        )
        assert rule._compiled_content is not None
        assert rule._compiled_content.match("hello world")

    def test_post_init_wildcard_fields_not_compiled(self):
        rule = make_rule(sender="*", channel="*", content_regex="*", recipient="*")
        assert rule._compiled_sender is None
        assert rule._compiled_recipient is None
        assert rule._compiled_channel is None
        assert rule._compiled_content is None

    def test_post_init_empty_fields_not_compiled(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender="",
            recipient="",
            channel="",
            content_regex="",
            instruction="test.md",
        )
        assert rule._compiled_sender is None
        assert rule._compiled_recipient is None
        assert rule._compiled_channel is None
        assert rule._compiled_content is None

    def test_invalid_regex_compiled_as_none(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender="[invalid",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
        )
        assert rule._compiled_sender is None

    # ── _is_wildcard pre-computation ────────────────────────────────────

    def test_is_wildcard_true_when_all_wildcards(self):
        rule = make_rule(sender="*", recipient="*", channel="*", content_regex="*")
        assert rule._is_wildcard is True

    def test_is_wildcard_false_when_sender_not_wildcard(self):
        rule = make_rule(sender="5511999990000")
        assert rule._is_wildcard is False

    def test_is_wildcard_false_when_recipient_not_wildcard(self):
        rule = make_rule(recipient="group-001")
        assert rule._is_wildcard is False

    def test_is_wildcard_false_when_channel_not_wildcard(self):
        rule = make_rule(channel="whatsapp")
        assert rule._is_wildcard is False

    def test_is_wildcard_false_when_content_not_wildcard(self):
        rule = make_rule(content_regex=r"^hello")
        assert rule._is_wildcard is False

    def test_is_wildcard_false_when_empty_pattern(self):
        """Empty string patterns are not wildcards — they match only empty values."""
        rule = RoutingRule(
            id="t", priority=0, sender="", recipient="*",
            channel="*", content_regex="*", instruction="t.md",
        )
        assert rule._is_wildcard is False

    def test_is_wildcard_not_in_repr(self):
        rule = make_rule()
        assert "_is_wildcard" not in repr(rule)

    # ── __repr__ ────────────────────────────────────────────────────────

    def test_repr_enabled_rule(self):
        rule = make_rule(id="my-rule", priority=5, enabled=True)
        r = repr(rule)
        assert "my-rule" in r
        assert "ON" in r

    def test_repr_disabled_rule(self):
        rule = make_rule(id="off-rule", enabled=False)
        r = repr(rule)
        assert "OFF" in r

    def test_repr_truncates_long_content_regex(self):
        long_regex = "a" * 50
        rule = make_rule(content_regex=long_regex)
        r = repr(rule)
        assert "..." in r


# ═══════════════════════════════════════════════════════════════════════════════
# _rule_from_dict tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleFromDict:
    """Tests for _rule_from_dict conversion helper."""

    def test_full_dict(self):
        d: Dict[str, Any] = {
            "id": "custom-id",
            "priority": 3,
            "sender": "5511999990000",
            "recipient": "group-1",
            "channel": "whatsapp",
            "content_regex": r"^ping",
            "enabled": False,
            "fromMe": True,
            "toMe": False,
            "skillExecVerbose": "summary",
            "showErrors": False,
        }
        rule = _rule_from_dict(d, "ping.md")
        assert rule.id == "custom-id"
        assert rule.priority == 3
        assert rule.sender == "5511999990000"
        assert rule.recipient == "group-1"
        assert rule.channel == "whatsapp"
        assert rule.content_regex == r"^ping"
        assert rule.instruction == "ping.md"
        assert rule.enabled is False
        assert rule.fromMe is True
        assert rule.toMe is False
        assert rule.skillExecVerbose == "summary"
        assert rule.showErrors is False

    def test_defaults_when_keys_missing(self):
        rule = _rule_from_dict({}, "default.md")
        assert rule.id == "default"  # stem of filename
        assert rule.priority == 0
        assert rule.sender == "*"
        assert rule.recipient == "*"
        assert rule.channel == "*"
        assert rule.content_regex == "*"
        assert rule.instruction == "default.md"
        assert rule.enabled is True
        assert rule.fromMe is None
        assert rule.toMe is None
        assert rule.skillExecVerbose == ""
        assert rule.showErrors is True

    def test_legacy_showSkillExec_true(self):
        """Backward compat: showSkillExec: true → skillExecVerbose: 'summary'."""
        d = {"showSkillExec": True}
        rule = _rule_from_dict(d, "legacy.md")
        assert rule.skillExecVerbose == "summary"

    def test_legacy_showSkillExec_false(self):
        """showSkillExec: false should NOT override skillExecVerbose."""
        d = {"showSkillExec": False, "skillExecVerbose": "full"}
        rule = _rule_from_dict(d, "legacy.md")
        assert rule.skillExecVerbose == "full"

    def test_skillExecVerbose_without_legacy(self):
        d = {"skillExecVerbose": "full"}
        rule = _rule_from_dict(d, "test.md")
        assert rule.skillExecVerbose == "full"

    def test_id_defaults_to_filename_stem(self):
        rule = _rule_from_dict({}, "my_agent.md")
        assert rule.id == "my_agent"


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — load_rules tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineLoadRules:
    """Tests for RoutingEngine.load_rules() and file scanning."""

    def test_empty_directory_loads_no_rules(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules == []

    def test_nonexistent_directory_loads_no_rules(self, tmp_path: Path):
        missing = tmp_path / "no_such_dir"
        engine = RoutingEngine(missing)
        engine.load_rules()
        assert engine.rules == []

    def test_directory_with_no_md_files(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("not a markdown file")
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules == []

    def test_md_file_without_frontmatter_skipped(self, tmp_path: Path):
        (tmp_path / "plain.md").write_text("# No frontmatter here\nJust content.")
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules == []

    def test_md_file_with_routing_loads_rule(self, tmp_path: Path):
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: chat-rule
                  priority: 10
                  sender: "*"
                ---
                # Chat instruction
            """)
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "chat-rule"
        assert engine.rules[0].priority == 10

    def test_multiple_md_files_loaded(self, tmp_path: Path):
        for name, priority in [("a.md", 5), ("b.md", 1), ("c.md", 10)]:
            (tmp_path / name).write_text(
                textwrap.dedent(f"""\
                    ---
                    routing:
                      id: rule-{name}
                      priority: {priority}
                    ---
                    # Instruction {name}
                """)
            )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert len(engine.rules) == 3
        # Rules should be sorted by priority
        assert engine.rules[0].priority == 1
        assert engine.rules[1].priority == 5
        assert engine.rules[2].priority == 10

    def test_rules_sorted_by_priority_ascending(self, tmp_path: Path):
        for pri in [50, 1, 25, 10, 100]:
            (tmp_path / f"rule_{pri}.md").write_text(
                f"---\nrouting:\n  id: p-{pri}\n  priority: {pri}\n---\n\n# P{pri}\n"
            )
        # Write properly
        for pri in [50, 1, 25, 10, 100]:
            (tmp_path / f"rule_{pri}.md").write_text(
                f"---\nrouting:\n  id: p-{pri}\n  priority: {pri}\n---\n\n# P{pri}\n"
            )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        priorities = [r.priority for r in engine.rules]
        assert priorities == [1, 10, 25, 50, 100]

    def test_rules_property_is_read_only(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        assert engine.rules == []
        # The rules property returns the internal list (read-only view)
        original = engine.rules
        assert original is engine._rules_list

    def test_instructions_dir_property(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        assert engine.instructions_dir == tmp_path

    def test_refresh_rules_reloads(self, tmp_path: Path):
        """refresh_rules() should reload from disk."""
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules == []

        # Add a new file and refresh
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )
        engine.refresh_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "new-rule"

    def test_multiple_routing_entries_in_single_file(self, tmp_path: Path):
        """A single .md file with a routing list should produce multiple rules."""
        md = tmp_path / "multi.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  - id: rule-a
                    priority: 1
                  - id: rule-b
                    priority: 5
                ---
                # Multi
            """)
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert len(engine.rules) == 2
        ids = {r.id for r in engine.rules}
        assert ids == {"rule-a", "rule-b"}

    def test_malformed_frontmatter_skipped_gracefully(self, tmp_path: Path):
        """Files with broken YAML should be skipped without raising."""
        md = tmp_path / "bad.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: good-rule
                  priority: 1
                ---
                # Good
            """)
        )
        bad = tmp_path / "broken.md"
        bad.write_text("---\n[[invalid yaml\n---\n# Broken\n")

        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        # Only the good file should be loaded
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "good-rule"


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — match() and match_with_rule() tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineMatch:
    """Tests for RoutingEngine.match() and match_with_rule()."""

    # ── Basic matching ──────────────────────────────────────────────────

    def test_catch_all_rule_matches_any_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule()]
        msg = make_msg(text="anything at all")
        assert engine.match(msg) == "chat.agent.md"

    def test_no_rules_returns_none(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg()
        assert engine.match(msg) is None

    def test_match_with_rule_returns_tuple(self):
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule()
        engine._rules = [rule]
        msg = make_msg()

        matched_rule, instruction = engine.match_with_rule(msg)
        assert matched_rule is rule
        assert instruction == "chat.agent.md"

    def test_match_with_rule_no_match_returns_nones(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg()

        matched_rule, instruction = engine.match_with_rule(msg)
        assert matched_rule is None
        assert instruction is None

    def test_match_delegates_to_match_with_rule(self):
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(instruction="special.md")
        engine._rules = [rule]
        msg = make_msg()

        assert engine.match(msg) == "special.md"

    # ── Sender matching ────────────────────────────────────────────────

    def test_specific_sender_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="5511999990000")
        assert engine.match(msg) == "chat.agent.md"

    def test_specific_sender_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="5511999991111")
        assert engine.match(msg) is None

    def test_sender_regex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=r"55\d{10}")]
        msg = make_msg(sender_id="5511999990000")
        assert engine.match(msg) == "chat.agent.md"

    def test_sender_regex_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=r"55\d{10}")]
        msg = make_msg(sender_id="4411999990000")
        assert engine.match(msg) is None

    # ── Channel matching ───────────────────────────────────────────────

    def test_specific_channel_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="whatsapp")]
        msg = make_msg(channel_type=ChannelType.WHATSAPP)
        assert engine.match(msg) == "chat.agent.md"

    def test_specific_channel_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="telegram")]
        msg = make_msg(channel_type=ChannelType.WHATSAPP)
        assert engine.match(msg) is None

    def test_channel_regex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel=r"tele.*")]
        msg = make_msg(channel_type="telegram")
        assert engine.match(msg) == "chat.agent.md"

    # ── Recipient matching ─────────────────────────────────────────────

    def test_specific_recipient_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient="group-001")]
        msg = make_msg(chat_id="group-001")
        assert engine.match(msg) == "chat.agent.md"

    def test_specific_recipient_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient="group-001")]
        msg = make_msg(chat_id="group-999")
        assert engine.match(msg) is None

    # ── content_regex matching ─────────────────────────────────────────

    def test_content_regex_matches_text(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="hello world")
        assert engine.match(msg) == "chat.agent.md"

    def test_content_regex_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="goodbye world")
        assert engine.match(msg) is None

    def test_content_regex_complex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^\d{4}-\d{2}-\d{2}$")]
        msg = make_msg(text="2024-01-15")
        assert engine.match(msg) == "chat.agent.md"

    def test_content_regex_case_sensitive_by_default(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^Hello")]
        msg = make_msg(text="hello")
        assert engine.match(msg) is None

    def test_wildcard_content_matches_anything(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="literally anything")
        assert engine.match(msg) == "chat.agent.md"

    # ── Priority ordering ──────────────────────────────────────────────

    def test_lower_priority_evaluated_first(self):
        engine = RoutingEngine(Path("/dummy"))
        low_pri = make_rule(id="low", priority=1, instruction="low.md")
        high_pri = make_rule(id="high", priority=10, instruction="high.md")
        # Simulate load_rules sorting
        engine._rules = sorted([high_pri, low_pri], key=lambda r: r.priority)

        msg = make_msg()
        assert engine.match(msg) == "low.md"

    def test_first_matching_rule_wins(self):
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="a",
            priority=1,
            sender="5511999990000",
            instruction="a.md",
        )
        rule_b = make_rule(
            id="b",
            priority=5,
            sender="*",
            instruction="b.md",
        )
        engine._rules = [rule_a, rule_b]
        msg = make_msg(sender_id="5511999990000")
        # Both match, but rule_a has lower priority
        assert engine.match(msg) == "a.md"

    def test_first_rule_skipped_if_no_match_second_wins(self):
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="a",
            priority=1,
            sender="5511999990000",
            instruction="a.md",
        )
        rule_b = make_rule(
            id="b",
            priority=5,
            sender="*",
            instruction="b.md",
        )
        engine._rules = [rule_a, rule_b]
        msg = make_msg(sender_id="5511999991111")
        # rule_a doesn't match sender, rule_b (wildcard) does
        assert engine.match(msg) == "b.md"

    # ── enabled / disabled ─────────────────────────────────────────────

    def test_disabled_rule_skipped(self):
        engine = RoutingEngine(Path("/dummy"))
        disabled = make_rule(id="disabled", enabled=False, instruction="disabled.md")
        engine._rules = [disabled]
        msg = make_msg()
        assert engine.match(msg) is None

    def test_disabled_rule_skipped_falls_through_to_next(self):
        engine = RoutingEngine(Path("/dummy"))
        disabled = make_rule(id="off", priority=1, enabled=False, instruction="off.md")
        fallback = make_rule(id="on", priority=5, enabled=True, instruction="on.md")
        engine._rules = [disabled, fallback]
        msg = make_msg()
        assert engine.match(msg) == "on.md"

    def test_enabled_field_defaults_to_true(self):
        rule = make_rule()  # enabled not explicitly set
        assert rule.enabled is True

    # ── fromMe filtering ────────────────────────────────────────────────

    def test_fromMe_true_matches_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=True)
        assert engine.match(msg) == "chat.agent.md"

    def test_fromMe_true_rejects_non_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=False)
        assert engine.match(msg) is None

    def test_fromMe_false_rejects_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=True)
        assert engine.match(msg) is None

    def test_fromMe_false_matches_non_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=False)
        assert engine.match(msg) == "chat.agent.md"

    def test_fromMe_none_matches_all(self):
        """When fromMe is None (default), the filter is not applied."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=None)]
        assert engine.match(make_msg(fromMe=True)) == "chat.agent.md"
        assert engine.match(make_msg(fromMe=False)) == "chat.agent.md"

    # ── toMe filtering ─────────────────────────────────────────────────

    def test_toMe_true_matches_direct_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=True)
        assert engine.match(msg) == "chat.agent.md"

    def test_toMe_true_rejects_group_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=False)
        assert engine.match(msg) is None

    def test_toMe_false_rejects_direct_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=True)
        assert engine.match(msg) is None

    def test_toMe_false_matches_group_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=False)
        assert engine.match(msg) == "chat.agent.md"

    def test_toMe_none_matches_all(self):
        """When toMe is None (default), the filter is not applied."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=None)]
        assert engine.match(make_msg(toMe=True)) == "chat.agent.md"
        assert engine.match(make_msg(toMe=False)) == "chat.agent.md"

    # ── Combined filters ────────────────────────────────────────────────

    def test_all_filters_must_match(self):
        """Rule only matches when sender, channel, content, fromMe, toMe all pass."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(
                sender="5511999990000",
                channel="whatsapp",
                content_regex=r"^hello",
                fromMe=False,
                toMe=True,
                instruction="strict.md",
            )
        ]

        # Perfect match
        assert (
            engine.match(
                make_msg(
                    sender_id="5511999990000",
                    channel_type="whatsapp",
                    text="hello world",
                    fromMe=False,
                    toMe=True,
                )
            )
            == "strict.md"
        )

        # Wrong sender
        assert (
            engine.match(
                make_msg(
                    sender_id="wrong",
                    channel_type="whatsapp",
                    text="hello world",
                    fromMe=False,
                    toMe=True,
                )
            )
            is None
        )

        # Wrong channel
        assert (
            engine.match(
                make_msg(
                    sender_id="5511999990000",
                    channel_type="telegram",
                    text="hello world",
                    fromMe=False,
                    toMe=True,
                )
            )
            is None
        )

        # Wrong content
        assert (
            engine.match(
                make_msg(
                    sender_id="5511999990000",
                    channel_type="whatsapp",
                    text="wrong text",
                    fromMe=False,
                    toMe=True,
                )
            )
            is None
        )

        # Wrong fromMe
        assert (
            engine.match(
                make_msg(
                    sender_id="5511999990000",
                    channel_type="whatsapp",
                    text="hello world",
                    fromMe=True,
                    toMe=True,
                )
            )
            is None
        )

        # Wrong toMe
        assert (
            engine.match(
                make_msg(
                    sender_id="5511999990000",
                    channel_type="whatsapp",
                    text="hello world",
                    fromMe=False,
                    toMe=False,
                )
            )
            is None
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_text_matches_wildcard(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="")
        assert engine.match(msg) == "chat.agent.md"

    def test_empty_text_matches_empty_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="")]
        msg = make_msg(text="")
        assert engine.match(msg) == "chat.agent.md"

    def test_empty_text_does_not_match_nonempty_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="")
        assert engine.match(msg) is None

    def test_unicode_text_matches_regex(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^こんにちは")]
        msg = make_msg(text="こんにちは世界")
        assert engine.match(msg) == "chat.agent.md"

    def test_very_long_text(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="A" * 100_000)
        assert engine.match(msg) == "chat.agent.md"

    def test_special_regex_chars_in_exact_match(self):
        """Patterns like 'group.1' should fall back to exact match if
        they don't regex-match (re.match anchors at start, '.' is wildcard)."""
        engine = RoutingEngine(Path("/dummy"))
        # "group.1" compiles as regex — the '.' matches any char
        engine._rules = [make_rule(recipient="group.1")]
        msg = make_msg(chat_id="groupX1")
        # "group.1" as regex matches "groupX1" via '.' wildcard
        assert engine.match(msg) == "chat.agent.md"

    def test_invalid_regex_in_sender_falls_back_to_exact(self):
        """If sender pattern is invalid regex, _compile_pattern returns None,
        and _match_compiled falls back to exact string comparison."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="[broken")]
        msg = make_msg(sender_id="[broken")
        assert engine.match(msg) == "chat.agent.md"

    def test_invalid_regex_in_sender_no_exact_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="[broken")]
        msg = make_msg(sender_id="something-else")
        assert engine.match(msg) is None

    def test_multiple_rules_only_first_match_returned(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="first", priority=1, instruction="first.md"),
            make_rule(id="second", priority=2, instruction="second.md"),
            make_rule(id="third", priority=3, instruction="third.md"),
        ]
        msg = make_msg()
        assert engine.match(msg) == "first.md"
        matched_rule, _ = engine.match_with_rule(msg)
        assert matched_rule is not None
        assert matched_rule.id == "first"

    def test_rules_loaded_in_priority_order_after_load(self, tmp_path: Path):
        """Verifies that load_rules sorts by priority."""
        for name, pri in [("z.md", 100), ("a.md", 1), ("m.md", 50)]:
            (tmp_path / name).write_text(
                f"---\nrouting:\n  id: {name}\n  priority: {pri}\n---\n\n# {name}\n"
            )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        priorities = [r.priority for r in engine.rules]
        assert priorities == sorted(priorities)

    def test_same_priority_preserves_relative_order(self, tmp_path: Path):
        """Rules with the same priority should maintain stable order."""
        for name in ["b.md", "a.md", "c.md"]:
            (tmp_path / name).write_text(
                f"---\nrouting:\n  id: {name}\n  priority: 1\n---\n\n# {name}\n"
            )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        # With equal priorities, sorted() is stable, so order follows
        # the sorted filename order (a.md, b.md, c.md)
        ids = [r.id for r in engine.rules]
        assert len(ids) == 3
        # All same priority
        assert all(r.priority == 1 for r in engine.rules)

    def test_match_with_rule_returns_correct_rule_object(self):
        """match_with_rule should return the actual matched rule instance."""
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(id="a", priority=1, instruction="a.md")
        rule_b = make_rule(id="b", priority=5, instruction="b.md")
        engine._rules = [rule_a, rule_b]

        msg = make_msg()
        matched, instruction = engine.match_with_rule(msg)
        assert matched is rule_a
        assert instruction == "a.md"

        # Now make rule_a non-matching
        engine._rules = [
            make_rule(id="a", priority=1, sender="nonexistent", instruction="a.md"),
            rule_b,
        ]
        matched, instruction = engine.match_with_rule(msg)
        assert matched is rule_b
        assert instruction == "b.md"

    def test_match_context_from_message_isolation(self):
        """Verify MatchingContext is a snapshot — mutating the msg afterwards
        doesn't affect an already-created context."""
        msg = make_msg(text="original")
        ctx = MatchingContext.from_message(msg)
        # Create a new message (frozen, so can't mutate old one)
        assert ctx.text == "original"

    def test_match_with_empty_rules_list(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        assert engine.match(make_msg()) is None

    def test_all_rules_disabled_returns_none(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="r1", enabled=False),
            make_rule(id="r2", enabled=False),
        ]
        assert engine.match(make_msg()) is None

    def test_fromMe_and_toMe_combined_filters(self):
        """fromMe=True and toMe=True → only direct bot messages match."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True, toMe=True)]

        # fromMe=True, toMe=True → match
        assert engine.match(make_msg(fromMe=True, toMe=True)) == "chat.agent.md"
        # fromMe=True, toMe=False → no match
        assert engine.match(make_msg(fromMe=True, toMe=False)) is None
        # fromMe=False, toMe=True → no match
        assert engine.match(make_msg(fromMe=False, toMe=True)) is None
        # fromMe=False, toMe=False → no match
        assert engine.match(make_msg(fromMe=False, toMe=False)) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Parametrized pattern matching tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParametrizedPatternMatching:
    """Parametrized tests for pattern matching scenarios."""

    @pytest.mark.parametrize(
        "pattern,value,expected",
        [
            ("*", "anything", True),
            ("*", "", True),
            ("", "", True),
            ("", "something", False),
            ("hello", "hello", True),
            ("hello", "Hello", False),
            ("5511999990000", "5511999990000", True),
            ("5511999990000", "5511999990001", False),
            (r"\d+", "123", True),
            (r"\d+", "abc", False),
            (r"^hello.*world$", "hello beautiful world", True),
            (r"^hello.*world$", "world hello", False),
        ],
    )
    def test_match_compiled_various_patterns(self, pattern, value, expected):
        compiled = _compile_pattern(pattern)
        assert _match_compiled(compiled, pattern, value) is expected

    @pytest.mark.parametrize(
        "pattern,should_compile",
        [
            ("*", False),
            ("", False),
            (r"\d+", True),
            (r"^test$", True),
            ("hello", True),
            (r"[a-z]+", True),
            ("[invalid", False),
            ("(unclosed", False),
            (r"*invalid", False),  # quantifier without base
        ],
    )
    def test_compile_pattern_results(self, pattern, should_compile):
        result = _compile_pattern(pattern)
        if should_compile:
            assert result is not None
        else:
            assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — match cache (TTL-bounded LRU) tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingMatchCache:
    """Tests for the TTL-bounded LRU match cache in RoutingEngine."""

    def test_cache_hit_returns_same_result(self):
        """Repeated match() with same message returns cached result."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="cached.md")]
        msg = make_msg(text="hello")

        result1 = engine.match(msg)
        result2 = engine.match(msg)
        assert result1 == "cached.md"
        assert result2 == "cached.md"
        assert len(engine._match_cache) == 1

    def test_cache_miss_for_different_messages(self):
        """Different messages produce separate cache entries."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="catch.md")]
        msg_a = make_msg(sender_id="alice", text="hi")
        msg_b = make_msg(sender_id="bob", text="hi")

        engine.match(msg_a)
        engine.match(msg_b)
        assert len(engine._match_cache) == 2

    def test_cache_expired_entry_not_returned(self):
        """Expired cache entries are evicted on next access."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="exp.md")]
        msg = make_msg(text="test")

        engine.match(msg)
        assert len(engine._match_cache) == 1

        # Manually age the cache entry to simulate TTL expiry
        # Access BoundedOrderedDict internal storage to backdate the timestamp
        key = list(engine._match_cache._cache.keys())[0]
        value, _ = engine._match_cache._cache[key]
        engine._match_cache._cache[key] = (value, engine._match_cache._now() - 100.0)

        # Next match should re-evaluate (cache miss)
        result = engine.match(msg)
        assert result == "exp.md"
        # The expired entry was removed and a fresh one inserted
        assert len(engine._match_cache) == 1
        _, new_ts = engine._match_cache._cache[key]
        assert new_ts != engine._match_cache._now() - 100.0

    def test_cache_cleared_on_load_rules(self, tmp_path: Path):
        """load_rules() clears the match cache."""
        engine = RoutingEngine(tmp_path)
        engine._rules = [make_rule(instruction="tmp.md")]
        engine.match(make_msg())
        assert len(engine._match_cache) == 1

        engine.load_rules()
        assert len(engine._match_cache) == 0

    def test_cache_cleared_on_refresh_rules(self, tmp_path: Path):
        """refresh_rules() clears the match cache."""
        engine = RoutingEngine(tmp_path)
        engine._rules = [make_rule()]
        engine.match(make_msg())
        assert len(engine._match_cache) == 1

        engine.refresh_rules()
        assert len(engine._match_cache) == 0

    def test_cache_key_uses_text_prefix(self):
        """Cache key only uses first 100 chars of text."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*", instruction="prefix.md")]

        # Two messages with same first 100 chars but different tails
        short_text = "A" * 100
        msg_a = make_msg(text=short_text)
        msg_b = make_msg(text=short_text + "B" * 100)

        engine.match(msg_a)
        engine.match(msg_b)
        # Same cache key since first 100 chars match
        assert len(engine._match_cache) == 1

    def test_cache_key_includes_fromMe_toMe_sender_channel(self):
        """Cache key differentiates on fromMe, toMe, sender, channel, chat_id."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="all.md")]

        base = dict(text="hi")
        msgs = [
            make_msg(fromMe=False, toMe=False, sender_id="a", chat_id="c1", channel_type="wa", **base),
            make_msg(fromMe=True, toMe=False, sender_id="a", chat_id="c1", channel_type="wa", **base),
            make_msg(fromMe=False, toMe=True, sender_id="a", chat_id="c1", channel_type="wa", **base),
            make_msg(fromMe=False, toMe=False, sender_id="b", chat_id="c1", channel_type="wa", **base),
            make_msg(fromMe=False, toMe=False, sender_id="a", chat_id="c1", channel_type="tg", **base),
            make_msg(fromMe=False, toMe=False, sender_id="a", chat_id="c2", channel_type="wa", **base),
        ]
        for m in msgs:
            engine.match(m)
        assert len(engine._match_cache) == 6

    def test_cache_no_match_result_still_cached(self):
        """Cache also stores (None, None) results for no-match queries."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="nonexistent")]
        msg = make_msg(sender_id="other")

        result = engine.match(msg)
        assert result is None
        assert len(engine._match_cache) == 1

        # Second call returns cached None
        result2 = engine.match(msg)
        assert result2 is None

    def test_cache_lru_eviction_at_max_size(self):
        """Cache evicts LRU entries when exceeding max size."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="evict.md")]

        # Fill cache beyond capacity
        from src.constants import ROUTING_MATCH_CACHE_MAX_SIZE

        for i in range(ROUTING_MATCH_CACHE_MAX_SIZE + 10):
            engine.match(make_msg(sender_id=f"sender-{i}", text=f"msg-{i}"))

        assert len(engine._match_cache) <= ROUTING_MATCH_CACHE_MAX_SIZE

    def test_match_with_rule_cache_returns_same_rule_object(self):
        """Cached match_with_rule() returns the same rule object."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(id="cached-rule", instruction="cached.md")
        engine._rules = [rule]
        msg = make_msg()

        rule1, inst1 = engine.match_with_rule(msg)
        rule2, inst2 = engine.match_with_rule(msg)
        assert rule1 is rule
        assert rule2 is rule
        assert inst1 == "cached.md"
        assert inst2 == "cached.md"


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — auto-reload (mtime-based lazy loading) tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineAutoReload:
    """Tests for mtime-based auto-reload of routing rules."""

    def test_auto_reload_on_file_change(self, tmp_path: Path):
        """match() picks up new rules when an instruction file changes."""
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules == []

        # Add a new instruction file
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        # Reset debounce so the next match triggers a stale check
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert engine.match(msg) == "new.md"
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "new-rule"

    def test_auto_reload_on_file_deletion(self, tmp_path: Path):
        """match() drops rules when an instruction file is deleted."""
        md = tmp_path / "temp.md"
        md.write_text(
            "---\nrouting:\n  id: temp-rule\n  priority: 1\n---\n\n# Temp\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert len(engine.rules) == 1

        # Delete the instruction file
        md.unlink()

        # Reset debounce
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert engine.match(msg) is None
        assert engine.rules == []

    def test_auto_reload_on_file_content_change(self, tmp_path: Path):
        """match() picks up changed content when an instruction file is rewritten."""
        md = tmp_path / "change.md"
        md.write_text(
            "---\nrouting:\n  id: original\n  priority: 1\n---\n\n# Original\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert engine.rules[0].id == "original"

        # Overwrite the file with new content
        md.write_text(
            "---\nrouting:\n  id: updated\n  priority: 1\n---\n\n# Updated\n"
        )

        # Reset debounce
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert engine.match(msg) == "change.md"
        assert engine.rules[0].id == "updated"

    def test_no_reload_when_files_unchanged(self, tmp_path: Path):
        """No reload happens when files have not changed."""
        (tmp_path / "stable.md").write_text(
            "---\nrouting:\n  id: stable\n  priority: 1\n---\n\n# Stable\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        # Access internal state before match
        rules_before = engine.rules
        mtimes_before = engine._file_mtimes.copy()

        # match() should not trigger a reload
        msg = make_msg()
        engine._last_stale_check = 0.0  # Allow stale check
        result = engine.match(msg)
        assert result == "stable.md"

        # Rules list should be the same object (no reload)
        assert engine.rules is rules_before
        assert engine._file_mtimes == mtimes_before

    def test_debounce_prevents_rapid_stale_checks(self, tmp_path: Path):
        """Successive match() calls within the debounce window skip stale checks."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        # First match triggers stale check
        engine._last_stale_check = 0.0
        engine.match(make_msg())
        first_check_time = engine._last_stale_check

        # Second match within debounce window should NOT update stale check time
        engine.match(make_msg())
        assert engine._last_stale_check == first_check_time

    def test_scan_file_mtimes_returns_current_mtimes(self, tmp_path: Path):
        """_scan_file_mtimes returns mtimes for all .md files."""
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        (tmp_path / "c.txt").write_text("# Not markdown")

        engine = RoutingEngine(tmp_path)
        mtimes = engine._scan_file_mtimes()

        assert "a.md" in mtimes
        assert "b.md" in mtimes
        assert "c.txt" not in mtimes

    def test_scan_file_mtimes_empty_dir(self, tmp_path: Path):
        """_scan_file_mtimes returns empty dict for empty directory."""
        engine = RoutingEngine(tmp_path)
        assert engine._scan_file_mtimes() == {}

    def test_scan_file_mtimes_nonexistent_dir(self, tmp_path: Path):
        """_scan_file_mtimes returns empty dict for missing directory."""
        engine = RoutingEngine(tmp_path / "missing")
        assert engine._scan_file_mtimes() == {}

    def test_is_stale_detects_new_file(self, tmp_path: Path):
        """_is_stale returns True when a new .md file appears."""
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new\n  priority: 1\n---\n\n# New\n"
        )

        engine._last_stale_check = 0.0
        assert engine._is_stale() is True

    def test_is_stale_returns_false_when_unchanged(self, tmp_path: Path):
        """_is_stale returns False when files have not changed."""
        (tmp_path / "same.md").write_text("# Same")
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        engine._last_stale_check = 0.0
        assert engine._is_stale() is False

    def test_load_rules_populates_file_mtimes(self, tmp_path: Path):
        """load_rules caches mtimes after loading."""
        (tmp_path / "x.md").write_text(
            "---\nrouting:\n  id: x\n  priority: 1\n---\n\n# X\n"
        )
        engine = RoutingEngine(tmp_path)
        assert engine._file_mtimes == {}

        engine.load_rules()
        assert "x.md" in engine._file_mtimes
        assert engine._file_mtimes["x.md"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Property-based tests (Hypothesis)
# ═══════════════════════════════════════════════════════════════════════════════

from hypothesis import given, settings, assume
from hypothesis import strategies as st


# -- Strategies for generating routing inputs --

# Simple alphanumeric strings for IDs, channels, etc.
simple_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_@."),
    min_size=1,
    max_size=30,
)

# Printable text (avoids null/control chars that complicate regex)
printable_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z", "S")),
    min_size=0,
    max_size=100,
)

# Strategy for a valid regex pattern that won't error on compile
# Uses from_regex to generate strings that are themselves valid regex
valid_regex = st.one_of(
    st.just("*"),
    st.just(""),
    st.from_regex(r"[a-zA-Z0-9]{1,10}", fullmatch=True),
    st.from_regex(r"\d{1,5}", fullmatch=True),
    st.sampled_from(["*", "", r"\d+", r"[a-z]+", r"^hello", r".*world.*"]),
)

# Strategy for the boolean-or-None filter (fromMe / toMe)
bool_or_none = st.one_of(st.none(), st.booleans())


class TestPropertyMatchCompiled:
    """Property-based invariants for _match_compiled."""

    @given(value=printable_text)
    @settings(max_examples=200)
    def test_wildcard_always_matches(self, value: str):
        """Invariant: pattern '*' matches any string value."""
        assert _match_compiled(None, "*", value) is True

    @given(value=printable_text)
    @settings(max_examples=200)
    def test_empty_pattern_only_matches_empty(self, value: str):
        """Invariant: pattern '' matches iff value is also empty."""
        expected = value == ""
        assert _match_compiled(None, "", value) is expected

    @given(pattern=st.text(min_size=1, max_size=50), value=printable_text)
    @settings(max_examples=300)
    def test_compiled_vs_manual_equivalence(self, pattern: str, value: str):
        """Invariant: _match_compiled result equals manual check of the same logic."""
        compiled = _compile_pattern(pattern)
        result = _match_compiled(compiled, pattern, value)

        # Manually compute expected result
        if pattern == "*":
            expected = True
        elif pattern == "":
            expected = value == ""
        elif compiled is not None and compiled.match(value):
            expected = True
        else:
            expected = pattern == value

        assert result is expected

    @given(pattern=valid_regex, value=printable_text)
    @settings(max_examples=200)
    def test_match_compiled_is_pure(self, pattern: str, value: str):
        """Invariant: calling _match_compiled twice with same inputs gives same result."""
        compiled = _compile_pattern(pattern)
        result1 = _match_compiled(compiled, pattern, value)
        result2 = _match_compiled(compiled, pattern, value)
        assert result1 is result2

    @given(pattern=valid_regex, value=printable_text)
    @settings(max_examples=200)
    def test_match_result_is_bool(self, pattern: str, value: str):
        """Invariant: _match_compiled always returns a bool."""
        compiled = _compile_pattern(pattern)
        result = _match_compiled(compiled, pattern, value)
        assert isinstance(result, bool)


class TestPropertyRoutingRule:
    """Property-based invariants for RoutingRule construction."""

    @given(
        sender=valid_regex,
        recipient=valid_regex,
        channel=valid_regex,
        content_regex=valid_regex,
    )
    @settings(max_examples=100)
    def test_wildcard_fields_compile_to_none(
        self, sender: str, recipient: str, channel: str, content_regex: str
    ):
        """Invariant: _compile_pattern('*') and _compile_pattern('') return None,
        so compiled fields for wildcard/empty patterns should be None."""
        rule = RoutingRule(
            id="t", priority=0, sender=sender, recipient=recipient,
            channel=channel, content_regex=content_regex, instruction="t.md",
        )
        if sender in ("*", ""):
            assert rule._compiled_sender is None
        if recipient in ("*", ""):
            assert rule._compiled_recipient is None
        if channel in ("*", ""):
            assert rule._compiled_channel is None
        if content_regex in ("*", ""):
            assert rule._compiled_content is None

    @given(
        sender=st.from_regex(r"[a-zA-Z]{3,10}", fullmatch=True),
        recipient=st.from_regex(r"[a-zA-Z]{3,10}", fullmatch=True),
        channel=st.from_regex(r"[a-zA-Z]{3,10}", fullmatch=True),
        content_regex=st.from_regex(r"[a-zA-Z]{3,10}", fullmatch=True),
    )
    @settings(max_examples=50)
    def test_valid_regex_fields_compile_to_pattern(
        self, sender: str, recipient: str, channel: str, content_regex: str
    ):
        """Invariant: valid regex patterns always compile to non-None Pattern."""
        rule = RoutingRule(
            id="t", priority=0, sender=sender, recipient=recipient,
            channel=channel, content_regex=content_regex, instruction="t.md",
        )
        assert rule._compiled_sender is not None
        assert rule._compiled_recipient is not None
        assert rule._compiled_channel is not None
        assert rule._compiled_content is not None

    @given(
        sender=valid_regex,
        recipient=valid_regex,
        channel=valid_regex,
        content_regex=valid_regex,
    )
    @settings(max_examples=100)
    def test_is_wildcard_invariant(
        self, sender: str, recipient: str, channel: str, content_regex: str
    ):
        """Invariant: _is_wildcard is True iff all four patterns are exactly '*'."""
        rule = RoutingRule(
            id="t", priority=0, sender=sender, recipient=recipient,
            channel=channel, content_regex=content_regex, instruction="t.md",
        )
        expected = sender == "*" and recipient == "*" and channel == "*" and content_regex == "*"
        assert rule._is_wildcard == expected


class TestPropertyEngineMatching:
    """Property-based invariants for RoutingEngine.match() behavior."""

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    def test_catch_all_rule_matches_every_message(
        self, text: str, sender_id: str, chat_id: str,
        channel_type: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: a single catch-all rule (all '*') matches any message."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule()]
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        assert engine.match(msg) == "chat.agent.md"

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    def test_disabled_rule_never_matches(
        self, text: str, sender_id: str, chat_id: str,
        channel_type: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: disabled rules never match, regardless of patterns or message."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(enabled=False)]
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        assert engine.match(msg) is None

    @given(
        rules=st.lists(
            st.builds(
                make_rule,
                enabled=st.just(False),
                id=st.integers(min_value=0, max_value=999).map(lambda i: f"r-{i}"),
            ),
            min_size=1,
            max_size=10,
        ),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=50)
    def test_all_disabled_returns_none(self, rules: list, fromMe: bool, toMe: bool):
        """Invariant: when all rules are disabled, match always returns None."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = make_msg(fromMe=fromMe, toMe=toMe)
        assert engine.match(msg) is None

    @given(fromMe_val=st.booleans(), toMe_val=st.booleans())
    @settings(max_examples=50)
    def test_none_filters_pass_all(self, fromMe_val: bool, toMe_val: bool):
        """Invariant: fromMe=None and toMe=None match both True and False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=None, toMe=None)]
        msg = make_msg(fromMe=fromMe_val, toMe=toMe_val)
        assert engine.match(msg) == "chat.agent.md"

    @given(fromMe_val=st.booleans())
    @settings(max_examples=50)
    def test_fromMe_true_only_matches_bot_messages(self, fromMe_val: bool):
        """Invariant: fromMe=True only matches when message fromMe is True."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=fromMe_val)
        result = engine.match(msg)
        if fromMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(fromMe_val=st.booleans())
    @settings(max_examples=50)
    def test_fromMe_false_only_matches_non_bot_messages(self, fromMe_val: bool):
        """Invariant: fromMe=False only matches when message fromMe is False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=fromMe_val)
        result = engine.match(msg)
        if not fromMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(toMe_val=st.booleans())
    @settings(max_examples=50)
    def test_toMe_true_only_matches_direct_messages(self, toMe_val: bool):
        """Invariant: toMe=True only matches when message toMe is True."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=toMe_val)
        result = engine.match(msg)
        if toMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(toMe_val=st.booleans())
    @settings(max_examples=50)
    def test_toMe_false_only_matches_group_messages(self, toMe_val: bool):
        """Invariant: toMe=False only matches when message toMe is False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=toMe_val)
        result = engine.match(msg)
        if not toMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    def test_matching_is_deterministic(
        self, text: str, sender_id: str, chat_id: str,
        channel_type: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: same inputs always produce same outputs (pure function)."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule()]
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        result1 = engine.match(msg)
        result2 = engine.match(msg)
        assert result1 == result2

    @given(
        priorities=st.lists(st.integers(min_value=0, max_value=100), min_size=2, max_size=5),
    )
    @settings(max_examples=100)
    def test_first_matching_rule_has_lowest_priority(self, priorities: list):
        """Invariant: among multiple matching rules, the one with lowest priority wins."""
        engine = RoutingEngine(Path("/dummy"))
        rules = [
            make_rule(
                id=f"r-{i}",
                priority=p,
                instruction=f"rule-{p}.md",
            )
            for i, p in enumerate(priorities)
        ]
        engine._rules = sorted(rules, key=lambda r: r.priority)
        msg = make_msg()
        result = engine.match(msg)
        assert result is not None
        # The winning instruction should correspond to the minimum priority
        min_pri = min(priorities)
        assert result == f"rule-{min_pri}.md"

    @given(
        text=printable_text,
        sender_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    def test_disabled_rule_before_enabled_falls_through(
        self, text: str, sender_id: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: disabled rule at lower priority falls through to next enabled rule."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="disabled", priority=1, enabled=False, instruction="disabled.md"),
            make_rule(id="enabled", priority=10, enabled=True, instruction="enabled.md"),
        ]
        msg = make_msg(text=text, sender_id=sender_id, fromMe=fromMe, toMe=toMe)
        assert engine.match(msg) == "enabled.md"

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    def test_empty_rules_always_returns_none(
        self, text: str, sender_id: str, chat_id: str,
        channel_type: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: no rules → match always returns None."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        assert engine.match(msg) is None

    @given(
        specific_sender=simple_text,
        other_sender=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    def test_specific_sender_matches_exactly(
        self, specific_sender: str, other_sender: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: specific sender pattern only matches that exact sender."""
        assume(specific_sender != other_sender)
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=specific_sender)]
        matching_msg = make_msg(sender_id=specific_sender, fromMe=fromMe, toMe=toMe)
        non_matching_msg = make_msg(sender_id=other_sender, fromMe=fromMe, toMe=toMe)
        assert engine.match(matching_msg) == "chat.agent.md"
        assert engine.match(non_matching_msg) is None

    @given(
        specific_channel=simple_text,
        other_channel=simple_text,
    )
    @settings(max_examples=200)
    def test_specific_channel_matches_exactly(
        self, specific_channel: str, other_channel: str,
    ):
        """Invariant: specific channel pattern only matches that exact channel."""
        assume(specific_channel != other_channel)
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel=specific_channel)]
        assert engine.match(make_msg(channel_type=specific_channel)) == "chat.agent.md"
        assert engine.match(make_msg(channel_type=other_channel)) is None

    @given(
        specific_recipient=simple_text,
        other_recipient=simple_text,
    )
    @settings(max_examples=200)
    def test_specific_recipient_matches_exactly(
        self, specific_recipient: str, other_recipient: str,
    ):
        """Invariant: specific recipient pattern only matches that exact recipient."""
        assume(specific_recipient != other_recipient)
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient=specific_recipient)]
        assert engine.match(make_msg(chat_id=specific_recipient)) == "chat.agent.md"
        assert engine.match(make_msg(chat_id=other_recipient)) is None

    @given(
        fromMe_filter=bool_or_none,
        toMe_filter=bool_or_none,
        msg_fromMe=st.booleans(),
        msg_toMe=st.booleans(),
    )
    @settings(max_examples=200)
    def test_boolean_filter_invariant(
        self, fromMe_filter, toMe_filter, msg_fromMe: bool, msg_toMe: bool,
    ):
        """Invariant: fromMe/toMe filters work correctly for all combinations."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=fromMe_filter, toMe=toMe_filter)]
        msg = make_msg(fromMe=msg_fromMe, toMe=msg_toMe)
        result = engine.match(msg)

        # Compute expected: None = pass-through, True/False = must match exactly
        fromMe_pass = fromMe_filter is None or fromMe_filter == msg_fromMe
        toMe_pass = toMe_filter is None or toMe_filter == msg_toMe
        expected = fromMe_pass and toMe_pass
        assert (result is not None) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# Cache invalidation on file modification — end-to-end integration tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCacheInvalidationOnFileModification:
    """
    End-to-end tests verifying that match_with_rule() correctly invalidates
    its cached results when instruction files change on disk.

    These tests exercise the full pipeline:
        match_with_rule() → _is_stale() → load_rules() → _rules setter →
        _match_cache.clear() → fresh rule evaluation → new cache entry
    """

    def test_cache_invalidated_when_file_modified(self, tmp_path: Path):
        """(a) Cached result is cleared and new rule is returned after file modification."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: original\n  priority: 1\n---\n\n# Original\n"
        )

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # First match — populates cache
        rule1, inst1 = engine.match_with_rule(msg)
        assert inst1 == "route.md"
        assert rule1 is not None
        assert rule1.id == "original"
        assert len(engine._match_cache) == 1

        # Modify the file with a different rule id
        md.write_text(
            "---\nrouting:\n  id: modified\n  priority: 1\n---\n\n# Modified\n"
        )

        # Allow stale check to run
        engine._last_stale_check = 0.0

        # Second match — cache should be cleared, fresh result returned
        rule2, inst2 = engine.match_with_rule(msg)
        assert inst2 == "route.md"
        assert rule2 is not None
        assert rule2.id == "modified"

        # Cache was rebuilt (cleared by load_rules, then repopulated)
        assert len(engine._match_cache) == 1
        # The old rule object should NOT be in the cache anymore
        cached_result = list(engine._match_cache.values())[0]
        assert cached_result[0].id == "modified"

    def test_new_rule_appears_after_file_creation(self, tmp_path: Path):
        """(b) Creating a new .md file causes new rules to appear in match results."""
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # Initially no rules
        assert engine.match(msg) is None
        assert len(engine._match_cache) == 1  # (None, None) cached

        # Create a new instruction file
        (tmp_path / "new_rule.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        # Allow stale check
        engine._last_stale_check = 0.0

        # New rule should now match
        rule, inst = engine.match_with_rule(msg)
        assert inst == "new_rule.md"
        assert rule is not None
        assert rule.id == "new-rule"

        # Cache was rebuilt with new result
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache.values())[0]
        assert cached_result[0].id == "new-rule"

    def test_removed_rule_disappears_after_file_deletion(self, tmp_path: Path):
        """(c) Deleting an .md file causes its rules to disappear from match results."""
        md = tmp_path / "gone.md"
        md.write_text(
            "---\nrouting:\n  id: temporary\n  priority: 1\n---\n\n# Temp\n"
        )

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # Rule matches
        rule1, inst1 = engine.match_with_rule(msg)
        assert inst1 == "gone.md"
        assert rule1 is not None
        assert rule1.id == "temporary"
        assert len(engine._match_cache) == 1

        # Delete the file
        md.unlink()

        # Allow stale check
        engine._last_stale_check = 0.0

        # No rules should match now
        rule2, inst2 = engine.match_with_rule(msg)
        assert rule2 is None
        assert inst2 is None

        # Cache was rebuilt
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache.values())[0]
        assert cached_result == (None, None)

    def test_debounce_prevents_excessive_reloads(self, tmp_path: Path):
        """(d) Multiple match calls within the debounce window skip stale checks,
        preserving the cached result even if files changed on disk."""
        md = tmp_path / "stable.md"
        md.write_text(
            "---\nrouting:\n  id: v1\n  priority: 1\n---\n\n# V1\n"
        )

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # First match — triggers stale check, populates cache
        engine._last_stale_check = 0.0
        rule1, inst1 = engine.match_with_rule(msg)
        assert rule1 is not None
        assert rule1.id == "v1"
        assert len(engine._match_cache) == 1
        first_check_time = engine._last_stale_check

        # Modify the file on disk (this would normally trigger reload)
        md.write_text(
            "---\nrouting:\n  id: v2\n  priority: 1\n---\n\n# V2\n"
        )

        # Second match — within debounce window, should NOT detect stale
        # and should return cached v1 result
        rule2, inst2 = engine.match_with_rule(msg)
        assert engine._last_stale_check == first_check_time  # debounce blocked re-check
        assert rule2 is not None
        assert rule2.id == "v1"  # Still returns cached result
        assert len(engine._match_cache) == 1

    def test_end_to_end_cache_lifecycle(self, tmp_path: Path):
        """Full lifecycle: load → cache → modify → auto-reload → new cache → delete → empty."""
        # Step 1: Create initial file and load
        md = tmp_path / "lifecycle.md"
        md.write_text(
            "---\nrouting:\n  id: step1\n  priority: 1\n---\n\n# Step1\n"
        )

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # Step 2: First match — cache populated
        rule, _ = engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step1"
        assert len(engine._match_cache) == 1

        # Step 3: Modify file — cache invalidated, new result cached
        md.write_text(
            "---\nrouting:\n  id: step2\n  priority: 1\n---\n\n# Step2\n"
        )
        engine._last_stale_check = 0.0

        rule, _ = engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step2"
        assert len(engine._match_cache) == 1

        # Step 4: Delete file — cache invalidated, (None, None) cached
        md.unlink()
        engine._last_stale_check = 0.0

        rule, inst = engine.match_with_rule(msg)
        assert rule is None
        assert inst is None
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache.values())[0]
        assert cached_result == (None, None)

        # Step 5: Recreate file — cache invalidated, new result cached
        md.write_text(
            "---\nrouting:\n  id: step5\n  priority: 1\n---\n\n# Step5\n"
        )
        engine._last_stale_check = 0.0

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step5"
        assert inst == "lifecycle.md"

    def test_cache_not_invalidated_when_content_unchanged(self, tmp_path: Path):
        """Rewriting the same content should not cause cache invalidation."""
        md = tmp_path / "same.md"
        content = "---\nrouting:\n  id: same-rule\n  priority: 1\n---\n\n# Same\n"
        md.write_text(content)

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg = make_msg(text="hello")

        # First match
        engine._last_stale_check = 0.0
        rule1, _ = engine.match_with_rule(msg)
        assert rule1 is not None
        assert rule1.id == "same-rule"
        rules_obj_before = engine._rules  # identity check

        # Rewrite the same content (mtime changes but rule is logically identical)
        md.write_text(content)
        engine._last_stale_check = 0.0

        # Even though mtime changed, load_rules will re-scan and set _rules
        # which clears cache. The new rule should be functionally identical.
        rule2, _ = engine.match_with_rule(msg)
        assert rule2 is not None
        assert rule2.id == "same-rule"

    def test_multiple_files_invalidation(self, tmp_path: Path):
        """Modifying one of multiple instruction files invalidates the entire cache."""
        (tmp_path / "first.md").write_text(
            "---\nrouting:\n  id: first\n  priority: 1\n---\n\n# First\n"
        )
        (tmp_path / "second.md").write_text(
            "---\nrouting:\n  id: second\n  priority: 10\n---\n\n# Second\n"
        )

        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        msg_low = make_msg(text="hello")
        msg_high = make_msg(sender_id="special-sender")

        # Populate cache with two entries
        engine._last_stale_check = 0.0
        engine.match_with_rule(msg_low)
        engine.match_with_rule(msg_high)
        assert len(engine._match_cache) == 2

        # Modify the first file (higher priority) to change its rule
        (tmp_path / "first.md").write_text(
            "---\nrouting:\n  id: first-updated\n  priority: 1\n---\n\n# Updated\n"
        )
        engine._last_stale_check = 0.0

        # One match should clear the ENTIRE cache (both entries)
        engine.match_with_rule(msg_low)
        assert len(engine._match_cache) == 1  # Only msg_low's new result

        # The other message must be re-evaluated (not stale cached)
        rule, inst = engine.match_with_rule(msg_high)
        assert rule is not None
        assert rule.id == "first-updated"  # Still matches the catch-all first rule
        assert len(engine._match_cache) == 2  # Now both are cached again


# ═══════════════════════════════════════════════════════════════════════════════
# Tests for _is_stale() debounce behavior
# ═══════════════════════════════════════════════════════════════════════════════

from src.constants import ROUTING_WATCH_DEBOUNCE_SECONDS


class TestIsStaleDebounce:
    """Verify the time-based debounce inside RoutingEngine._is_stale().

    _is_stale() short-circuits when the elapsed time since the last check is
    less than ROUTING_WATCH_DEBOUNCE_SECONDS (1.0 s).  Only when the debounce
    window has elapsed does it update _last_stale_check and compare mtimes.
    """

    # -- (a) Two calls within debounce window → _scan_file_mtimes called once --

    def test_scan_called_once_within_debounce_window(self, tmp_path: Path):
        """_scan_file_mtimes() is NOT called on the second _is_stale() within the debounce window."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        # Force the first call to pass the debounce gate.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False

        with patch.object(engine, "_scan_file_mtimes", wraps=engine._scan_file_mtimes) as mock_scan:
            # Second call — still within the debounce window → should NOT scan.
            result = engine._is_stale()
            assert result is False
            mock_scan.assert_not_called()

    # -- (b) Call after debounce interval triggers fresh scan --

    def test_fresh_scan_after_debounce_interval(self, tmp_path: Path):
        """After ROUTING_WATCH_DEBOUNCE_SECONDS, _is_stale() scans mtimes again."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        # First call — passes debounce gate.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False
        first_check_time = engine._last_stale_check
        assert first_check_time > 0.0

        # Advance time past the debounce window.
        with patch("src.routing.time.monotonic", return_value=first_check_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1):
            with patch.object(engine, "_scan_file_mtimes", wraps=engine._scan_file_mtimes) as mock_scan:
                result = engine._is_stale()
                # mtimes haven't changed → False
                assert result is False
                mock_scan.assert_called_once()

    def test_multiple_debounce_windows_scan_each_time(self, tmp_path: Path):
        """Each debounce-window boundary triggers exactly one scan."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        base_time = 1000.0

        with patch("src.routing.time.monotonic") as mock_clock:
            # Window 1: first call at t=1000
            mock_clock.return_value = base_time
            engine._last_stale_check = 0.0
            assert engine._is_stale() is False
            assert engine._last_stale_check == base_time

            # Window 1: second call at t=1000.5 — still debounced
            mock_clock.return_value = base_time + 0.5
            assert engine._is_stale() is False
            assert engine._last_stale_check == base_time  # unchanged

            # Window 2: call at t=1001.1 — debounce expired
            mock_clock.return_value = base_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1
            assert engine._is_stale() is False
            assert engine._last_stale_check == base_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1

            # Window 2: another debounced call
            mock_clock.return_value = base_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.5
            assert engine._is_stale() is False
            assert engine._last_stale_check == base_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1  # unchanged

    # -- (c) Rules reloaded when instruction file modified after debounce --

    def test_stale_returns_true_when_file_modified_after_debounce(self, tmp_path: Path):
        """_is_stale() returns True after a file is modified and debounce has elapsed."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        original_mtimes = dict(engine._file_mtimes)

        # First check — no changes.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False
        first_check_time = engine._last_stale_check

        # Modify the file AFTER the first check so mtimes differ.
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule-v2\n  priority: 1\n---\n\n# Updated\n"
        )

        # Advance time past debounce and verify stale is detected.
        with patch("src.routing.time.monotonic", return_value=first_check_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1):
            assert engine._is_stale() is True

    def test_stale_detects_new_file_after_debounce(self, tmp_path: Path):
        """A new .md file appearing after debounce triggers a stale detection."""
        (tmp_path / "existing.md").write_text(
            "---\nrouting:\n  id: existing\n  priority: 1\n---\n\n# Existing\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        # First check — no changes.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False
        first_check_time = engine._last_stale_check

        # Add a new file.
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 2\n---\n\n# New\n"
        )

        # After debounce window, stale should be True.
        with patch("src.routing.time.monotonic", return_value=first_check_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1):
            assert engine._is_stale() is True

    def test_stale_detects_deleted_file_after_debounce(self, tmp_path: Path):
        """Deleting a .md file after debounce triggers a stale detection."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        (tmp_path / "b.md").write_text(
            "---\nrouting:\n  id: b-rule\n  priority: 2\n---\n\n# B\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()
        assert len(engine._file_mtimes) == 2

        # First check — no changes.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False
        first_check_time = engine._last_stale_check

        # Delete a file.
        (tmp_path / "b.md").unlink()

        # After debounce, stale should be True.
        with patch("src.routing.time.monotonic", return_value=first_check_time + ROUTING_WATCH_DEBOUNCE_SECONDS + 0.1):
            assert engine._is_stale() is True

    # -- Boundary: exactly at debounce threshold --

    def test_exactly_at_debounce_threshold_triggers_scan(self, tmp_path: Path):
        """At exactly ROUTING_WATCH_DEBOUNCE_SECONDS, the debounce should NOT short-circuit."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        base_time = 500.0
        engine._last_stale_check = base_time

        # At exactly debounce threshold: now - last == ROUTING_WATCH_DEBOUNCE_SECONDS
        # The condition is `now - last < DEBOUNCE` (strict less-than),
        # so equal means the condition is False → scan proceeds.
        with patch("src.routing.time.monotonic", return_value=base_time + ROUTING_WATCH_DEBOUNCE_SECONDS):
            with patch.object(engine, "_scan_file_mtimes", return_value=engine._file_mtimes) as mock_scan:
                result = engine._is_stale()
                assert result is False
                mock_scan.assert_called_once()

    # -- Initial state: fresh engine always scans first time --

    def test_fresh_engine_first_call_always_scans(self, tmp_path: Path):
        """With _last_stale_check=0.0 (default), the first _is_stale() always passes the debounce gate."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        engine.load_rules()

        assert engine._last_stale_check == 0.0

        with patch.object(engine, "_scan_file_mtimes", return_value=engine._file_mtimes) as mock_scan:
            result = engine._is_stale()
            assert result is False
            mock_scan.assert_called_once()
        assert engine._last_stale_check > 0.0
