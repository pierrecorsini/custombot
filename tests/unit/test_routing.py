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
from dataclasses import dataclass, FrozenInstanceError
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.channels.base import IncomingMessage
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
    channel_type: str = "whatsapp",
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
            channel_type="whatsapp",
            text="Hello world",
            fromMe=True,
            toMe=False,
        )
        assert ctx.sender_id == "5511999990000"
        assert ctx.chat_id == "chat-001"
        assert ctx.channel_type == "whatsapp"
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
        assert ctx.channel_type == "telegram"
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
        ctx = MatchingContext(
            sender_id="s", chat_id="c", channel_type="ch", text=long_text
        )
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
        compiled = _compile_pattern(r"^55\d{10}$")
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
        # Attempting to assign should not affect internal state
        # (the property returns a reference, but _rules is private)
        original = engine.rules
        assert original is engine._rules

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
        msg = make_msg(channel_type="whatsapp")
        assert engine.match(msg) == "chat.agent.md"

    def test_specific_channel_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="telegram")]
        msg = make_msg(channel_type="whatsapp")
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
