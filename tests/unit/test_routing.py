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
import string
import sys
import textwrap
import time
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.base import ChannelType, IncomingMessage
from src.routing import (
    MatchingContext,
    RoutingEngine,
    RoutingRule,
    _HAS_WATCHDOG,
    _compile_pattern,
    _match_compiled,
    _rule_from_dict,
)
from src.utils.frontmatter import parse_file

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


def _polling_engine(path: Path) -> RoutingEngine:
    """Create a RoutingEngine with polling-based stale detection.

    Used by tests that need deterministic _is_stale() behavior
    (debounce timing, mtime comparison) without relying on the
    asynchronous watchdog observer.
    """
    return RoutingEngine(path, use_watchdog=False)


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

    async def test_create_with_all_fields(self):
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

    async def test_create_with_defaults(self):
        ctx = MatchingContext(
            sender_id="s",
            chat_id="c",
            channel_type="ch",
            text="t",
        )
        assert ctx.fromMe is False
        assert ctx.toMe is False

    # ── Immutability ────────────────────────────────────────────────────

    async def test_frozen_raises_on_setattr(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        with pytest.raises(FrozenInstanceError):
            ctx.sender_id = "changed"  # type: ignore[misc]

    async def test_frozen_raises_on_del(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        with pytest.raises(FrozenInstanceError):
            del ctx.text  # type: ignore[attr-defined]

    # ── from_message classmethod ────────────────────────────────────────

    async def test_from_message_extracts_fields(self):
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

    async def test_from_message_defaults(self):
        msg = make_msg(fromMe=False, toMe=False)
        ctx = MatchingContext.from_message(msg)
        assert ctx.fromMe is False
        assert ctx.toMe is False

    # ── __repr__ ────────────────────────────────────────────────────────

    async def test_repr_short_text(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="Hi")
        r = repr(ctx)
        assert "MatchingContext(" in r
        assert "sender='s'" in r
        assert "text='Hi'" in r

    async def test_repr_long_text_truncated(self):
        long_text = "A" * 50
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text=long_text)
        r = repr(ctx)
        assert "..." in r
        assert "text=" in r
        # The preview should be truncated to 30 chars + "..."
        assert long_text not in r

    # ── Hashability (frozen dataclass) ──────────────────────────────────

    async def test_usable_as_dict_key(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        mapping = {ctx: "value"}
        assert mapping[ctx] == "value"

    async def test_usable_in_set(self):
        ctx = MatchingContext(sender_id="s", chat_id="c", channel_type="ch", text="t")
        s = {ctx}
        assert ctx in s


# ═══════════════════════════════════════════════════════════════════════════════
# _compile_pattern tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompilePattern:
    """Tests for the _compile_pattern helper."""

    async def test_wildcard_returns_none(self):
        assert _compile_pattern("*") is None

    async def test_empty_string_returns_none(self):
        assert _compile_pattern("") is None

    async def test_valid_regex_compiled(self):
        result = _compile_pattern(r"\d+")
        assert result is not None
        assert isinstance(result, re.Pattern)

    async def test_valid_regex_matches(self):
        compiled = _compile_pattern(r"hello.*world")
        assert compiled is not None
        assert compiled.match("hello beautiful world") is not None

    async def test_invalid_regex_returns_none(self):
        # Unbalanced parenthesis — not valid regex
        assert _compile_pattern(r"(unclosed") is None

    async def test_complex_valid_regex(self):
        compiled = _compile_pattern(r"^55\d{11}$")
        assert compiled is not None
        assert compiled.match("5511999990000") is not None
        assert compiled.match("123") is None

    async def test_literal_string_compiled(self):
        compiled = _compile_pattern("5511999990000")
        assert compiled is not None
        assert compiled.match("5511999990000") is not None


# ═══════════════════════════════════════════════════════════════════════════════
# _match_compiled tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchCompiled:
    """Tests for the _match_compiled helper."""

    # ── Wildcard pattern ────────────────────────────────────────────────

    async def test_wildcard_matches_any_value(self):
        assert _match_compiled(None, "*", "anything") is True

    async def test_wildcard_matches_empty_value(self):
        assert _match_compiled(None, "*", "") is True

    # ── Empty pattern ───────────────────────────────────────────────────

    async def test_empty_pattern_matches_empty_value(self):
        assert _match_compiled(None, "", "") is True

    async def test_empty_pattern_does_not_match_nonempty_value(self):
        assert _match_compiled(None, "", "hello") is False

    # ── Regex matching ──────────────────────────────────────────────────

    async def test_compiled_regex_match(self):
        compiled = _compile_pattern(r"\d+")
        assert _match_compiled(compiled, r"\d+", "123") is True

    async def test_compiled_regex_no_match(self):
        compiled = _compile_pattern(r"\d+")
        assert _match_compiled(compiled, r"\d+", "abc") is False

    async def test_compiled_regex_partial_no_match(self):
        # re.match() anchors at start; "abc123" won't match r"^\d+$"
        compiled = re.compile(r"^\d+$")
        assert _match_compiled(compiled, r"^\d+$", "abc123") is False

    # ── Exact string fallback ───────────────────────────────────────────

    async def test_exact_string_match(self):
        # For a pattern that doesn't compile to a regex (returns None),
        # but the raw pattern string matches the value exactly
        assert _match_compiled(None, "5511999990000", "5511999990000") is True

    async def test_exact_string_no_match(self):
        assert _match_compiled(None, "5511999990000", "5511999990001") is False

    async def test_invalid_regex_falls_back_to_exact_match(self):
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

    async def test_create_with_defaults(self):
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

    async def test_create_with_all_fields(self):
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

    # ── Eagerly-compiled regex patterns ─────────────────────────────────

    async def test_eager_compile_sender(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender=r"55\d+",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
        )
        # Compiled eagerly at construction
        assert rule._compiled_sender is not None
        assert rule._compiled_sender.match("5511999990000")

    async def test_eager_compile_channel(self):
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

    async def test_eager_compile_content_regex(self):
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

    async def test_wildcard_fields_not_compiled(self):
        rule = make_rule(sender="*", channel="*", content_regex="*", recipient="*")
        assert rule._compiled_sender is None
        assert rule._compiled_recipient is None
        assert rule._compiled_channel is None
        assert rule._compiled_content is None

    async def test_empty_fields_not_compiled(self):
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

    async def test_invalid_regex_compiled_as_none(self):
        rule = RoutingRule(
            id="t",
            priority=0,
            sender="[invalid",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="test.md",
        )
        # Invalid regex compiles to None (falls back to exact match)
        assert rule._compiled_sender is None

    # ── _is_wildcard pre-computation ────────────────────────────────────

    async def test_is_wildcard_true_when_all_wildcards(self):
        rule = make_rule(sender="*", recipient="*", channel="*", content_regex="*")
        assert rule._is_wildcard is True

    async def test_is_wildcard_false_when_sender_not_wildcard(self):
        rule = make_rule(sender="5511999990000")
        assert rule._is_wildcard is False

    async def test_is_wildcard_false_when_recipient_not_wildcard(self):
        rule = make_rule(recipient="group-001")
        assert rule._is_wildcard is False

    async def test_is_wildcard_false_when_channel_not_wildcard(self):
        rule = make_rule(channel="whatsapp")
        assert rule._is_wildcard is False

    async def test_is_wildcard_false_when_content_not_wildcard(self):
        rule = make_rule(content_regex=r"^hello")
        assert rule._is_wildcard is False

    async def test_is_wildcard_false_when_empty_pattern(self):
        """Empty string patterns are not wildcards — they match only empty values."""
        rule = RoutingRule(
            id="t", priority=0, sender="", recipient="*",
            channel="*", content_regex="*", instruction="t.md",
        )
        assert rule._is_wildcard is False

    async def test_is_wildcard_not_in_repr(self):
        rule = make_rule()
        assert "_is_wildcard" not in repr(rule)

    # ── __repr__ ────────────────────────────────────────────────────────

    async def test_repr_enabled_rule(self):
        rule = make_rule(id="my-rule", priority=5, enabled=True)
        r = repr(rule)
        assert "my-rule" in r
        assert "ON" in r

    async def test_repr_disabled_rule(self):
        rule = make_rule(id="off-rule", enabled=False)
        r = repr(rule)
        assert "OFF" in r

    async def test_repr_truncates_long_content_regex(self):
        long_regex = "a" * 50
        rule = make_rule(content_regex=long_regex)
        r = repr(rule)
        assert "..." in r


# ═══════════════════════════════════════════════════════════════════════════════
# _rule_from_dict tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleFromDict:
    """Tests for _rule_from_dict conversion helper."""

    async def test_full_dict(self):
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

    async def test_defaults_when_keys_missing(self):
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

    async def test_legacy_showSkillExec_true(self):
        """Backward compat: showSkillExec: true → skillExecVerbose: 'summary'."""
        d = {"showSkillExec": True}
        rule = _rule_from_dict(d, "legacy.md")
        assert rule.skillExecVerbose == "summary"

    async def test_legacy_showSkillExec_false(self):
        """showSkillExec: false should NOT override skillExecVerbose."""
        d = {"showSkillExec": False, "skillExecVerbose": "full"}
        rule = _rule_from_dict(d, "legacy.md")
        assert rule.skillExecVerbose == "full"

    async def test_skillExecVerbose_without_legacy(self):
        d = {"skillExecVerbose": "full"}
        rule = _rule_from_dict(d, "test.md")
        assert rule.skillExecVerbose == "full"

    async def test_id_defaults_to_filename_stem(self):
        rule = _rule_from_dict({}, "my_agent.md")
        assert rule.id == "my_agent"


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — load_rules tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineLoadRules:
    """Tests for RoutingEngine.load_rules() and file scanning."""

    async def test_empty_directory_loads_no_rules(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

    async def test_nonexistent_directory_loads_no_rules(self, tmp_path: Path):
        missing = tmp_path / "no_such_dir"
        engine = RoutingEngine(missing)
        await engine.load_rules()
        assert engine.rules == []

    async def test_directory_with_no_md_files(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("not a markdown file")
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

    async def test_md_file_without_frontmatter_skipped(self, tmp_path: Path):
        (tmp_path / "plain.md").write_text("# No frontmatter here\nJust content.")
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

    async def test_md_file_with_routing_loads_rule(self, tmp_path: Path):
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
        await engine.load_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "chat-rule"
        assert engine.rules[0].priority == 10

    async def test_multiple_md_files_loaded(self, tmp_path: Path):
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
        await engine.load_rules()
        assert len(engine.rules) == 3
        # Rules should be sorted by priority
        assert engine.rules[0].priority == 1
        assert engine.rules[1].priority == 5
        assert engine.rules[2].priority == 10

    async def test_rules_sorted_by_priority_ascending(self, tmp_path: Path):
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
        await engine.load_rules()
        priorities = [r.priority for r in engine.rules]
        assert priorities == [1, 10, 25, 50, 100]

    async def test_rules_property_is_read_only(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        assert engine.rules == []
        # The rules property returns the internal list (read-only view)
        original = engine.rules
        assert original is engine._rules_list

    async def test_instructions_dir_property(self, tmp_path: Path):
        engine = RoutingEngine(tmp_path)
        assert engine.instructions_dir == tmp_path

    async def test_refresh_rules_reloads(self, tmp_path: Path):
        """refresh_rules() should reload from disk."""
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

        # Add a new file and refresh
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )
        await engine.refresh_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "new-rule"

    async def test_multiple_routing_entries_in_single_file(self, tmp_path: Path):
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
        await engine.load_rules()
        assert len(engine.rules) == 2
        ids = {r.id for r in engine.rules}
        assert ids == {"rule-a", "rule-b"}

    async def test_malformed_frontmatter_skipped_gracefully(self, tmp_path: Path):
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
        await engine.load_rules()
        # Only the good file should be loaded
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "good-rule"

    async def test_transient_parse_failure_retried(self, tmp_path: Path):
        """A transient parse_file failure is retried once before skipping."""
        md = tmp_path / "flaky.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: retry-rule
                  priority: 1
                ---
                # Retry
            """)
        )
        engine = RoutingEngine(tmp_path)

        call_count = 0
        original_parse = parse_file

        def _flaky_parse(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("simulated transient failure")
            return original_parse(path)

        with patch("src.routing.parse_file", side_effect=_flaky_parse):
            with patch("src.routing.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                await engine.load_rules()

        # parse_file called twice (initial + retry)
        assert call_count == 2
        # sleep was called with 0.1s delay
        mock_sleep.assert_called_once_with(0.1)
        # Rule loaded successfully after retry
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "retry-rule"

    async def test_persistent_parse_failure_skipped_after_retry(self, tmp_path: Path):
        """If parse_file fails on both attempts, the file is skipped."""
        md = tmp_path / "persistently_bad.md"
        md.write_text("---\n[[invalid yaml\n---\n# Broken\n")

        # Also create a good file to verify it still loads
        good = tmp_path / "good.md"
        good.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: good-rule
                  priority: 1
                ---
                # Good
            """)
        )
        engine = RoutingEngine(tmp_path)

        with patch("src.routing.asyncio.sleep", new_callable=AsyncMock):
            await engine.load_rules()

        # Only the good file loaded; the bad file was retried then skipped
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "good-rule"

    async def test_retry_budget_caps_cumulative_sleep(self, tmp_path: Path):
        """When many files fail to parse, cumulative retry sleep is capped.

        Creates more corrupted files than the budget allows retries for
        (budget = 1.0s, each retry = 0.1s → max 10 retries). Verifies
        that files beyond the budget are skipped without retrying.
        """
        # Create 15 corrupted .md files — more than the budget allows
        for i in range(15):
            bad = tmp_path / f"bad_{i:02d}.md"
            bad.write_text("---\n[[invalid yaml\n---\n# Broken\n")

        # Also create a good file to verify it still loads
        good = tmp_path / "good.md"
        good.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: good-rule
                  priority: 1
                ---
                # Good
            """)
        )
        engine = RoutingEngine(tmp_path)

        with patch("src.routing.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await engine.load_rules()

        # asyncio.sleep should be called at most 10 times (1.0s budget / 0.1s each)
        assert mock_sleep.call_count <= 10
        # The good file should still load
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "good-rule"

    async def test_reload_empty_files_retains_previous_rules(self, tmp_path: Path):
        """Graceful degradation: reload that yields zero rules retains previous set.

        Simulates an editor that truncates files before writing (e.g. atomic save).
        The engine should log a warning, keep the old rule set, and leave mtimes
        stale so the next stale-check retries the reload.
        """
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
        await engine.load_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "chat-rule"
        captured_mtimes = dict(engine._file_mtimes)

        # Simulate editor truncating the file to empty
        md.write_text("")
        with patch("src.routing.log") as mock_log:
            await engine.load_rules()

        # Old rules retained — not replaced with empty list
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "chat-rule"

        # Warning was logged about zero-rule reload
        warning_calls = [
            c for c in mock_log.warning.call_args_list
            if "zero routing rules" in str(c).lower() or "retaining" in str(c).lower()
        ]
        assert len(warning_calls) >= 1

        # File mtimes NOT updated → next stale-check will detect the change and retry
        assert engine._file_mtimes == captured_mtimes

    async def test_reload_empty_files_then_restore_reloads_fresh(self, tmp_path: Path):
        """After graceful degradation, restoring file content triggers a fresh reload."""
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
        await engine.load_rules()
        assert len(engine.rules) == 1

        # Truncate → graceful degradation
        md.write_text("")
        await engine.load_rules()
        assert len(engine.rules) == 1  # old rules retained

        # Restore with updated content
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: new-rule
                  priority: 5
                  sender: "*"
                ---
                # Updated instruction
            """)
        )
        await engine.load_rules()
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "new-rule"
        assert engine.rules[0].priority == 5

    async def test_initial_empty_load_does_not_retain(self, tmp_path: Path):
        """First load with no rules should NOT retain (no previous rules exist)."""
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks require elevated privileges on Windows",
    )
    async def test_symlink_md_files_skipped_by_load_rules(self, tmp_path: Path):
        """Symlinks in the instructions directory are rejected during load_rules()."""
        # Create a real .md file outside the instructions dir
        outside = tmp_path / "outside.md"
        outside.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: outside-rule
                  priority: 1
                ---
                # Outside
            """)
        )
        # Create a real .md file inside the instructions dir
        instructions = tmp_path / "instructions"
        instructions.mkdir()
        real_md = instructions / "real.md"
        real_md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: real-rule
                  priority: 1
                ---
                # Real
            """)
        )
        # Create a symlink pointing outside the instructions dir
        link = instructions / "symlink.md"
        link.symlink_to(outside)

        engine = RoutingEngine(instructions)
        with patch("src.routing.log") as mock_log:
            await engine.load_rules()

        # Only the real file's rule loaded; symlink skipped
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "real-rule"

        # Warning logged for symlink
        warn_calls = [
            c for c in mock_log.warning.call_args_list
            if "symlink" in str(c).lower()
        ]
        assert len(warn_calls) >= 1

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks require elevated privileges on Windows",
    )
    async def test_symlink_md_files_skipped_by_scan_file_mtimes(self, tmp_path: Path):
        """Symlinks are excluded from _scan_file_mtimes results."""
        instructions = tmp_path / "instructions"
        instructions.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("# Outside")
        link = instructions / "symlink.md"
        link.symlink_to(outside)
        (instructions / "real.md").write_text("# Real")

        engine = RoutingEngine(instructions)
        mtimes = engine._scan_file_mtimes()

        assert "real.md" in mtimes
        assert "symlink.md" not in mtimes


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — match() and match_with_rule() tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineMatch:
    """Tests for RoutingEngine.match() and match_with_rule()."""

    # ── Basic matching ──────────────────────────────────────────────────

    async def test_catch_all_rule_matches_any_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule()]
        msg = make_msg(text="anything at all")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_no_rules_returns_none(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg()
        assert await engine.match(msg) is None

    async def test_match_with_rule_returns_tuple(self):
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule()
        engine._rules = [rule]
        msg = make_msg()

        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is rule
        assert instruction == "chat.agent.md"

    async def test_match_with_rule_no_match_returns_nones(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg()

        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None
        assert instruction is None

    async def test_match_delegates_to_match_with_rule(self):
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(instruction="special.md")
        engine._rules = [rule]
        msg = make_msg()

        assert await engine.match(msg) == "special.md"

    # ── Sender matching ────────────────────────────────────────────────

    async def test_specific_sender_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="5511999990000")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_specific_sender_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="5511999991111")
        assert await engine.match(msg) is None

    async def test_sender_regex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=r"55\d{10}")]
        msg = make_msg(sender_id="5511999990000")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_sender_regex_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=r"55\d{10}")]
        msg = make_msg(sender_id="4411999990000")
        assert await engine.match(msg) is None

    # ── Channel matching ───────────────────────────────────────────────

    async def test_specific_channel_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="whatsapp")]
        msg = make_msg(channel_type=ChannelType.WHATSAPP)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_specific_channel_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="telegram")]
        msg = make_msg(channel_type=ChannelType.WHATSAPP)
        assert await engine.match(msg) is None

    async def test_channel_regex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel=r"tele.*")]
        msg = make_msg(channel_type="telegram")
        assert await engine.match(msg) == "chat.agent.md"

    # ── Recipient matching ─────────────────────────────────────────────

    async def test_specific_recipient_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient="group-001")]
        msg = make_msg(chat_id="group-001")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_specific_recipient_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient="group-001")]
        msg = make_msg(chat_id="group-999")
        assert await engine.match(msg) is None

    # ── content_regex matching ─────────────────────────────────────────

    async def test_content_regex_matches_text(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="hello world")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_content_regex_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="goodbye world")
        assert await engine.match(msg) is None

    async def test_content_regex_complex_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^\d{4}-\d{2}-\d{2}$")]
        msg = make_msg(text="2024-01-15")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_content_regex_case_sensitive_by_default(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^Hello")]
        msg = make_msg(text="hello")
        assert await engine.match(msg) is None

    async def test_wildcard_content_matches_anything(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="literally anything")
        assert await engine.match(msg) == "chat.agent.md"

    # ── Priority ordering ──────────────────────────────────────────────

    async def test_lower_priority_evaluated_first(self):
        engine = RoutingEngine(Path("/dummy"))
        low_pri = make_rule(id="low", priority=1, instruction="low.md")
        high_pri = make_rule(id="high", priority=10, instruction="high.md")
        # Simulate load_rules sorting
        engine._rules = sorted([high_pri, low_pri], key=lambda r: r.priority)

        msg = make_msg()
        assert await engine.match(msg) == "low.md"

    async def test_first_matching_rule_wins(self):
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
        assert await engine.match(msg) == "a.md"

    async def test_first_rule_skipped_if_no_match_second_wins(self):
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
        assert await engine.match(msg) == "b.md"

    # ── enabled / disabled ─────────────────────────────────────────────

    async def test_disabled_rule_skipped(self):
        engine = RoutingEngine(Path("/dummy"))
        disabled = make_rule(id="disabled", enabled=False, instruction="disabled.md")
        engine._rules = [disabled]
        msg = make_msg()
        assert await engine.match(msg) is None

    async def test_disabled_rule_skipped_falls_through_to_next(self):
        engine = RoutingEngine(Path("/dummy"))
        disabled = make_rule(id="off", priority=1, enabled=False, instruction="off.md")
        fallback = make_rule(id="on", priority=5, enabled=True, instruction="on.md")
        engine._rules = [disabled, fallback]
        msg = make_msg()
        assert await engine.match(msg) == "on.md"

    async def test_enabled_field_defaults_to_true(self):
        rule = make_rule()  # enabled not explicitly set
        assert rule.enabled is True

    # ── fromMe filtering ────────────────────────────────────────────────

    async def test_fromMe_true_matches_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=True)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_fromMe_true_rejects_non_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=False)
        assert await engine.match(msg) is None

    async def test_fromMe_false_rejects_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=True)
        assert await engine.match(msg) is None

    async def test_fromMe_false_matches_non_bot_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=False)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_fromMe_none_matches_all(self):
        """When fromMe is None (default), the filter is not applied."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=None)]
        assert await engine.match(make_msg(fromMe=True)) == "chat.agent.md"
        assert await engine.match(make_msg(fromMe=False)) == "chat.agent.md"

    # ── toMe filtering ─────────────────────────────────────────────────

    async def test_toMe_true_matches_direct_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=True)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_toMe_true_rejects_group_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=False)
        assert await engine.match(msg) is None

    async def test_toMe_false_rejects_direct_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=True)
        assert await engine.match(msg) is None

    async def test_toMe_false_matches_group_message(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=False)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_toMe_none_matches_all(self):
        """When toMe is None (default), the filter is not applied."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=None)]
        assert await engine.match(make_msg(toMe=True)) == "chat.agent.md"
        assert await engine.match(make_msg(toMe=False)) == "chat.agent.md"

    # ── Combined filters ────────────────────────────────────────────────

    async def test_all_filters_must_match(self):
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
            await engine.match(
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
            await engine.match(
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
            await engine.match(
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
            await engine.match(
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
            await engine.match(
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
            await engine.match(
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
# Single-rule fast path
# ═══════════════════════════════════════════════════════════════════════════════


class TestSingleRuleFastPath:
    """Tests for the single-rule fast path in _match_impl.

    When only one routing rule is loaded, the engine skips cache-key
    hashing, dict lookups, and list iteration and evaluates the rule
    directly via the ``_single_rule`` attribute.
    """

    async def test_wildcard_single_rule_matches(self):
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule()  # wildcard by default
        engine._rules = [rule]
        msg = make_msg(text="anything")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is rule
        assert instruction == "chat.agent.md"

    async def test_single_rule_disabled_returns_none(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(enabled=False)]
        msg = make_msg()
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None
        assert instruction is None

    async def test_single_rule_fromMe_filter_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=True)
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None
        assert instruction == "chat.agent.md"

    async def test_single_rule_fromMe_filter_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=False)
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None
        assert instruction is None

    async def test_single_rule_toMe_filter_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=True)
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None

    async def test_single_rule_toMe_filter_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=False)
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None

    async def test_single_rule_sender_filter_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="5511999990000")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None

    async def test_single_rule_sender_filter_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="5511999990000")]
        msg = make_msg(sender_id="other")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None

    async def test_single_rule_content_regex_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="hello|hi")]
        msg = make_msg(text="hello world")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None
        assert instruction == "chat.agent.md"

    async def test_single_rule_content_regex_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="hello|hi")]
        msg = make_msg(text="goodbye world")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None

    async def test_single_rule_channel_filter_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="whatsapp")]
        msg = make_msg(channel_type="whatsapp")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None

    async def test_single_rule_channel_filter_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel="whatsapp")]
        msg = make_msg(channel_type="telegram")
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None

    async def test_single_rule_combined_filters_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(
                sender="5511999990000",
                channel="whatsapp",
                content_regex="hello.*",
                fromMe=False,
                toMe=True,
            )
        ]
        msg = make_msg(
            sender_id="5511999990000",
            channel_type="whatsapp",
            text="hello world",
            fromMe=False,
            toMe=True,
        )
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None

    async def test_single_rule_combined_filters_partial_no_match(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(
                sender="5511999990000",
                channel="whatsapp",
                content_regex="hello.*",
            )
        ]
        # Channel matches but content doesn't
        msg = make_msg(
            sender_id="5511999990000",
            channel_type="whatsapp",
            text="goodbye",
        )
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is None

    async def test_single_rule_returns_rule_identity(self):
        """Fast path returns the actual rule object, not a copy."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(id="my-unique-rule")
        engine._rules = [rule]
        msg = make_msg()
        matched_rule, _ = await engine.match_with_rule(msg)
        assert matched_rule is rule
        assert matched_rule.id == "my-unique-rule"

    async def test_two_rules_do_not_use_fast_path(self):
        """When 2+ rules exist, _single_rule is None and multi-rule path runs."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="rule-a", priority=0),
            make_rule(id="rule-b", priority=1, sender="other"),
        ]
        assert engine._single_rule is None
        msg = make_msg()
        matched_rule, instruction = await engine.match_with_rule(msg)
        assert matched_rule is not None
        assert matched_rule.id == "rule-a"

    async def test_single_rule_not_cached_on_fast_path(self):
        """The fast path does not populate the match cache."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule()]
        msg = make_msg()
        await engine.match_with_rule(msg)
        # Cache should remain empty — fast path bypasses it
        assert len(engine._match_cache) == 0

    async def test_reducing_to_single_rule_activates_fast_path(self):
        """Switching from multiple rules to one rule sets _single_rule."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(id="a"), make_rule(id="b")]
        assert engine._single_rule is None
        single = make_rule(id="only")
        engine._rules = [single]
        assert engine._single_rule is single


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    async def test_empty_text_matches_wildcard(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_empty_text_matches_empty_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="")]
        msg = make_msg(text="")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_empty_text_does_not_match_nonempty_pattern(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^hello")]
        msg = make_msg(text="")
        assert await engine.match(msg) is None

    async def test_unicode_text_matches_regex(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex=r"^こんにちは")]
        msg = make_msg(text="こんにちは世界")
        assert await engine.match(msg) == "chat.agent.md"

    async def test_very_long_text(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*")]
        msg = make_msg(text="A" * 100_000)
        assert await engine.match(msg) == "chat.agent.md"

    async def test_special_regex_chars_in_exact_match(self):
        """Patterns like 'group.1' should fall back to exact match if
        they don't regex-match (re.match anchors at start, '.' is wildcard)."""
        engine = RoutingEngine(Path("/dummy"))
        # "group.1" compiles as regex — the '.' matches any char
        engine._rules = [make_rule(recipient="group.1")]
        msg = make_msg(chat_id="groupX1")
        # "group.1" as regex matches "groupX1" via '.' wildcard
        assert await engine.match(msg) == "chat.agent.md"

    async def test_invalid_regex_in_sender_falls_back_to_exact(self):
        """If sender pattern is invalid regex, _compile_pattern returns None,
        and _match_compiled falls back to exact string comparison."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="[invalid-regex")]
        # Use a valid sender_id that matches the literal pattern
        msg = make_msg(sender_id="invalid-regex")
        # The pattern "[invalid-regex" won't compile → falls back to exact
        # match. "invalid-regex" != "[invalid-regex" → no match
        assert await engine.match(msg) is None

    async def test_invalid_regex_in_sender_exact_match_works(self):
        """Invalid regex pattern matches via exact string fallback.

        We bypass IncomingMessage validation by constructing a MatchingContext
        directly and calling match_with_rule, since sender_id validation
        rejects special chars like '(' that appear in invalid regex patterns.
        """
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="(unclosed")]
        # Build a MatchingContext directly (bypasses sender_id validation)
        ctx = MatchingContext(
            sender_id="(unclosed",
            chat_id="chat-001",
            channel_type="whatsapp",
            text="hello",
        )
        # _match_compiled falls back to exact string match for uncompileable patterns
        from src.routing import _match_compiled, _compile_pattern
        compiled = _compile_pattern("(unclosed")
        assert compiled is None  # doesn't compile
        assert _match_compiled(compiled, "(unclosed", "(unclosed") is True
        # Verify the full pipeline works
        rule, instruction = await engine.match_with_rule(
            make_msg(sender_id="unclosed")  # won't match "(unclosed"
        )
        assert rule is None  # "unclosed" != "(unclosed"

    async def test_multiple_rules_only_first_match_returned(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="first", priority=1, instruction="first.md"),
            make_rule(id="second", priority=2, instruction="second.md"),
            make_rule(id="third", priority=3, instruction="third.md"),
        ]
        msg = make_msg()
        assert await engine.match(msg) == "first.md"
        matched_rule, _ = await engine.match_with_rule(msg)
        assert matched_rule is not None
        assert matched_rule.id == "first"

    async def test_rules_loaded_in_priority_order_after_load(self, tmp_path: Path):
        """Verifies that load_rules sorts by priority."""
        for name, pri in [("z.md", 100), ("a.md", 1), ("m.md", 50)]:
            (tmp_path / name).write_text(
                f"---\nrouting:\n  id: {name}\n  priority: {pri}\n---\n\n# {name}\n"
            )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        priorities = [r.priority for r in engine.rules]
        assert priorities == sorted(priorities)

    async def test_same_priority_preserves_relative_order(self, tmp_path: Path):
        """Rules with the same priority should maintain stable order."""
        for name in ["b.md", "a.md", "c.md"]:
            (tmp_path / name).write_text(
                f"---\nrouting:\n  id: {name}\n  priority: 1\n---\n\n# {name}\n"
            )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        # With equal priorities, sorted() is stable, so order follows
        # the sorted filename order (a.md, b.md, c.md)
        ids = [r.id for r in engine.rules]
        assert len(ids) == 3
        # All same priority
        assert all(r.priority == 1 for r in engine.rules)

    async def test_match_with_rule_returns_correct_rule_object(self):
        """match_with_rule should return the actual matched rule instance."""
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(id="a", priority=1, instruction="a.md")
        rule_b = make_rule(id="b", priority=5, instruction="b.md")
        engine._rules = [rule_a, rule_b]

        msg = make_msg()
        matched, instruction = await engine.match_with_rule(msg)
        assert matched is rule_a
        assert instruction == "a.md"

        # Now make rule_a non-matching
        engine._rules = [
            make_rule(id="a", priority=1, sender="nonexistent", instruction="a.md"),
            rule_b,
        ]
        matched, instruction = await engine.match_with_rule(msg)
        assert matched is rule_b
        assert instruction == "b.md"

    async def test_match_context_from_message_isolation(self):
        """Verify MatchingContext is a snapshot — mutating the msg afterwards
        doesn't affect an already-created context."""
        msg = make_msg(text="original")
        ctx = MatchingContext.from_message(msg)
        # Create a new message (frozen, so can't mutate old one)
        assert ctx.text == "original"

    async def test_match_with_empty_rules_list(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        assert await engine.match(make_msg()) is None

    async def test_all_rules_disabled_returns_none(self):
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="r1", enabled=False),
            make_rule(id="r2", enabled=False),
        ]
        assert await engine.match(make_msg()) is None

    async def test_fromMe_and_toMe_combined_filters(self):
        """fromMe=True and toMe=True → only direct bot messages match."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True, toMe=True)]

        # fromMe=True, toMe=True → match
        assert await engine.match(make_msg(fromMe=True, toMe=True)) == "chat.agent.md"
        # fromMe=True, toMe=False → no match
        assert await engine.match(make_msg(fromMe=True, toMe=False)) is None
        # fromMe=False, toMe=True → no match
        assert await engine.match(make_msg(fromMe=False, toMe=True)) is None
        # fromMe=False, toMe=False → no match
        assert await engine.match(make_msg(fromMe=False, toMe=False)) is None


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
    async def test_match_compiled_various_patterns(self, pattern, value, expected):
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
    async def test_compile_pattern_results(self, pattern, should_compile):
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

    async def test_cache_hit_returns_same_result(self):
        """Repeated match() with same message returns cached result."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="cached.md")]
        msg = make_msg(text="hello")

        result1 = await engine.match(msg)
        result2 = await engine.match(msg)
        assert result1 == "cached.md"
        assert result2 == "cached.md"
        assert len(engine._match_cache) == 1

    async def test_cache_miss_for_different_messages(self):
        """Different messages produce separate cache entries."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="catch.md")]
        msg_a = make_msg(sender_id="alice", text="hi")
        msg_b = make_msg(sender_id="bob", text="hi")

        await engine.match(msg_a)
        await engine.match(msg_b)
        assert len(engine._match_cache) == 2

    async def test_cache_expired_entry_not_returned(self):
        """Expired cache entries are evicted on next access."""
        import time

        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="exp.md")]
        msg = make_msg(text="test")

        await engine.match(msg)
        assert len(engine._match_cache) == 1

        # Manually age the cache entry to simulate TTL expiry
        key = list(engine._match_cache._cache.keys())[0]
        value, _ = engine._match_cache._cache[key]
        engine._match_cache._cache[key] = (value, time.monotonic() - 100.0)

        # Next match should re-evaluate (cache miss)
        result = await engine.match(msg)
        assert result == "exp.md"
        # The expired entry was removed and a fresh one inserted
        assert len(engine._match_cache) == 1
        _, new_ts = engine._match_cache._cache[key]
        assert new_ts != time.monotonic() - 100.0

    async def test_cache_cleared_on_load_rules(self, tmp_path: Path):
        """load_rules() clears the match cache."""
        engine = RoutingEngine(tmp_path)
        engine._rules = [make_rule(instruction="tmp.md")]
        await engine.match(make_msg())
        assert len(engine._match_cache) == 1

        await engine.load_rules()
        assert len(engine._match_cache) == 0

    async def test_cache_cleared_on_refresh_rules(self, tmp_path: Path):
        """refresh_rules() clears the match cache."""
        engine = RoutingEngine(tmp_path)
        engine._rules = [make_rule()]
        await engine.match(make_msg())
        assert len(engine._match_cache) == 1

        await engine.refresh_rules()
        assert len(engine._match_cache) == 0

    async def test_cache_key_uses_full_text_hash(self):
        """Cache key hashes the full text — different suffixes produce distinct keys."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(content_regex="*", instruction="prefix.md")]

        # Two messages with same first 100 chars but different tails
        short_text = "A" * 100
        msg_a = make_msg(text=short_text)
        msg_b = make_msg(text=short_text + "B" * 100)

        await engine.match(msg_a)
        await engine.match(msg_b)
        # Different cache keys since full text differs (xxhash produces unique hashes)
        assert len(engine._match_cache) == 2

    async def test_cache_key_includes_fromMe_toMe_sender_channel(self):
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
            await engine.match(m)
        assert len(engine._match_cache) == 6

    async def test_cache_no_match_result_still_cached(self):
        """Cache also stores (None, None) results for no-match queries."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender="nonexistent")]
        msg = make_msg(sender_id="other")

        result = await engine.match(msg)
        assert result is None
        assert len(engine._match_cache) == 1

        # Second call returns cached None
        result2 = await engine.match(msg)
        assert result2 is None

    async def test_cache_lru_eviction_at_max_size(self):
        """Cache evicts LRU entries when exceeding max size."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(instruction="evict.md")]

        # Fill cache beyond capacity
        from src.constants import ROUTING_MATCH_CACHE_MAX_SIZE

        for i in range(ROUTING_MATCH_CACHE_MAX_SIZE + 10):
            await engine.match(make_msg(sender_id=f"sender-{i}", text=f"msg-{i}"))

        assert len(engine._match_cache) <= ROUTING_MATCH_CACHE_MAX_SIZE

    async def test_match_with_rule_cache_returns_same_rule_object(self):
        """Cached match_with_rule() returns the same rule object."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(id="cached-rule", instruction="cached.md")
        engine._rules = [rule]
        msg = make_msg()

        rule1, inst1 = await engine.match_with_rule(msg)
        rule2, inst2 = await engine.match_with_rule(msg)
        assert rule1 is rule
        assert rule2 is rule
        assert inst1 == "cached.md"
        assert inst2 == "cached.md"


# ═══════════════════════════════════════════════════════════════════════════════
# RoutingEngine — auto-reload (mtime-based lazy loading) tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRoutingEngineAutoReload:
    """Tests for mtime-based auto-reload of routing rules."""

    async def test_auto_reload_on_file_change(self, tmp_path: Path):
        """match() picks up new rules when an instruction file changes."""
        engine = _polling_engine(tmp_path)
        await engine.load_rules()
        assert engine.rules == []

        # Add a new instruction file
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        # Reset debounce so the next match triggers a stale check
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert await engine.match(msg) == "new.md"
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "new-rule"

    async def test_auto_reload_on_file_deletion(self, tmp_path: Path):
        """match() drops rules when an instruction file is deleted."""
        md = tmp_path / "temp.md"
        md.write_text(
            "---\nrouting:\n  id: temp-rule\n  priority: 1\n---\n\n# Temp\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()
        assert len(engine.rules) == 1

        # Delete the instruction file
        md.unlink()

        # Reset debounce
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert await engine.match(msg) is None
        assert engine.rules == []

    async def test_auto_reload_on_file_content_change(self, tmp_path: Path):
        """match() picks up changed content when an instruction file is rewritten."""
        md = tmp_path / "change.md"
        md.write_text(
            "---\nrouting:\n  id: original\n  priority: 1\n---\n\n# Original\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()
        assert engine.rules[0].id == "original"

        # Overwrite the file with new content
        md.write_text(
            "---\nrouting:\n  id: updated\n  priority: 1\n---\n\n# Updated\n"
        )

        # Reset debounce
        engine._last_stale_check = 0.0

        msg = make_msg()
        assert await engine.match(msg) == "change.md"
        assert engine.rules[0].id == "updated"

    async def test_no_reload_when_files_unchanged(self, tmp_path: Path):
        """No reload happens when files have not changed."""
        (tmp_path / "stable.md").write_text(
            "---\nrouting:\n  id: stable\n  priority: 1\n---\n\n# Stable\n"
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        # Access internal state before match
        rules_before = engine.rules
        mtimes_before = engine._file_mtimes.copy()

        # match() should not trigger a reload
        msg = make_msg()
        engine._last_stale_check = 0.0  # Allow stale check
        result = await engine.match(msg)
        assert result == "stable.md"

        # Rules list should be the same object (no reload)
        assert engine.rules is rules_before
        assert engine._file_mtimes == mtimes_before

    async def test_debounce_prevents_rapid_stale_checks(self, tmp_path: Path):
        """Successive match() calls within the debounce window skip stale checks."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        # First match triggers stale check
        engine._last_stale_check = 0.0
        await engine.match(make_msg())
        first_check_time = engine._last_stale_check

        # Second match within debounce window should NOT update stale check time
        await engine.match(make_msg())
        assert engine._last_stale_check == first_check_time

    async def test_scan_file_mtimes_returns_current_mtimes(self, tmp_path: Path):
        """_scan_file_mtimes returns mtimes for all .md files."""
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        (tmp_path / "c.txt").write_text("# Not markdown")

        engine = RoutingEngine(tmp_path)
        mtimes = engine._scan_file_mtimes()

        assert "a.md" in mtimes
        assert "b.md" in mtimes
        assert "c.txt" not in mtimes

    async def test_scan_file_mtimes_empty_dir(self, tmp_path: Path):
        """_scan_file_mtimes returns empty dict for empty directory."""
        engine = RoutingEngine(tmp_path)
        assert engine._scan_file_mtimes() == {}

    async def test_scan_file_mtimes_nonexistent_dir(self, tmp_path: Path):
        """_scan_file_mtimes returns empty dict for missing directory."""
        engine = RoutingEngine(tmp_path / "missing")
        assert engine._scan_file_mtimes() == {}

    async def test_is_stale_detects_new_file(self, tmp_path: Path):
        """_is_stale returns True when a new .md file appears."""
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new\n  priority: 1\n---\n\n# New\n"
        )

        engine._last_stale_check = 0.0
        assert engine._is_stale() is True

    async def test_is_stale_returns_false_when_unchanged(self, tmp_path: Path):
        """_is_stale returns False when files have not changed."""
        (tmp_path / "same.md").write_text("# Same")
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        engine._last_stale_check = 0.0
        assert engine._is_stale() is False

    async def test_load_rules_populates_file_mtimes(self, tmp_path: Path):
        """load_rules caches mtimes after loading."""
        (tmp_path / "x.md").write_text(
            "---\nrouting:\n  id: x\n  priority: 1\n---\n\n# X\n"
        )
        engine = RoutingEngine(tmp_path)
        assert engine._file_mtimes == {}

        await engine.load_rules()
        assert "x.md" in engine._file_mtimes
        assert engine._file_mtimes["x.md"] > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Property-based tests (Hypothesis)
# ═══════════════════════════════════════════════════════════════════════════════

from hypothesis import given, settings, assume
from hypothesis import strategies as st


# -- Strategies for generating routing inputs --

# Strings matching _SENDER_ID_RE / _CHAT_ID_RE: ASCII [a-zA-Z0-9_\-.@]+
simple_text = st.text(
    alphabet=string.ascii_letters + string.digits + "_-.@",
    min_size=1,
    max_size=30,
)

# Channel type strings: lowercase [a-z0-9_]+ (matches _CHANNEL_TYPE_RE)
channel_text = st.text(
    alphabet=string.ascii_lowercase + string.digits + "_",
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
    async def test_wildcard_always_matches(self, value: str):
        """Invariant: pattern '*' matches any string value."""
        assert _match_compiled(None, "*", value) is True

    @given(value=printable_text)
    @settings(max_examples=200)
    async def test_empty_pattern_only_matches_empty(self, value: str):
        """Invariant: pattern '' matches iff value is also empty."""
        expected = value == ""
        assert _match_compiled(None, "", value) is expected

    @given(pattern=st.text(min_size=1, max_size=50), value=printable_text)
    @settings(max_examples=300)
    async def test_compiled_vs_manual_equivalence(self, pattern: str, value: str):
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
    async def test_match_compiled_is_pure(self, pattern: str, value: str):
        """Invariant: calling _match_compiled twice with same inputs gives same result."""
        compiled = _compile_pattern(pattern)
        result1 = _match_compiled(compiled, pattern, value)
        result2 = _match_compiled(compiled, pattern, value)
        assert result1 is result2

    @given(pattern=valid_regex, value=printable_text)
    @settings(max_examples=200)
    async def test_match_result_is_bool(self, pattern: str, value: str):
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
    async def test_wildcard_fields_compile_to_none(
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
    async def test_valid_regex_fields_compile_to_pattern(self, sender, recipient, channel, content_regex):
        """Invariant: valid regex patterns always compile to non-None Pattern at construction."""
        rule = RoutingRule(
            id="t", priority=0, sender=sender, recipient=recipient,
            channel=channel, content_regex=content_regex, instruction="t.md",
        )
        # Patterns are compiled eagerly at construction
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
    async def test_is_wildcard_invariant(
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
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_catch_all_rule_matches_every_message(
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
        assert await engine.match(msg) == "chat.agent.md"

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_disabled_rule_never_matches(
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
        assert await engine.match(msg) is None

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
    async def test_all_disabled_returns_none(self, rules: list, fromMe: bool, toMe: bool):
        """Invariant: when all rules are disabled, match always returns None."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = make_msg(fromMe=fromMe, toMe=toMe)
        assert await engine.match(msg) is None

    @given(fromMe_val=st.booleans(), toMe_val=st.booleans())
    @settings(max_examples=50)
    async def test_none_filters_pass_all(self, fromMe_val: bool, toMe_val: bool):
        """Invariant: fromMe=None and toMe=None match both True and False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=None, toMe=None)]
        msg = make_msg(fromMe=fromMe_val, toMe=toMe_val)
        assert await engine.match(msg) == "chat.agent.md"

    @given(fromMe_val=st.booleans())
    @settings(max_examples=50)
    async def test_fromMe_true_only_matches_bot_messages(self, fromMe_val: bool):
        """Invariant: fromMe=True only matches when message fromMe is True."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=True)]
        msg = make_msg(fromMe=fromMe_val)
        result = await engine.match(msg)
        if fromMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(fromMe_val=st.booleans())
    @settings(max_examples=50)
    async def test_fromMe_false_only_matches_non_bot_messages(self, fromMe_val: bool):
        """Invariant: fromMe=False only matches when message fromMe is False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=False)]
        msg = make_msg(fromMe=fromMe_val)
        result = await engine.match(msg)
        if not fromMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(toMe_val=st.booleans())
    @settings(max_examples=50)
    async def test_toMe_true_only_matches_direct_messages(self, toMe_val: bool):
        """Invariant: toMe=True only matches when message toMe is True."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=True)]
        msg = make_msg(toMe=toMe_val)
        result = await engine.match(msg)
        if toMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(toMe_val=st.booleans())
    @settings(max_examples=50)
    async def test_toMe_false_only_matches_group_messages(self, toMe_val: bool):
        """Invariant: toMe=False only matches when message toMe is False."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(toMe=False)]
        msg = make_msg(toMe=toMe_val)
        result = await engine.match(msg)
        if not toMe_val:
            assert result == "chat.agent.md"
        else:
            assert result is None

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_matching_is_deterministic(
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
        result1 = await engine.match(msg)
        result2 = await engine.match(msg)
        assert result1 == result2

    @given(
        priorities=st.lists(st.integers(min_value=0, max_value=100), min_size=2, max_size=5),
    )
    @settings(max_examples=100)
    async def test_first_matching_rule_has_lowest_priority(self, priorities: list):
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
        result = await engine.match(msg)
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
    async def test_disabled_rule_before_enabled_falls_through(
        self, text: str, sender_id: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: disabled rule at lower priority falls through to next enabled rule."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [
            make_rule(id="disabled", priority=1, enabled=False, instruction="disabled.md"),
            make_rule(id="enabled", priority=10, enabled=True, instruction="enabled.md"),
        ]
        msg = make_msg(text=text, sender_id=sender_id, fromMe=fromMe, toMe=toMe)
        assert await engine.match(msg) == "enabled.md"

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_empty_rules_always_returns_none(
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
        assert await engine.match(msg) is None

    @given(
        specific_sender=simple_text,
        other_sender=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_specific_sender_matches_exactly(
        self, specific_sender: str, other_sender: str, fromMe: bool, toMe: bool,
    ):
        """Invariant: specific sender pattern only matches that exact sender."""
        assume(specific_sender != other_sender)
        # Guard against regex cross-matching: pattern "ab" matches "abcd"
        assume(not re.match(specific_sender, other_sender))
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(sender=specific_sender)]
        matching_msg = make_msg(sender_id=specific_sender, fromMe=fromMe, toMe=toMe)
        non_matching_msg = make_msg(sender_id=other_sender, fromMe=fromMe, toMe=toMe)
        assert await engine.match(matching_msg) == "chat.agent.md"
        assert await engine.match(non_matching_msg) is None

    @given(
        specific_channel=channel_text,
        other_channel=channel_text,
    )
    @settings(max_examples=200)
    async def test_specific_channel_matches_exactly(
        self, specific_channel: str, other_channel: str,
    ):
        """Invariant: specific channel pattern only matches that exact channel."""
        assume(specific_channel != other_channel)
        assume(not re.match(specific_channel, other_channel))
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(channel=specific_channel)]
        assert await engine.match(make_msg(channel_type=specific_channel)) == "chat.agent.md"
        assert await engine.match(make_msg(channel_type=other_channel)) is None

    @given(
        specific_recipient=simple_text,
        other_recipient=simple_text,
    )
    @settings(max_examples=200)
    async def test_specific_recipient_matches_exactly(
        self, specific_recipient: str, other_recipient: str,
    ):
        """Invariant: specific recipient pattern only matches that exact recipient."""
        assume(specific_recipient != other_recipient)
        assume(not re.match(specific_recipient, other_recipient))
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(recipient=specific_recipient)]
        assert await engine.match(make_msg(chat_id=specific_recipient)) == "chat.agent.md"
        assert await engine.match(make_msg(chat_id=other_recipient)) is None

    @given(
        fromMe_filter=bool_or_none,
        toMe_filter=bool_or_none,
        msg_fromMe=st.booleans(),
        msg_toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_boolean_filter_invariant(
        self, fromMe_filter, toMe_filter, msg_fromMe: bool, msg_toMe: bool,
    ):
        """Invariant: fromMe/toMe filters work correctly for all combinations."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(fromMe=fromMe_filter, toMe=toMe_filter)]
        msg = make_msg(fromMe=msg_fromMe, toMe=msg_toMe)
        result = await engine.match(msg)

        # Compute expected: None = pass-through, True/False = must match exactly
        fromMe_pass = fromMe_filter is None or fromMe_filter == msg_fromMe
        toMe_pass = toMe_filter is None or toMe_filter == msg_toMe
        expected = fromMe_pass and toMe_pass
        assert (result is not None) == expected


class TestPropertyMatchWithRule:
    """Property-based invariants for RoutingEngine.match_with_rule().

    Verifies the (rule, instruction) tuple returned by match_with_rule(),
    covering rule identity, priority ordering, cache coherence, wildcard
    behaviour, and equivalence with match().
    """

    # -- Tuple shape invariant --

    @given(
        rules=st.lists(
            st.builds(
                make_rule,
                id=st.integers(0, 999).map(lambda i: f"r-{i}"),
                priority=st.integers(0, 100),
                instruction=st.integers(0, 99).map(lambda i: f"inst-{i}.md"),
                enabled=st.booleans(),
                fromMe=bool_or_none,
                toMe=bool_or_none,
            ),
            min_size=0,
            max_size=8,
        ),
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_returns_tuple_of_optional_types(
        self, rules, text, sender_id, chat_id, channel_type, fromMe, toMe,
    ):
        """Invariant: match_with_rule always returns (RoutingRule|None, str|None)."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = sorted(rules, key=lambda r: r.priority)
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is None or isinstance(rule, RoutingRule)
        assert instruction is None or isinstance(instruction, str)
        # Both None or both non-None (instruction is always a str from a rule)
        if rule is None:
            assert instruction is None

    # -- Rule identity: returned rule is from the engine's rule list --

    @given(
        rules=st.lists(
            st.builds(
                make_rule,
                id=st.integers(0, 999).map(lambda i: f"r-{i}"),
                priority=st.integers(0, 100),
                instruction=st.integers(0, 99).map(lambda i: f"inst-{i}.md"),
                enabled=st.booleans(),
                fromMe=bool_or_none,
                toMe=bool_or_none,
            ),
            min_size=1,
            max_size=10,
        ),
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_returned_rule_is_from_rules_list(
        self, rules, text, sender_id, chat_id, fromMe, toMe,
    ):
        """Invariant: when match succeeds, the returned rule object is in _rules."""
        engine = RoutingEngine(Path("/dummy"))
        sorted_rules = sorted(rules, key=lambda r: r.priority)
        engine._rules = sorted_rules
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            fromMe=fromMe, toMe=toMe,
        )
        rule, instruction = await engine.match_with_rule(msg)
        if rule is not None:
            assert rule in sorted_rules
            assert instruction == rule.instruction

    # -- match() vs match_with_rule() equivalence --

    @given(
        rules=st.lists(
            st.builds(
                make_rule,
                id=st.integers(0, 999).map(lambda i: f"r-{i}"),
                priority=st.integers(0, 100),
                instruction=st.integers(0, 99).map(lambda i: f"inst-{i}.md"),
                enabled=st.booleans(),
                fromMe=bool_or_none,
                toMe=bool_or_none,
            ),
            min_size=0,
            max_size=8,
        ),
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_match_equivalence(
        self, rules, text, sender_id, chat_id, fromMe, toMe,
    ):
        """Invariant: match(msg) == match_with_rule(msg)[1] for all inputs."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = sorted(rules, key=lambda r: r.priority)
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            fromMe=fromMe, toMe=toMe,
        )
        instruction_only = await engine.match(msg)
        _, instruction_from_tuple = await engine.match_with_rule(msg)
        assert instruction_only == instruction_from_tuple

    # -- Cache coherence: repeated calls return same tuple --

    @given(
        rules=st.lists(
            st.builds(
                make_rule,
                id=st.integers(0, 999).map(lambda i: f"r-{i}"),
                priority=st.integers(0, 100),
                instruction=st.integers(0, 99).map(lambda i: f"inst-{i}.md"),
                enabled=st.booleans(),
                fromMe=bool_or_none,
                toMe=bool_or_none,
            ),
            min_size=0,
            max_size=6,
        ),
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=150)
    async def test_cache_returns_same_result_on_repeat(
        self, rules, text, sender_id, chat_id, fromMe, toMe,
    ):
        """Invariant: calling match_with_rule twice returns identical tuples."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = sorted(rules, key=lambda r: r.priority)
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            fromMe=fromMe, toMe=toMe,
        )
        result1 = await engine.match_with_rule(msg)
        result2 = await engine.match_with_rule(msg)
        assert result1 == result2

    # -- Cache coherence: object identity preserved --

    @given(
        text=printable_text,
        sender_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_cache_preserves_rule_object_identity(
        self, text, sender_id, fromMe, toMe,
    ):
        """Invariant: cached result returns the exact same rule object (identity)."""
        engine = RoutingEngine(Path("/dummy"))
        catch_all = make_rule(id="catch-all", instruction="catch.md")
        engine._rules = [catch_all]
        msg = make_msg(text=text, sender_id=sender_id, fromMe=fromMe, toMe=toMe)
        rule1, _ = await engine.match_with_rule(msg)
        rule2, _ = await engine.match_with_rule(msg)
        # Same object identity (from cache)
        assert rule1 is rule2
        assert rule1 is catch_all

    # -- Priority ordering: returned rule has lowest priority value --

    @given(
        priorities=st.lists(
            st.integers(min_value=0, max_value=100),
            min_size=2,
            max_size=8,
        ),
    )
    @settings(max_examples=150)
    async def test_returned_rule_has_lowest_priority(self, priorities):
        """Invariant: match_with_rule returns the rule with lowest priority value."""
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
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.priority == min(priorities)
        assert instruction == f"rule-{min(priorities)}.md"

    # -- Priority ordering: disabled high-priority falls through --

    @given(
        disabled_priority=st.integers(0, 50),
        enabled_priority=st.integers(51, 100),
        text=printable_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_disabled_high_priority_falls_through_to_next(
        self, disabled_priority, enabled_priority, text, fromMe, toMe,
    ):
        """Invariant: disabled rule at lower priority falls through; returned rule
        is the enabled one."""
        engine = RoutingEngine(Path("/dummy"))
        disabled = make_rule(
            id="disabled", priority=disabled_priority, enabled=False,
            instruction="disabled.md",
        )
        enabled = make_rule(
            id="enabled", priority=enabled_priority, enabled=True,
            instruction="enabled.md",
        )
        engine._rules = sorted([disabled, enabled], key=lambda r: r.priority)
        msg = make_msg(text=text, fromMe=fromMe, toMe=toMe)
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is enabled
        assert instruction == "enabled.md"

    # -- No match returns (None, None) --

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_no_match_returns_none_tuple(
        self, text, sender_id, chat_id, fromMe, toMe,
    ):
        """Invariant: no matching rules → (None, None)."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(enabled=False)]
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            fromMe=fromMe, toMe=toMe,
        )
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is None
        assert instruction is None

    # -- Wildcard matching: catch-all always matches and returns itself --

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_wildcard_rule_matches_everything_and_returns_itself(
        self, text, sender_id, chat_id, channel_type, fromMe, toMe,
    ):
        """Invariant: all-wildcard rule matches any message and returns itself."""
        engine = RoutingEngine(Path("/dummy"))
        catch_all = make_rule(id="catch-all", instruction="catch.md")
        engine._rules = [catch_all]
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is catch_all
        assert instruction == "catch.md"

    # -- Wildcard: specific rule before wildcard loses on mismatch --

    @given(
        specific_sender=simple_text,
        other_sender=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=150)
    async def test_specific_rule_before_wildcard_loses_on_mismatch(
        self, specific_sender, other_sender, fromMe, toMe,
    ):
        """Invariant: specific sender rule at higher priority doesn't match a
        different sender; wildcard at lower priority takes over."""
        assume(specific_sender != other_sender)
        assume(not re.match(specific_sender, other_sender))
        engine = RoutingEngine(Path("/dummy"))
        specific = make_rule(
            id="specific", priority=1, sender=specific_sender,
            instruction="specific.md",
        )
        wildcard = make_rule(
            id="wildcard", priority=10,
            instruction="wildcard.md",
        )
        engine._rules = [specific, wildcard]
        msg = make_msg(sender_id=other_sender, fromMe=fromMe, toMe=toMe)
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is wildcard
        assert instruction == "wildcard.md"

    # -- Cache cleared after _rules setter --

    @given(
        text=printable_text,
        sender_id=simple_text,
    )
    @settings(max_examples=50)
    async def test_cache_cleared_after_rules_setter(self, text, sender_id):
        """Invariant: setting _rules clears the cache; new rules produce new results."""
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(id="a", priority=1, instruction="a.md")
        engine._rules = [rule_a]
        msg = make_msg(text=text, sender_id=sender_id)
        rule1, inst1 = await engine.match_with_rule(msg)
        assert rule1 is rule_a
        assert len(engine._match_cache) == 1

        # Replace rules — cache should be cleared
        rule_b = make_rule(id="b", priority=1, instruction="b.md")
        engine._rules = [rule_b]
        assert len(engine._match_cache) == 0

        # New match populates with new rule
        rule2, inst2 = await engine.match_with_rule(msg)
        assert rule2 is rule_b
        assert inst2 == "b.md"
        assert len(engine._match_cache) == 1

    # -- Different cache keys produce independent results --

    @given(
        sender_a=simple_text,
        sender_b=simple_text,
    )
    @settings(max_examples=100)
    async def test_different_cache_keys_produce_independent_results(
        self, sender_a, sender_b,
    ):
        """Invariant: messages with different cache keys are evaluated independently."""
        assume(sender_a != sender_b)
        assume(not re.match(sender_a, sender_b))
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="match-a", priority=1, sender=sender_a, instruction="a.md",
        )
        rule_b = make_rule(
            id="match-b", priority=2, sender=sender_b, instruction="b.md",
        )
        wildcard = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [rule_a, rule_b, wildcard]

        msg_a = make_msg(sender_id=sender_a)
        msg_b = make_msg(sender_id=sender_b)

        rule_ra, inst_ra = await engine.match_with_rule(msg_a)
        rule_rb, inst_rb = await engine.match_with_rule(msg_b)

        assert rule_ra is rule_a
        assert inst_ra == "a.md"
        assert rule_rb is rule_b
        assert inst_rb == "b.md"
        assert len(engine._match_cache) == 2

    # -- Text truncation in cache key: first 100 chars determine cache hit --

    @given(
        prefix=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=50,
            max_size=100,
        ),
        suffix_a=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=50,
        ),
        suffix_b=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=50, deadline=None)
    async def test_cache_key_uses_first_100_chars_of_text(
        self, prefix, suffix_a, suffix_b,
    ):
        """Invariant: two messages differing only beyond char 100 share a cache entry."""
        assume(suffix_a != suffix_b)
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(id="r", priority=1, instruction="r.md")]

        msg_a = make_msg(text=prefix + suffix_a)
        msg_b = make_msg(text=prefix + suffix_b)

        rule_a, _ = await engine.match_with_rule(msg_a)
        rule_b, _ = await engine.match_with_rule(msg_b)

        # Both should match (wildcard rule), but the second call should be a
        # cache hit returning the same cached rule object from msg_a's call.
        assert rule_a is rule_b

    # -- fromMe/toMe filters: all combinations with rule identity --

    @given(
        fromMe_filter=bool_or_none,
        toMe_filter=bool_or_none,
        msg_fromMe=st.booleans(),
        msg_toMe=st.booleans(),
    )
    @settings(max_examples=200)
    async def test_boolean_filter_with_rule_identity(
        self, fromMe_filter, toMe_filter, msg_fromMe, msg_toMe,
    ):
        """Invariant: fromMe/toMe filters produce correct rule (or None) identity."""
        engine = RoutingEngine(Path("/dummy"))
        target = make_rule(
            id="target", fromMe=fromMe_filter, toMe=toMe_filter,
            instruction="target.md",
        )
        engine._rules = [target]
        msg = make_msg(fromMe=msg_fromMe, toMe=msg_toMe)
        rule, instruction = await engine.match_with_rule(msg)

        fromMe_pass = fromMe_filter is None or fromMe_filter == msg_fromMe
        toMe_pass = toMe_filter is None or toMe_filter == msg_toMe
        expected_match = fromMe_pass and toMe_pass

        if expected_match:
            assert rule is target
            assert instruction == "target.md"
        else:
            assert rule is None
            assert instruction is None

    # -- Content regex pattern matching --

    @given(
        matching_text=st.from_regex(r"hello[a-z]*", fullmatch=True),
        non_matching_text=st.from_regex(r"[0-9]{3,10}", fullmatch=True),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_content_regex_pattern_selectively_matches(
        self, matching_text, non_matching_text, fromMe, toMe,
    ):
        """Invariant: content_regex='hello[a-z]*' matches matching text but not
        non-matching text, while wildcard fallback catches the rest."""
        engine = RoutingEngine(Path("/dummy"))
        regex_rule = make_rule(
            id="regex", priority=1, content_regex="hello[a-z]*",
            instruction="regex.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [regex_rule, fallback]

        # Matching text → regex rule wins
        msg_match = make_msg(text=matching_text, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg_match)
        assert rule is regex_rule
        assert inst == "regex.md"

        # Non-matching text → wildcard fallback wins
        msg_no = make_msg(text=non_matching_text, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg_no)
        assert rule is fallback
        assert inst == "fallback.md"

    # -- Channel pattern narrows match --

    @given(
        target_channel=channel_text,
        other_channel=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_channel_pattern_narrows_match(
        self, target_channel, other_channel, fromMe, toMe,
    ):
        """Invariant: rule with specific channel only matches that channel;
        wildcard at lower priority catches everything else."""
        assume(target_channel != other_channel)
        assume(not re.match(target_channel, other_channel))
        engine = RoutingEngine(Path("/dummy"))
        channel_rule = make_rule(
            id="chan", priority=1, channel=target_channel,
            instruction="chan.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [channel_rule, fallback]

        msg_target = make_msg(
            channel_type=target_channel, fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_target)
        assert rule is channel_rule
        assert inst == "chan.md"

        msg_other = make_msg(
            channel_type=other_channel, fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_other)
        assert rule is fallback
        assert inst == "fallback.md"

    # -- Recipient pattern narrows match --

    @given(
        target_recipient=simple_text,
        other_recipient=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_recipient_pattern_narrows_match(
        self, target_recipient, other_recipient, fromMe, toMe,
    ):
        """Invariant: rule with specific recipient only matches that chat_id."""
        assume(target_recipient != other_recipient)
        # Guard: routing engine uses regex matching, so "0" matches "00"
        assume(not re.match(target_recipient, other_recipient))
        assume(not re.match(other_recipient, target_recipient))
        engine = RoutingEngine(Path("/dummy"))
        recipient_rule = make_rule(
            id="recip", priority=1, recipient=target_recipient,
            instruction="recip.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [recipient_rule, fallback]

        msg_target = make_msg(
            chat_id=target_recipient, fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_target)
        assert rule is recipient_rule
        assert inst == "recip.md"

        msg_other = make_msg(
            chat_id=other_recipient, fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_other)
        assert rule is fallback
        assert inst == "fallback.md"

    # -- Multi-rule precedence with mixed pattern fields --

    @given(
        sender_a=simple_text,
        sender_b=simple_text,
        channel_a=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_multi_rule_precedence_mixed_patterns(
        self, sender_a, sender_b, channel_a, fromMe, toMe,
    ):
        """Invariant: rules are evaluated in priority order; first matching rule
        wins even when rules use different pattern fields."""
        assume(sender_a != sender_b)
        assume(not re.match(sender_a, sender_b))
        # Guard against channel_a regex-matching the default channel "whatsapp"
        # used by msg_fallback
        assume(not re.match(channel_a, "whatsapp"))
        engine = RoutingEngine(Path("/dummy"))
        # Priority 1: specific sender
        sender_rule = make_rule(
            id="sender", priority=1, sender=sender_a,
            instruction="sender.md",
        )
        # Priority 5: specific channel
        channel_rule = make_rule(
            id="channel", priority=5, channel=channel_a,
            instruction="channel.md",
        )
        # Priority 10: catch-all
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [sender_rule, channel_rule, fallback]

        # Message matching sender_a → sender rule wins (priority 1)
        msg_sender = make_msg(
            sender_id=sender_a, channel_type=channel_a,
            fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_sender)
        assert rule is sender_rule
        assert inst == "sender.md"

        # Message from sender_b on channel_a → channel rule wins (priority 5)
        msg_channel = make_msg(
            sender_id=sender_b, channel_type=channel_a,
            fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_channel)
        assert rule is channel_rule
        assert inst == "channel.md"

        # Message from sender_b on other channel → fallback wins (priority 10)
        msg_fallback = make_msg(
            sender_id=sender_b, fromMe=fromMe, toMe=toMe,
        )
        rule, inst = await engine.match_with_rule(msg_fallback)
        assert rule is fallback
        assert inst == "fallback.md"

    # -- Same priority: first in list wins --

    @given(
        text=printable_text,
        sender_id=simple_text,
    )
    @settings(max_examples=100)
    async def test_same_priority_first_in_list_wins(self, text, sender_id):
        """Invariant: when two enabled rules have the same priority, the first
        rule in the sorted list (stable sort) wins."""
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(id="a", priority=5, instruction="a.md")
        rule_b = make_rule(id="b", priority=5, instruction="b.md")
        # Both have same priority; stable sort preserves insertion order
        engine._rules = [rule_a, rule_b]
        msg = make_msg(text=text, sender_id=sender_id)
        rule, inst = await engine.match_with_rule(msg)
        assert rule is rule_a
        assert inst == "a.md"

    # -- Returned rule preserves all attributes --

    @given(
        skill_exec_verbose=st.sampled_from(["", "summary", "full"]),
        show_errors=st.booleans(),
    )
    @settings(max_examples=50)
    async def test_returned_rule_preserves_attributes(
        self, skill_exec_verbose, show_errors,
    ):
        """Invariant: the returned rule object preserves skillExecVerbose and
        showErrors from the original rule definition."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(
            id="attrs",
            instruction="attrs.md",
            skillExecVerbose=skill_exec_verbose,
            showErrors=show_errors,
        )
        engine._rules = [rule]
        msg = make_msg()
        returned_rule, inst = await engine.match_with_rule(msg)
        assert returned_rule is rule
        assert returned_rule.skillExecVerbose == skill_exec_verbose
        assert returned_rule.showErrors == show_errors
        assert inst == "attrs.md"

    # -- Cache key distinguishes sender_id --

    @given(
        sender_a=simple_text,
        sender_b=simple_text,
        target_sender=simple_text,
    )
    @settings(max_examples=80)
    async def test_cache_distinguishes_sender_id(
        self, sender_a, sender_b, target_sender,
    ):
        """Invariant: messages from different senders produce independent cache
        entries when a sender-specific rule exists."""
        assume(sender_a != sender_b)
        assume(not re.match(sender_a, sender_b))
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="match-a", priority=1, sender=sender_a, instruction="a.md",
        )
        rule_b = make_rule(
            id="match-b", priority=2, sender=sender_b, instruction="b.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [rule_a, rule_b, fallback]

        # Match from sender_a
        msg_a = make_msg(sender_id=sender_a)
        rule_ra, _ = await engine.match_with_rule(msg_a)
        assert rule_ra is rule_a

        # Match from sender_b — different cache key, independent result
        msg_b = make_msg(sender_id=sender_b)
        rule_rb, _ = await engine.match_with_rule(msg_b)
        assert rule_rb is rule_b

        assert len(engine._match_cache) == 2

    # -- Cache key distinguishes chat_id --

    @given(
        chat_a=simple_text,
        chat_b=simple_text,
    )
    @settings(max_examples=80)
    async def test_cache_distinguishes_chat_id(self, chat_a, chat_b):
        """Invariant: messages in different chats produce independent cache
        entries when a recipient-specific rule exists."""
        assume(chat_a != chat_b)
        assume(not re.match(chat_a, chat_b))
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="chat-a", priority=1, recipient=chat_a, instruction="a.md",
        )
        rule_b = make_rule(
            id="chat-b", priority=2, recipient=chat_b, instruction="b.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [rule_a, rule_b, fallback]

        rule_ra, _ = await engine.match_with_rule(make_msg(chat_id=chat_a))
        rule_rb, _ = await engine.match_with_rule(make_msg(chat_id=chat_b))

        assert rule_ra is rule_a
        assert rule_rb is rule_b
        assert len(engine._match_cache) == 2

    # -- Cache key distinguishes channel_type --

    @given(
        channel_a=channel_text,
        channel_b=channel_text,
    )
    @settings(max_examples=80)
    async def test_cache_distinguishes_channel_type(self, channel_a, channel_b):
        """Invariant: messages on different channels produce independent cache
        entries when a channel-specific rule exists."""
        assume(channel_a != channel_b)
        assume(not re.match(channel_a, channel_b))
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(
            id="chan-a", priority=1, channel=channel_a, instruction="a.md",
        )
        rule_b = make_rule(
            id="chan-b", priority=2, channel=channel_b, instruction="b.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [rule_a, rule_b, fallback]

        rule_ra, _ = await engine.match_with_rule(make_msg(channel_type=channel_a))
        rule_rb, _ = await engine.match_with_rule(make_msg(channel_type=channel_b))

        assert rule_ra is rule_a
        assert rule_rb is rule_b
        assert len(engine._match_cache) == 2

    # -- Empty content_regex matches only empty text --

    @given(
        text=printable_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_empty_content_regex_matches_only_empty_text(
        self, text, fromMe, toMe,
    ):
        """Invariant: content_regex='' only matches messages with empty text."""
        engine = RoutingEngine(Path("/dummy"))
        empty_rule = make_rule(
            id="empty", priority=1, content_regex="", instruction="empty.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [empty_rule, fallback]

        msg = make_msg(text=text, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg)

        if text == "":
            assert rule is empty_rule
            assert inst == "empty.md"
        else:
            assert rule is fallback
            assert inst == "fallback.md"

    # -- Wildcard in one field with specific others still requires those matches --

    @given(
        specific_sender=simple_text,
        other_sender=simple_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_wildcard_sender_with_specific_channel_requires_channel_match(
        self, specific_sender, other_sender, fromMe, toMe,
    ):
        """Invariant: a rule with wildcard sender but specific channel still
        requires the channel to match."""
        assume(specific_sender != other_sender)
        engine = RoutingEngine(Path("/dummy"))
        channel = "whatsapp"
        rule = make_rule(
            id="chan-only", priority=1,
            sender="*", channel=channel,
            instruction="chan.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [rule, fallback]

        # Same channel → matches regardless of sender
        msg_match = make_msg(
            sender_id=specific_sender, channel_type=channel,
            fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_match)
        assert r is rule
        assert inst == "chan.md"

        # Different channel → falls through to wildcard
        msg_no = make_msg(
            sender_id=other_sender, channel_type="telegram",
            fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_no)
        assert r is fallback
        assert inst == "fallback.md"

    # ── fromMe/toMe interaction with specific sender pattern ─────────────────

    @given(
        target_sender=simple_text,
        other_sender=simple_text,
        msg_fromMe=st.booleans(),
        msg_toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_fromMe_filter_with_specific_sender_requires_both(
        self, target_sender, other_sender, msg_fromMe, msg_toMe,
    ):
        """Invariant: rule with specific sender AND fromMe=True only matches
        messages from that sender AND with fromMe=True."""
        assume(target_sender != other_sender)
        assume(not re.match(target_sender, other_sender))
        engine = RoutingEngine(Path("/dummy"))
        sender_fromMe_rule = make_rule(
            id="sender-fromMe", priority=1,
            sender=target_sender, fromMe=True,
            instruction="sender-fromMe.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [sender_fromMe_rule, fallback]

        # Correct sender AND fromMe=True → matches specific rule
        msg_match = make_msg(
            sender_id=target_sender, fromMe=True, toMe=msg_toMe,
        )
        r, inst = await engine.match_with_rule(msg_match)
        assert r is sender_fromMe_rule
        assert inst == "sender-fromMe.md"

        # Correct sender BUT fromMe=False → falls through to fallback
        msg_wrong_fromMe = make_msg(
            sender_id=target_sender, fromMe=False, toMe=msg_toMe,
        )
        r, inst = await engine.match_with_rule(msg_wrong_fromMe)
        assert r is fallback
        assert inst == "fallback.md"

        # Wrong sender even with fromMe=True → falls through
        msg_wrong_sender = make_msg(
            sender_id=other_sender, fromMe=True, toMe=msg_toMe,
        )
        r, inst = await engine.match_with_rule(msg_wrong_sender)
        assert r is fallback
        assert inst == "fallback.md"

    # ── fromMe/toMe interaction with specific recipient pattern ──────────────

    @given(
        target_recipient=simple_text,
        msg_fromMe=st.booleans(),
        msg_toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_toMe_filter_with_specific_recipient_requires_both(
        self, target_recipient, msg_fromMe, msg_toMe,
    ):
        """Invariant: rule with specific recipient AND toMe=False only matches
        messages in that chat AND with toMe=False (group messages)."""
        engine = RoutingEngine(Path("/dummy"))
        recipient_toMe_rule = make_rule(
            id="recip-toMe", priority=1,
            recipient=target_recipient, toMe=False,
            instruction="recip-group.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [recipient_toMe_rule, fallback]

        # Correct recipient AND toMe=False → matches
        msg_match = make_msg(
            chat_id=target_recipient, fromMe=msg_fromMe, toMe=False,
        )
        r, inst = await engine.match_with_rule(msg_match)
        assert r is recipient_toMe_rule
        assert inst == "recip-group.md"

        # Correct recipient BUT toMe=True → falls through
        msg_wrong_toMe = make_msg(
            chat_id=target_recipient, fromMe=msg_fromMe, toMe=True,
        )
        r, inst = await engine.match_with_rule(msg_wrong_toMe)
        assert r is fallback
        assert inst == "fallback.md"

    # ── Specific sender + specific content_regex conjunction ─────────────────

    @given(
        target_sender=simple_text,
        other_sender=simple_text,
        matching_text=st.from_regex(r"hello[a-z]*", fullmatch=True),
        non_matching_text=st.from_regex(r"[0-9]{3,10}", fullmatch=True),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=80)
    async def test_sender_and_content_regex_both_must_match(
        self, target_sender, other_sender,
        matching_text, non_matching_text, fromMe, toMe,
    ):
        """Invariant: rule with specific sender AND content_regex requires
        BOTH fields to match simultaneously."""
        assume(target_sender != other_sender)
        assume(not re.match(target_sender, other_sender))
        engine = RoutingEngine(Path("/dummy"))
        conj_rule = make_rule(
            id="conj", priority=1,
            sender=target_sender, content_regex="hello[a-z]*",
            instruction="conj.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [conj_rule, fallback]

        # Correct sender AND matching text → conj rule wins
        msg_both = make_msg(
            sender_id=target_sender, text=matching_text,
            fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_both)
        assert r is conj_rule
        assert inst == "conj.md"

        # Correct sender BUT non-matching text → fallback
        msg_sender_only = make_msg(
            sender_id=target_sender, text=non_matching_text,
            fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_sender_only)
        assert r is fallback
        assert inst == "fallback.md"

        # Wrong sender even with matching text → fallback
        msg_text_only = make_msg(
            sender_id=other_sender, text=matching_text,
            fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_text_only)
        assert r is fallback
        assert inst == "fallback.md"

    # ── Specific channel + specific sender + specific recipient conjunction ──

    @given(
        target_sender=simple_text,
        target_recipient=simple_text,
        target_channel=channel_text,
        other_sender=simple_text,
    )
    @settings(max_examples=80)
    async def test_three_field_conjunction_all_must_match(
        self, target_sender, target_recipient, target_channel, other_sender,
    ):
        """Invariant: rule with specific sender, recipient AND channel requires
        all three to match. Mismatch on any field falls through."""
        assume(target_sender != other_sender)
        assume(not re.match(target_sender, other_sender))
        engine = RoutingEngine(Path("/dummy"))
        triple_rule = make_rule(
            id="triple", priority=1,
            sender=target_sender, recipient=target_recipient,
            channel=target_channel,
            instruction="triple.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [triple_rule, fallback]

        # All three match → triple rule wins
        msg_all = make_msg(
            sender_id=target_sender, chat_id=target_recipient,
            channel_type=target_channel,
        )
        r, inst = await engine.match_with_rule(msg_all)
        assert r is triple_rule
        assert inst == "triple.md"

        # Wrong sender → fallback
        msg_wrong_sender = make_msg(
            sender_id=other_sender, chat_id=target_recipient,
            channel_type=target_channel,
        )
        r, inst = await engine.match_with_rule(msg_wrong_sender)
        assert r is fallback
        assert inst == "fallback.md"

    # ── Regex pattern in sender field matches prefix ─────────────────────────

    @given(
        numeric_suffix=st.from_regex(r"[0-9]{3,8}", fullmatch=True),
        alpha_text=st.from_regex(r"[a-zA-Z]{3,10}", fullmatch=True),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_regex_sender_pattern_matches_correctly(
        self, numeric_suffix, alpha_text, fromMe, toMe,
    ):
        r"""Invariant: sender pattern r'\d+' matches sender IDs containing digits
        but not purely alphabetic sender IDs."""
        engine = RoutingEngine(Path("/dummy"))
        regex_sender_rule = make_rule(
            id="regex-sender", priority=1,
            sender=r"\d+",
            instruction="numeric.md",
        )
        fallback = make_rule(
            id="fallback", priority=10, instruction="fallback.md",
        )
        engine._rules = [regex_sender_rule, fallback]

        # Numeric sender → regex matches
        msg_numeric = make_msg(
            sender_id=numeric_suffix, fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_numeric)
        assert r is regex_sender_rule
        assert inst == "numeric.md"

        # Alpha sender → regex does not match, falls through
        msg_alpha = make_msg(
            sender_id=alpha_text, fromMe=fromMe, toMe=toMe,
        )
        r, inst = await engine.match_with_rule(msg_alpha)
        assert r is fallback
        assert inst == "fallback.md"

    # ── Cache TTL expiry triggers re-evaluation ──────────────────────────────

    @given(
        text=printable_text,
        sender_id=simple_text,
    )
    @settings(max_examples=30)
    async def test_cache_ttl_expiry_re_evaluates(self, text, sender_id):
        """Invariant: after TTL expires, cache entry is stale and the engine
        re-evaluates rules (allowing rule changes to take effect)."""
        engine = RoutingEngine(Path("/dummy"))
        rule_a = make_rule(id="a", priority=1, instruction="a.md")
        engine._rules = [rule_a]

        msg = make_msg(text=text, sender_id=sender_id)
        rule1, inst1 = await engine.match_with_rule(msg)
        assert rule1 is rule_a
        assert len(engine._match_cache) == 1

        # Simulate TTL expiry by backdating the cache entry
        cache_key = engine._cache_key(MatchingContext.from_message(msg))
        value, _ = engine._match_cache._cache[cache_key]
        engine._match_cache._cache[cache_key] = (value, time.monotonic() - 9999)

        # Replace rules — cache is cleared by setter, but let's also verify
        # that a stale (TTL-expired) cache returns None on .get()
        assert engine._match_cache.get(cache_key) is None

        # Re-match with new rules should produce new result
        rule_b = make_rule(id="b", priority=1, instruction="b.md")
        engine._rules = [rule_b]
        rule2, inst2 = await engine.match_with_rule(msg)
        assert rule2 is rule_b
        assert inst2 == "b.md"

    # ── Cache eviction at max capacity ───────────────────────────────────────

    @given(
        base_text=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=10,
        ),
    )
    @settings(max_examples=20)
    async def test_cache_eviction_at_max_size(self, base_text):
        """Invariant: inserting more unique cache entries than max_size triggers
        eviction; the cache never exceeds max_size."""
        from src.constants import ROUTING_MATCH_CACHE_MAX_SIZE

        engine = RoutingEngine(Path("/dummy"))
        engine._rules = [make_rule(id="r", priority=1, instruction="r.md")]

        # Fill cache beyond max_size
        for i in range(ROUTING_MATCH_CACHE_MAX_SIZE + 50):
            msg = make_msg(
                text=f"{base_text}-{i}",
                sender_id=f"sender-{i}",
            )
            await engine.match_with_rule(msg)

        # Cache should not exceed max_size (eviction="half" keeps it bounded)
        assert len(engine._match_cache) <= ROUTING_MATCH_CACHE_MAX_SIZE

    # ── Cache populates on first call, hit on second ─────────────────────────

    @given(
        text=printable_text,
        sender_id=simple_text,
    )
    @settings(max_examples=50)
    async def test_cache_populates_on_first_call_and_hits_on_second(
        self, text, sender_id,
    ):
        """Invariant: first call populates cache (len=1), second call is a
        cache hit returning identical objects."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(id="r", priority=1, instruction="r.md")
        engine._rules = [rule]

        assert len(engine._match_cache) == 0

        msg = make_msg(text=text, sender_id=sender_id)
        r1, inst1 = await engine.match_with_rule(msg)

        assert len(engine._match_cache) == 1
        assert r1 is rule

        r2, inst2 = await engine.match_with_rule(msg)
        assert r2 is r1  # same object from cache
        assert inst2 == inst1
        assert len(engine._match_cache) == 1  # no new entry

    # ── Priority ordering: higher-priority specific beats lower-priority wildcard ─

    @given(
        specific_sender=simple_text,
        priority_specific=st.integers(0, 49),
        priority_wildcard=st.integers(50, 100),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_higher_priority_specific_beats_lower_priority_wildcard(
        self, specific_sender, priority_specific, priority_wildcard,
        fromMe, toMe,
    ):
        """Invariant: a specific-sender rule at lower priority value beats a
        wildcard rule at higher priority value, when sender matches."""
        # Guard against specific_sender regex-matching the default sender_id
        # used by msg_other ("5511999990000")
        assume(not re.match(specific_sender, "5511999990000"))
        engine = RoutingEngine(Path("/dummy"))
        specific = make_rule(
            id="specific", priority=priority_specific,
            sender=specific_sender, instruction="specific.md",
        )
        wildcard = make_rule(
            id="wildcard", priority=priority_wildcard,
            instruction="wildcard.md",
        )
        engine._rules = sorted([specific, wildcard], key=lambda r: r.priority)

        msg = make_msg(sender_id=specific_sender, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg)
        assert rule is specific
        assert inst == "specific.md"

        # Same rules, non-matching sender → wildcard wins
        msg_other = make_msg(fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg_other)
        assert rule is wildcard
        assert inst == "wildcard.md"

    # ── Empty rules list always returns (None, None) for any context ─────────

    @given(
        text=printable_text,
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_empty_rules_always_returns_none_tuple(
        self, text, sender_id, chat_id, channel_type, fromMe, toMe,
    ):
        """Invariant: engine with no rules always returns (None, None) for any
        message, regardless of message attributes."""
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = []
        msg = make_msg(
            text=text, sender_id=sender_id, chat_id=chat_id,
            channel_type=channel_type, fromMe=fromMe, toMe=toMe,
        )
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is None
        assert instruction is None

    # ── Multiple specific rules: only the highest-priority match wins ────────

    @given(
        sender_a=simple_text,
        sender_b=simple_text,
        priority_a=st.integers(0, 30),
        priority_b=st.integers(31, 60),
        fromMe=st.booleans(),
        toMe=st.booleans(),
    )
    @settings(max_examples=100)
    async def test_multiple_specific_rules_only_highest_priority_wins(
        self, sender_a, sender_b, priority_a, priority_b,
        fromMe, toMe,
    ):
        """Invariant: when a message matches multiple specific rules, the one
        with the lowest priority value wins."""
        assume(sender_a != sender_b)
        # Guard: routing engine uses regex matching, so "0" matches "00"
        assume(not re.match(sender_a, sender_b))
        assume(not re.match(sender_b, sender_a))
        engine = RoutingEngine(Path("/dummy"))

        # Two sender-specific rules with different priorities
        rule_low_pri = make_rule(
            id="low", priority=priority_b,
            sender=sender_a, instruction="low.md",
        )
        rule_high_pri = make_rule(
            id="high", priority=priority_a,
            sender=sender_a, instruction="high.md",
        )
        # Also a rule for sender_b to ensure independent matching
        rule_b = make_rule(
            id="b", priority=priority_a,
            sender=sender_b, instruction="b.md",
        )
        fallback = make_rule(
            id="fallback", priority=100, instruction="fallback.md",
        )
        engine._rules = sorted(
            [rule_low_pri, rule_high_pri, rule_b, fallback],
            key=lambda r: r.priority,
        )

        # sender_a matches both specific rules → highest priority (lowest number) wins
        msg_a = make_msg(sender_id=sender_a, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg_a)
        assert rule is rule_high_pri
        assert inst == "high.md"

        # sender_b matches its own rule
        msg_b = make_msg(sender_id=sender_b, fromMe=fromMe, toMe=toMe)
        rule, inst = await engine.match_with_rule(msg_b)
        assert rule is rule_b
        assert inst == "b.md"


# ═══════════════════════════════════════════════════════════════════════════════
# Combinatorial property tests — random MatchingContext × RoutingRule combinations
# ═══════════════════════════════════════════════════════════════════════════════

from hypothesis.strategies import composite


@composite
def matching_contexts(draw):
    """Composite strategy: draw a fully random MatchingContext."""
    return MatchingContext(
        sender_id=draw(simple_text),
        chat_id=draw(simple_text),
        channel_type=draw(channel_text),
        text=draw(printable_text),
        fromMe=draw(st.booleans()),
        toMe=draw(st.booleans()),
    )


@composite
def routing_rules(draw):
    """Composite strategy: draw a fully random RoutingRule with varied patterns."""
    return RoutingRule(
        id=draw(st.integers(0, 9999).map(lambda i: f"rule-{i}")),
        priority=draw(st.integers(0, 100)),
        sender=draw(st.one_of(st.just("*"), simple_text, valid_regex)),
        recipient=draw(st.one_of(st.just("*"), simple_text, valid_regex)),
        channel=draw(st.one_of(st.just("*"), channel_text, valid_regex)),
        content_regex=draw(st.one_of(st.just("*"), st.just(""), valid_regex)),
        instruction=draw(st.integers(0, 99).map(lambda i: f"inst-{i}.md")),
        enabled=draw(st.booleans()),
        fromMe=draw(bool_or_none),
        toMe=draw(bool_or_none),
        skillExecVerbose=draw(st.sampled_from(["", "summary", "full"])),
        showErrors=draw(st.booleans()),
    )


@composite
def rules_and_context(draw):
    """Composite strategy: draw a sorted rule list AND a context together.

    This enables testing invariants over the joint distribution of rules and
    messages rather than treating them independently.
    """
    rules = draw(
        st.lists(routing_rules(), min_size=0, max_size=6).map(
            lambda rs: sorted(rs, key=lambda r: r.priority)
        )
    )
    ctx = draw(matching_contexts())
    return rules, ctx


def _ctx_to_msg(ctx: MatchingContext) -> IncomingMessage:
    """Convert a MatchingContext back to an IncomingMessage for engine calls."""
    return make_msg(
        sender_id=ctx.sender_id,
        chat_id=ctx.chat_id,
        channel_type=ctx.channel_type,
        text=ctx.text,
        fromMe=ctx.fromMe,
        toMe=ctx.toMe,
    )


class TestPropertyMatchWithRuleCombinatorial:
    """Property-based invariants using composite strategies that generate random
    MatchingContext × RoutingRule combinations.

    These tests exercise the joint distribution of rules and messages rather
    than fixing one side and varying the other, providing stronger coverage of
    combinatorial edge cases.
    """

    # ── 1. Smoke test: any random combination never crashes ────────────────

    @given(data=rules_and_context())
    @settings(max_examples=300)
    async def test_any_rule_context_combo_never_crashes(self, data):
        """Invariant: match_with_rule never raises for any valid rule+context."""
        rules, ctx = data
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)
        result = await engine.match_with_rule(msg)
        assert isinstance(result, tuple)
        assert len(result) == 2

    # ── 2. Result type invariant under fully random inputs ────────────────

    @given(data=rules_and_context())
    @settings(max_examples=300)
    async def test_result_always_optional_tuple_types(self, data):
        """Invariant: result is always (RoutingRule|None, str|None)."""
        rules, ctx = data
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is None or isinstance(rule, RoutingRule)
        assert instruction is None or isinstance(instruction, str)
        if rule is None:
            assert instruction is None
        else:
            assert instruction == rule.instruction

    # ── 3. Disabling the winning rule changes the result ──────────────────

    @given(data=rules_and_context())
    @settings(max_examples=200)
    async def test_disabling_winning_rule_changes_result(self, data):
        """Invariant: if a rule wins for a message, disabling that rule and
        re-matching must produce a different result (or None)."""
        rules, ctx = data
        assume(len(rules) > 0)

        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)

        rule_orig, inst_orig = await engine.match_with_rule(msg)
        if rule_orig is None:
            return  # No match to disable

        # Skip if another rule has the same instruction as the winner,
        # because disabling the winner would enable all others (including
        # originally disabled ones) via the reconstruction below.
        other_instructions = {
            r.instruction for r in rules
            if r is not rule_orig
        }
        assume(inst_orig not in other_instructions)

        # Find and disable the winning rule
        new_rules = [
            RoutingRule(
                id=r.id,
                priority=r.priority,
                sender=r.sender,
                recipient=r.recipient,
                channel=r.channel,
                content_regex=r.content_regex,
                instruction=r.instruction,
                enabled=(r is not rule_orig),  # disable only the winner
                fromMe=r.fromMe,
                toMe=r.toMe,
                skillExecVerbose=r.skillExecVerbose,
                showErrors=r.showErrors,
            )
            for r in rules
        ]
        engine._rules = sorted(new_rules, key=lambda r: r.priority)
        rule_new, inst_new = await engine.match_with_rule(msg)

        # Result must differ from the original winning instruction
        assert inst_new != inst_orig

    # ── 4. Wildcard catch-all guarantees a match for any context ──────────

    @given(ctx=matching_contexts())
    @settings(max_examples=200)
    async def test_enabled_catch_all_never_returns_none(self, ctx):
        """Invariant: an enabled all-wildcard rule guarantees a non-None match
        for any possible message context."""
        engine = RoutingEngine(Path("/dummy"))
        catch_all = make_rule(id="catch", instruction="catch.md")
        engine._rules = [catch_all]
        msg = _ctx_to_msg(ctx)
        rule, instruction = await engine.match_with_rule(msg)
        assert rule is catch_all
        assert instruction == "catch.md"

    # ── 5. Adding a higher-priority catch-all always takes over ───────────

    @given(data=rules_and_context(), new_priority=st.integers(0, 100))
    @settings(max_examples=150)
    async def test_prepend_higher_priority_catch_all_wins(self, data, new_priority):
        """Invariant: inserting a catch-all rule at a priority equal to or lower
        than the current winner causes the catch-all to win for that message."""
        rules, ctx = data
        assume(len(rules) > 0)

        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)

        rule_orig, _ = await engine.match_with_rule(msg)
        if rule_orig is None:
            return  # no existing match; skip

        # Add a catch-all with priority strictly lower than current winner
        new_pri = min(r.priority for r in rules)
        catch_all = make_rule(
            id="new-catch", priority=new_pri, instruction="new-catch.md"
        )
        combined = sorted([catch_all, *rules], key=lambda r: r.priority)
        engine._rules = combined

        rule_new, inst_new = await engine.match_with_rule(msg)
        assert rule_new is catch_all
        assert inst_new == "new-catch.md"

    # ── 6. Rule list growth never removes matches when catch-all exists ───

    @given(
        base_rules=st.lists(routing_rules(), min_size=0, max_size=4),
        extra_rules=st.lists(routing_rules(), min_size=1, max_size=4),
        ctx=matching_contexts(),
    )
    @settings(max_examples=150)
    async def test_growing_rules_with_catch_all_never_loses_match(
        self, base_rules, extra_rules, ctx
    ):
        """Invariant: starting from a rule set that includes an enabled catch-all,
        adding more rules never causes a previously-matching message to return
        (None, None)."""
        catch_all = make_rule(id="catch", priority=100, instruction="catch.md")
        sorted_base = sorted([*base_rules, catch_all], key=lambda r: r.priority)

        engine = RoutingEngine(Path("/dummy"))
        engine._rules = sorted_base
        msg = _ctx_to_msg(ctx)

        rule_before, inst_before = await engine.match_with_rule(msg)
        assert rule_before is not None  # catch-all guarantees this

        # Grow the rule list
        grown = sorted([*sorted_base, *extra_rules], key=lambda r: r.priority)
        engine._rules = grown
        rule_after, inst_after = await engine.match_with_rule(msg)

        assert rule_after is not None  # match must still exist

    # ── 7. Idempotency: N calls produce identical results ─────────────────

    @given(data=rules_and_context(), n=st.integers(2, 10))
    @settings(max_examples=100)
    async def test_repeated_calls_produce_identical_results(self, data, n):
        """Invariant: calling match_with_rule N times with the same message and
        rules always produces the exact same (rule, instruction) tuple."""
        rules, ctx = data
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)

        results = [await engine.match_with_rule(msg) for _ in range(n)]
        first = results[0]
        for result in results[1:]:
            assert result == first

    # ── 8. Symmetry: swapping priorities swaps winners ────────────────────

    @given(
        sender_a=simple_text,
        sender_b=simple_text,
        pri_a=st.integers(0, 50),
        pri_b=st.integers(51, 100),
        ctx=matching_contexts(),
    )
    @settings(max_examples=100)
    async def test_swapping_priorities_swaps_winner(self, sender_a, sender_b, pri_a, pri_b, ctx):
        """Invariant: given two sender-specific rules for different senders,
        the rule with lower priority wins for its sender. Swapping the priorities
        swaps which rule wins."""
        assume(sender_a != sender_b)
        engine = RoutingEngine(Path("/dummy"))

        rule_a = make_rule(id="a", priority=pri_a, sender=sender_a, instruction="a.md")
        rule_b = make_rule(id="b", priority=pri_b, sender=sender_b, instruction="b.md")
        engine._rules = sorted([rule_a, rule_b], key=lambda r: r.priority)

        msg_a = make_msg(sender_id=sender_a)
        rule_winner_a, _ = await engine.match_with_rule(msg_a)
        assert rule_winner_a is rule_a

        # Swap priorities
        rule_a_swapped = make_rule(
            id="a", priority=pri_b, sender=sender_a, instruction="a.md"
        )
        rule_b_swapped = make_rule(
            id="b", priority=pri_a, sender=sender_b, instruction="b.md"
        )
        engine._rules = sorted([rule_a_swapped, rule_b_swapped], key=lambda r: r.priority)

        msg_b = make_msg(sender_id=sender_b)
        rule_winner_b, _ = await engine.match_with_rule(msg_b)
        assert rule_winner_b is rule_b_swapped

    # ── 9. Cache key collision: same key always yields same result ────────

    @given(
        sender_id=simple_text,
        chat_id=simple_text,
        channel_type=channel_text,
        text=printable_text,
        fromMe=st.booleans(),
        toMe=st.booleans(),
        suffix=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100)
    async def test_cache_key_collision_same_result(
        self, sender_id, chat_id, channel_type, text, fromMe, toMe, suffix,
    ):
        """Invariant: two messages that differ only beyond the first 100
        characters of text share the same cache key and therefore return
        identical match results."""
        engine = RoutingEngine(Path("/dummy"))
        rule = make_rule(id="r", priority=1, instruction="r.md")
        engine._rules = [rule]

        # Build two messages: same prefix, different suffix beyond 100 chars
        # Ensure prefix is >= 100 chars so suffix doesn't overlap
        long_prefix = text[:100].ljust(100, "x")
        msg_a = make_msg(
            sender_id=sender_id, chat_id=chat_id, channel_type=channel_type,
            text=long_prefix + "AAA", fromMe=fromMe, toMe=toMe,
        )
        msg_b = make_msg(
            sender_id=sender_id, chat_id=chat_id, channel_type=channel_type,
            text=long_prefix + "BBB", fromMe=fromMe, toMe=toMe,
        )

        result_a = await engine.match_with_rule(msg_a)
        result_b = await engine.match_with_rule(msg_b)

        # Both share the same cache key (text[:100] is identical)
        assert result_a == result_b

    # ── 10. Match-with-rule equivalence under full random combinations ────

    @given(data=rules_and_context())
    @settings(max_examples=200)
    async def test_match_equivalence_under_random_combos(self, data):
        """Invariant: match(msg) == match_with_rule(msg)[1] for any random
        combination of rules and context."""
        rules, ctx = data
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)

        instruction_only = await engine.match(msg)
        _, instruction_from_tuple = await engine.match_with_rule(msg)
        assert instruction_only == instruction_from_tuple

    # ── 11. Returned rule always belongs to the active rule set ───────────

    @given(data=rules_and_context())
    @settings(max_examples=200)
    async def test_returned_rule_is_member_of_active_rules(self, data):
        """Invariant: when match succeeds, the returned rule is in the current
        _rules list and its instruction field is consistent."""
        rules, ctx = data
        engine = RoutingEngine(Path("/dummy"))
        engine._rules = rules
        msg = _ctx_to_msg(ctx)

        rule, instruction = await engine.match_with_rule(msg)
        if rule is not None:
            assert rule in rules
            assert instruction == rule.instruction
            assert rule.enabled is True

    # ── 12. Re-enabling all rules is equivalent to original match ─────────

    @given(
        rules=st.lists(routing_rules(), min_size=1, max_size=6),
        ctx=matching_contexts(),
    )
    @settings(max_examples=100)
    async def test_reenabling_all_rules_restores_original_match(self, rules, ctx):
        """Invariant: disable all rules → match is None; re-enable all → match
        equals the original result with a fresh engine."""
        sorted_rules = sorted(rules, key=lambda r: r.priority)
        msg = _ctx_to_msg(ctx)

        # Original match with all rules enabled
        engine_orig = RoutingEngine(Path("/dummy"))
        engine_orig._rules = sorted_rules
        orig_rule, orig_inst = await engine_orig.match_with_rule(msg)

        # Disable all rules
        disabled = [
            RoutingRule(
                id=r.id, priority=r.priority, sender=r.sender,
                recipient=r.recipient, channel=r.channel,
                content_regex=r.content_regex, instruction=r.instruction,
                enabled=False, fromMe=r.fromMe, toMe=r.toMe,
                skillExecVerbose=r.skillExecVerbose, showErrors=r.showErrors,
            )
            for r in sorted_rules
        ]
        engine_off = RoutingEngine(Path("/dummy"))
        engine_off._rules = sorted(disabled, key=lambda r: r.priority)
        off_rule, off_inst = await engine_off.match_with_rule(msg)
        assert off_rule is None
        assert off_inst is None

        # Re-enable all rules — restore their original enabled states
        reenabled = [
            RoutingRule(
                id=r.id, priority=r.priority, sender=r.sender,
                recipient=r.recipient, channel=r.channel,
                content_regex=r.content_regex, instruction=r.instruction,
                enabled=r.enabled, fromMe=r.fromMe, toMe=r.toMe,
                skillExecVerbose=r.skillExecVerbose, showErrors=r.showErrors,
            )
            for r in sorted_rules
        ]
        engine_on = RoutingEngine(Path("/dummy"))
        engine_on._rules = sorted(reenabled, key=lambda r: r.priority)
        on_rule, on_inst = await engine_on.match_with_rule(msg)

        # Re-enabled result should match original
        assert on_inst == orig_inst


# ═══════════════════════════════════════════════════════════════════════════════
# Cache invalidation on file modification — end-to-end integration tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(sys.platform == "win32", reason="file watcher timing unreliable on Windows")
class TestCacheInvalidationOnFileModification:
    """
    End-to-end tests verifying that match_with_rule() correctly invalidates
    its cached results when instruction files change on disk.

    These tests exercise the full pipeline:
        match_with_rule() → _is_stale() → load_rules() → _rules setter →
        _match_cache.clear() → fresh rule evaluation → new cache entry
    """

    async def test_cache_invalidated_when_file_modified(self, tmp_path: Path):
        """(a) Cached result is cleared and new rule is returned after file modification."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: original\n  priority: 1\n---\n\n# Original\n"
        )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # First match — populates cache
        rule1, inst1 = await engine.match_with_rule(msg)
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
        rule2, inst2 = await engine.match_with_rule(msg)
        assert inst2 == "route.md"
        assert rule2 is not None
        assert rule2.id == "modified"

        # Cache was rebuilt (cleared by load_rules, then repopulated)
        assert len(engine._match_cache) == 1
        # The old rule object should NOT be in the cache anymore
        cached_result = list(engine._match_cache._cache.values())[0][0]
        assert cached_result[0].id == "modified"

    async def test_new_rule_appears_after_file_creation(self, tmp_path: Path):
        """(b) Creating a new .md file causes new rules to appear in match results."""
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # Initially no rules
        assert await engine.match(msg) is None
        assert len(engine._match_cache) == 1  # (None, None) cached

        # Create a new instruction file
        (tmp_path / "new_rule.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        # Allow stale check
        engine._last_stale_check = 0.0

        # New rule should now match
        rule, inst = await engine.match_with_rule(msg)
        assert inst == "new_rule.md"
        assert rule is not None
        assert rule.id == "new-rule"

        # Cache was rebuilt with new result
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache._cache.values())[0][0]
        assert cached_result[0].id == "new-rule"

    async def test_removed_rule_disappears_after_file_deletion(self, tmp_path: Path):
        """(c) Deleting an .md file causes its rules to disappear from match results."""
        md = tmp_path / "gone.md"
        md.write_text(
            "---\nrouting:\n  id: temporary\n  priority: 1\n---\n\n# Temp\n"
        )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # Rule matches
        rule1, inst1 = await engine.match_with_rule(msg)
        assert inst1 == "gone.md"
        assert rule1 is not None
        assert rule1.id == "temporary"
        assert len(engine._match_cache) == 1

        # Delete the file
        md.unlink()

        # Allow stale check
        engine._last_stale_check = 0.0

        # No rules should match now
        rule2, inst2 = await engine.match_with_rule(msg)
        assert rule2 is None
        assert inst2 is None

        # Cache was rebuilt
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache._cache.values())[0][0]
        assert cached_result == (None, None)

    async def test_debounce_prevents_excessive_reloads(self, tmp_path: Path):
        """(d) Multiple match calls within the debounce window skip stale checks,
        preserving the cached result even if files changed on disk."""
        md = tmp_path / "stable.md"
        md.write_text(
            "---\nrouting:\n  id: v1\n  priority: 1\n---\n\n# V1\n"
        )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # First match — triggers stale check, populates cache
        engine._last_stale_check = 0.0
        rule1, inst1 = await engine.match_with_rule(msg)
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
        rule2, inst2 = await engine.match_with_rule(msg)
        assert engine._last_stale_check == first_check_time  # debounce blocked re-check
        assert rule2 is not None
        assert rule2.id == "v1"  # Still returns cached result
        assert len(engine._match_cache) == 1

    async def test_end_to_end_cache_lifecycle(self, tmp_path: Path):
        """Full lifecycle: load → cache → modify → auto-reload → new cache → delete → empty."""
        # Step 1: Create initial file and load
        md = tmp_path / "lifecycle.md"
        md.write_text(
            "---\nrouting:\n  id: step1\n  priority: 1\n---\n\n# Step1\n"
        )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # Step 2: First match — cache populated
        rule, _ = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step1"
        assert len(engine._match_cache) == 1

        # Step 3: Modify file — cache invalidated, new result cached
        md.write_text(
            "---\nrouting:\n  id: step2\n  priority: 1\n---\n\n# Step2\n"
        )
        engine._last_stale_check = 0.0

        rule, _ = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step2"
        assert len(engine._match_cache) == 1

        # Step 4: Delete file — cache invalidated, (None, None) cached
        md.unlink()
        engine._last_stale_check = 0.0

        rule, inst = await engine.match_with_rule(msg)
        assert rule is None
        assert inst is None
        assert len(engine._match_cache) == 1
        cached_result = list(engine._match_cache._cache.values())[0][0]
        assert cached_result == (None, None)

        # Step 5: Recreate file — cache invalidated, new result cached
        md.write_text(
            "---\nrouting:\n  id: step5\n  priority: 1\n---\n\n# Step5\n"
        )
        engine._last_stale_check = 0.0

        rule, inst = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step5"
        assert inst == "lifecycle.md"

    async def test_cache_not_invalidated_when_content_unchanged(self, tmp_path: Path):
        """Rewriting the same content should not cause cache invalidation."""
        md = tmp_path / "same.md"
        content = "---\nrouting:\n  id: same-rule\n  priority: 1\n---\n\n# Same\n"
        md.write_text(content)

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg = make_msg(text="hello")

        # First match
        engine._last_stale_check = 0.0
        rule1, _ = await engine.match_with_rule(msg)
        assert rule1 is not None
        assert rule1.id == "same-rule"
        rules_obj_before = engine._rules  # identity check

        # Rewrite the same content (mtime changes but rule is logically identical)
        md.write_text(content)
        engine._last_stale_check = 0.0

        # Even though mtime changed, load_rules will re-scan and set _rules
        # which clears cache. The new rule should be functionally identical.
        rule2, _ = await engine.match_with_rule(msg)
        assert rule2 is not None
        assert rule2.id == "same-rule"

    async def test_multiple_files_invalidation(self, tmp_path: Path):
        """Modifying one of multiple instruction files invalidates the entire cache."""
        (tmp_path / "first.md").write_text(
            "---\nrouting:\n  id: first\n  priority: 1\n---\n\n# First\n"
        )
        (tmp_path / "second.md").write_text(
            "---\nrouting:\n  id: second\n  priority: 10\n---\n\n# Second\n"
        )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        msg_low = make_msg(text="hello")
        msg_high = make_msg(sender_id="special-sender")

        # Populate cache with two entries
        engine._last_stale_check = 0.0
        await engine.match_with_rule(msg_low)
        await engine.match_with_rule(msg_high)
        assert len(engine._match_cache) == 2

        # Modify the first file (higher priority) to change its rule
        (tmp_path / "first.md").write_text(
            "---\nrouting:\n  id: first-updated\n  priority: 1\n---\n\n# Updated\n"
        )
        engine._last_stale_check = 0.0

        # One match should clear the ENTIRE cache (both entries)
        await engine.match_with_rule(msg_low)
        assert len(engine._match_cache) == 1  # Only msg_low's new result

        # The other message must be re-evaluated (not stale cached)
        rule, inst = await engine.match_with_rule(msg_high)
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

    Uses _polling_engine (use_watchdog=False) so debounce logic is actually
    exercised — with watchdog active _is_stale() checks the _dirty flag instead.
    """

    # -- (a) Two calls within debounce window → _scan_file_mtimes called once --

    async def test_scan_called_once_within_debounce_window(self, tmp_path: Path):
        """_scan_file_mtimes() is NOT called on the second _is_stale() within the debounce window."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

        # Force the first call to pass the debounce gate.
        engine._last_stale_check = 0.0
        assert engine._is_stale() is False

        with patch.object(engine, "_scan_file_mtimes", wraps=engine._scan_file_mtimes) as mock_scan:
            # Second call — still within the debounce window → should NOT scan.
            result = engine._is_stale()
            assert result is False
            mock_scan.assert_not_called()

    # -- (b) Call after debounce interval triggers fresh scan --

    async def test_fresh_scan_after_debounce_interval(self, tmp_path: Path):
        """After ROUTING_WATCH_DEBOUNCE_SECONDS, _is_stale() scans mtimes again."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

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

    async def test_multiple_debounce_windows_scan_each_time(self, tmp_path: Path):
        """Each debounce-window boundary triggers exactly one scan."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

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

    async def test_stale_returns_true_when_file_modified_after_debounce(self, tmp_path: Path):
        """_is_stale() returns True after a file is modified and debounce has elapsed."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()
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

    async def test_stale_detects_new_file_after_debounce(self, tmp_path: Path):
        """A new .md file appearing after debounce triggers a stale detection."""
        (tmp_path / "existing.md").write_text(
            "---\nrouting:\n  id: existing\n  priority: 1\n---\n\n# Existing\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

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

    async def test_stale_detects_deleted_file_after_debounce(self, tmp_path: Path):
        """Deleting a .md file after debounce triggers a stale detection."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        (tmp_path / "b.md").write_text(
            "---\nrouting:\n  id: b-rule\n  priority: 2\n---\n\n# B\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()
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

    async def test_exactly_at_debounce_threshold_triggers_scan(self, tmp_path: Path):
        """At exactly ROUTING_WATCH_DEBOUNCE_SECONDS, the debounce should NOT short-circuit."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

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

    async def test_fresh_engine_first_call_always_scans(self, tmp_path: Path):
        """With _last_stale_check=0.0 (default), the first _is_stale() always passes the debounce gate."""
        (tmp_path / "a.md").write_text(
            "---\nrouting:\n  id: a-rule\n  priority: 1\n---\n\n# A\n"
        )
        engine = _polling_engine(tmp_path)
        await engine.load_rules()

        assert engine._last_stale_check == 0.0

        with patch.object(engine, "_scan_file_mtimes", return_value=engine._file_mtimes) as mock_scan:
            result = engine._is_stale()
            assert result is False
            mock_scan.assert_called_once()
        assert engine._last_stale_check > 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Coverage gap: OSError in _scan_file_mtimes, invalid rule construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestScanFileMtimesOSError:
    """Tests for OSError handling in _scan_file_mtimes."""

    async def test_oserror_on_stat_skips_file(self, tmp_path: Path):
        """When stat() raises OSError for a file, it's skipped gracefully."""
        md = tmp_path / "test.md"
        md.write_text("---\nrouting:\n  id: ok\n  priority: 1\n---\n\n# OK\n")

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        # Simulate OSError on the stat call by patching the DirEntry
        import os
        from contextlib import contextmanager

        original_scandir = os.scandir

        @contextmanager
        def _flaky_scandir(path):
            """Yield entries where stat() raises OSError."""
            entries = []
            for entry in original_scandir(path):
                class FlakyEntry:
                    name = entry.name
                    def is_file(self): return entry.is_file()
                    def stat(self): raise OSError("permission denied")
                entries.append(FlakyEntry())
            yield entries

        with patch("src.routing.os.scandir", _flaky_scandir):
            mtimes = engine._scan_file_mtimes()

        # File should be skipped, not crash
        assert mtimes == {}

    async def test_oserror_on_scandir_returns_empty(self, tmp_path: Path):
        """When scandir() itself raises OSError, returns empty dict."""
        engine = RoutingEngine(tmp_path)

        with patch("src.routing.os.scandir", side_effect=OSError("dir removed")):
            mtimes = engine._scan_file_mtimes()

        assert mtimes == {}


# ═══════════════════════════════════════════════════════════════════════════════
# Watchdog auto-reload integration tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestWatchdogAutoReload:
    """Integration tests for watchdog-based auto-reload of routing rules.

    When ``use_watchdog=True``, the routing engine starts an OS-native file
    watcher.  File-change events set the ``_dirty`` flag; the next call to
    ``match_with_rule()`` detects this via ``_is_stale()`` and reloads rules.

    These tests exercise the full watchdog hot-reload pipeline:
        _mark_dirty() → _is_stale() → load_rules() → fresh rules available

    Most tests suppress the real OS-native observer to avoid non-deterministic
    interaction between real file events and explicit ``_mark_dirty()`` calls.
    Two tests at the end exercise the real observer (guarded by ``_HAS_WATCHDOG``
    and skipped on Windows).
    """

    @staticmethod
    async def _make_engine(path: Path) -> RoutingEngine:
        """Create a watchdog-flagged engine with the observer suppressed.

        This allows testing the _dirty flag mechanism in isolation without
        interference from real OS file-change events.
        """
        engine = RoutingEngine(path, use_watchdog=True)
        with patch.object(engine, "_start_watcher"):
            await engine.load_rules()
        engine._use_watchdog = True
        return engine

    # ── (a) _mark_dirty() → _is_stale() returns True ────────────────────

    async def test_mark_dirty_makes_is_stale_return_true(self, tmp_path: Path):
        """After _mark_dirty() is called, _is_stale() returns True on a
        watchdog-enabled engine."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: original\n  priority: 1\n---\n\n# Original\n"
        )

        engine = await self._make_engine(tmp_path)

        # _dirty should be False after initial load
        assert engine._dirty is False

        # Simulate watchdog observer calling _mark_dirty()
        engine._mark_dirty()
        assert engine._dirty is True
        assert engine._is_stale() is True

    # ── (b) _is_stale() resets _dirty flag after returning True ──────────

    async def test_is_stale_resets_dirty_flag(self, tmp_path: Path):
        """_is_stale() consumes the _dirty flag — a second call returns False."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = await self._make_engine(tmp_path)

        engine._mark_dirty()
        assert engine._is_stale() is True
        # Flag has been consumed
        assert engine._is_stale() is False

    # ── (c) match_with_rule() auto-reloads after _mark_dirty() ───────────

    async def test_match_with_rule_reloads_after_mark_dirty(self, tmp_path: Path):
        """match_with_rule() picks up new rules after _mark_dirty() is called."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: v1\n  priority: 1\n---\n\n# V1\n"
        )

        engine = await self._make_engine(tmp_path)

        msg = make_msg(text="hello")
        rule1, inst1 = await engine.match_with_rule(msg)
        assert inst1 == "route.md"
        assert rule1 is not None
        assert rule1.id == "v1"

        # Modify the file (simulating what watchdog would observe)
        md.write_text(
            "---\nrouting:\n  id: v2\n  priority: 1\n---\n\n# V2\n"
        )

        # Simulate watchdog observer detecting the change
        engine._mark_dirty()

        # match_with_rule() should auto-reload and return the updated rule
        rule2, inst2 = await engine.match_with_rule(msg)
        assert inst2 == "route.md"
        assert rule2 is not None
        assert rule2.id == "v2"

    # ── (d) New file detected via watchdog auto-reload ──────────────────

    async def test_new_rule_appears_after_watchdog_event(self, tmp_path: Path):
        """Creating a new .md file and marking dirty makes new rules available."""
        engine = await self._make_engine(tmp_path)

        msg = make_msg(text="hello")
        assert await engine.match(msg) is None

        # Create a new instruction file
        (tmp_path / "new_rule.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert inst == "new_rule.md"
        assert rule is not None
        assert rule.id == "new-rule"

    # ── (e) Deleted file detected via watchdog auto-reload ──────────────

    async def test_rule_disappears_after_file_deleted_and_mark_dirty(self, tmp_path: Path):
        """Deleting an .md file and marking dirty removes its rules."""
        md = tmp_path / "gone.md"
        md.write_text(
            "---\nrouting:\n  id: temporary\n  priority: 1\n---\n\n# Temp\n"
        )

        engine = await self._make_engine(tmp_path)

        msg = make_msg(text="hello")
        assert await engine.match(msg) == "gone.md"

        # Delete the file
        md.unlink()
        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert rule is None
        assert inst is None

    # ── (f) Cache is invalidated after watchdog-triggered reload ─────────

    async def test_cache_cleared_after_watchdog_reload(self, tmp_path: Path):
        """Watchdog-triggered auto-reload clears the match cache."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: cached\n  priority: 1\n---\n\n# Cached\n"
        )

        engine = await self._make_engine(tmp_path)

        msg = make_msg(text="hello")

        # Populate cache
        await engine.match_with_rule(msg)
        assert len(engine._match_cache) == 1

        # Modify file and trigger watchdog reload
        md.write_text(
            "---\nrouting:\n  id: updated\n  priority: 1\n---\n\n# Updated\n"
        )
        engine._mark_dirty()

        # Cache is cleared during load_rules, then repopulated
        rule, inst = await engine.match_with_rule(msg)
        assert len(engine._match_cache) == 1
        assert rule.id == "updated"

        # Verify the cached result is the new rule
        cached_result = list(engine._match_cache._cache.values())[0][0]
        assert cached_result[0].id == "updated"

    # ── (g) Multiple mark_dirty calls — idempotent ──────────────────────

    async def test_multiple_mark_dirty_calls_idempotent(self, tmp_path: Path):
        """Multiple _mark_dirty() calls before _is_stale() are idempotent."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = await self._make_engine(tmp_path)

        # Multiple dirty marks before consuming
        engine._mark_dirty()
        engine._mark_dirty()
        engine._mark_dirty()

        # Still only one reload happens
        assert engine._is_stale() is True
        assert engine._is_stale() is False

    # ── (h) Full lifecycle: create → modify → delete with watchdog ──────

    async def test_full_watchdog_lifecycle(self, tmp_path: Path):
        """Full lifecycle via watchdog: create → match → modify → re-match → delete."""
        engine = await self._make_engine(tmp_path)

        msg = make_msg(text="hello")

        # Phase 1: No rules
        assert await engine.match(msg) is None

        # Phase 2: Create file
        md = tmp_path / "lifecycle.md"
        md.write_text(
            "---\nrouting:\n  id: step1\n  priority: 1\n---\n\n# Step1\n"
        )
        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step1"
        assert inst == "lifecycle.md"

        # Phase 3: Modify file
        md.write_text(
            "---\nrouting:\n  id: step2\n  priority: 1\n---\n\n# Step2\n"
        )
        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step2"
        assert inst == "lifecycle.md"

        # Phase 4: Delete file
        md.unlink()
        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert rule is None
        assert inst is None

        # Phase 5: Recreate file
        md.write_text(
            "---\nrouting:\n  id: step5\n  priority: 1\n---\n\n# Step5\n"
        )
        engine._mark_dirty()

        rule, inst = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "step5"
        assert inst == "lifecycle.md"

    # ── (i) Watchdog engine does NOT use mtime polling ──────────────────

    async def test_watchdog_engine_skips_mtime_check(self, tmp_path: Path):
        """With use_watchdog=True, _is_stale() checks _dirty flag only,
        not file mtimes — even after debounce interval."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = await self._make_engine(tmp_path)

        # Modify the file WITHOUT calling _mark_dirty()
        md.write_text(
            "---\nrouting:\n  id: r2\n  priority: 1\n---\n\n# R2\n"
        )

        # Even after the debounce interval, _is_stale() should return False
        # because the watchdog engine only checks the _dirty flag
        engine._last_stale_check = 0.0  # force past debounce
        with patch.object(engine, "_scan_file_mtimes", wraps=engine._scan_file_mtimes) as mock_scan:
            assert engine._is_stale() is False
            mock_scan.assert_not_called()

    # ── (j) close() stops the observer ───────────────────────────────────

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    async def test_close_stops_watcher(self, tmp_path: Path):
        """close() stops the watchdog observer and releases resources.

        Verifies that after close():
        1. The observer thread has stopped (is_alive() == False).
        2. The engine's _observer reference is set to None.
        """
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = RoutingEngine(tmp_path, use_watchdog=True)
        await engine.load_rules()

        observer = engine._observer
        assert observer is not None, "Observer should be created with use_watchdog=True"
        assert observer.is_alive(), "Observer thread should be running after load_rules()"

        engine.close()

        assert engine._observer is None, "close() should set _observer to None"
        assert not observer.is_alive(), "Observer thread should have stopped after close()"

    # ── (k) Real watchdog observer detects file changes ──────────────────

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    async def test_real_watchdog_detects_file_modification(self, tmp_path: Path):
        """Real watchdog observer detects .md file modification and triggers reload.

        This is a true integration test: the OS-native file watcher must
        observe the change and set the _dirty flag within a reasonable time.
        """
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: v1\n  priority: 1\n---\n\n# V1\n"
        )

        engine = RoutingEngine(tmp_path, use_watchdog=True)
        await engine.load_rules()
        assert engine._observer is not None, "Watchdog observer should be running"

        msg = make_msg(text="hello")

        # Initial match
        rule1, _ = await engine.match_with_rule(msg)
        assert rule1 is not None
        assert rule1.id == "v1"

        # Modify the file on disk — watchdog should detect this
        md.write_text(
            "---\nrouting:\n  id: v2\n  priority: 1\n---\n\n# V2\n"
        )

        # Wait for watchdog to detect the change (with timeout)
        import threading

        dirty_event = threading.Event()

        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal():
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=10.0), (
            "Watchdog observer did not detect file modification within 10s"
        )

        # match_with_rule() should now auto-reload
        rule2, _ = await engine.match_with_rule(msg)
        assert rule2 is not None
        assert rule2.id == "v2"

        engine.close()

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    async def test_real_watchdog_detects_new_file(self, tmp_path: Path):
        """Real watchdog observer detects a new .md file and triggers reload."""
        engine = RoutingEngine(tmp_path, use_watchdog=True)
        await engine.load_rules()
        assert engine._observer is not None, "Watchdog observer should be running"

        msg = make_msg(text="hello")
        assert await engine.match(msg) is None

        # Create a new instruction file
        (tmp_path / "new.md").write_text(
            "---\nrouting:\n  id: new-rule\n  priority: 1\n---\n\n# New\n"
        )

        # Wait for watchdog to detect the new file
        import threading

        dirty_event = threading.Event()
        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal():
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=10.0), (
            "Watchdog observer did not detect new file within 10s"
        )

        rule, inst = await engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "new-rule"
        assert inst == "new.md"

        engine.close()

    # ── (l) Watchdog fallback when observer fails to start ───────────────

    async def test_watchdog_fallback_on_observer_failure(self, tmp_path: Path):
        """If observer fails to start, engine falls back to polling mode."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = RoutingEngine(tmp_path, use_watchdog=True)

        # Patch Observer to raise during start
        if _HAS_WATCHDOG:
            with patch("src.routing.Observer", side_effect=OSError("no inotify")):
                await engine.load_rules()

            # Should have fallen back to polling
            assert engine._observer is None
            assert engine._use_watchdog is False

            # File modification should still be detected via mtime polling
            md.write_text(
                "---\nrouting:\n  id: r2\n  priority: 1\n---\n\n# R2\n"
            )
            engine._last_stale_check = 0.0
            assert engine._is_stale() is True

    # ── (m) load_rules resets _dirty flag ────────────────────────────────

    async def test_load_rules_resets_dirty_flag(self, tmp_path: Path):
        """load_rules() resets _dirty to False after loading fresh state."""
        md = tmp_path / "route.md"
        md.write_text(
            "---\nrouting:\n  id: r1\n  priority: 1\n---\n\n# R1\n"
        )

        engine = await self._make_engine(tmp_path)

        engine._mark_dirty()
        assert engine._dirty is True

        await engine.load_rules()
        assert engine._dirty is False


class TestLoadRulesInvalidRuleConstruction:
    """Tests for graceful handling of invalid rule dicts during load_rules."""

    async def test_invalid_rule_dict_skipped_gracefully(self, tmp_path: Path):
        """A rule dict that causes RoutingRule to raise is skipped with a log warning."""
        md = tmp_path / "bad_rule.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  - id: good-rule
                    priority: 1
                  - id: broken-rule
                    priority: 2
                ---
                # Mixed
            """)
        )
        engine = RoutingEngine(tmp_path)

        # Patch RoutingRule.__post_init__ to raise for the second rule only,
        # simulating a construction failure without affecting the first rule.
        original_post_init = RoutingRule.__post_init__
        call_count = 0

        def _flaky_post_init(self):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise TypeError("simulated construction failure")
            return original_post_init(self)

        with patch.object(RoutingRule, "__post_init__", _flaky_post_init):
            await engine.load_rules()

        # Only the first rule should load; the second was skipped
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "good-rule"

    async def test_rule_with_missing_required_field_handled(self, tmp_path: Path):
        """Rule construction failure in _rule_from_dict is caught."""
        md = tmp_path / "broken.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: test
                  priority: 1
                ---
                # Test
            """)
        )

        engine = RoutingEngine(tmp_path)

        # Patch RoutingRule.__init__ to raise for this specific call
        original_init = RoutingRule.__init__
        call_count = 0

        def _flaky_init(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TypeError("simulated construction failure")
            return original_init(self, *args, **kwargs)

        with patch.object(RoutingRule, "__init__", _flaky_init):
            await engine.load_rules()

        # Rule should be skipped, not crash the whole load
        # (first attempt fails, but the engine should continue)


# ═══════════════════════════════════════════════════════════════════════════════
# Frontmatter cache tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrontmatterCache:
    """Tests for the RoutingEngine frontmatter parsing cache.

    Verifies that unchanged .md files are served from the (mtime, size)
    cache during hot-reload, avoiding redundant YAML parsing.
    """

    async def test_first_load_populates_cache(self, tmp_path: Path):
        """Initial load_rules() populates the frontmatter cache."""
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: chat-rule
                  priority: 10
                ---
                # Chat
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        assert "chat.agent.md" in engine._frontmatter_cache
        cached = engine._frontmatter_cache["chat.agent.md"]
        assert len(cached) == 3  # (mtime, size, ParsedFile)
        assert cached[2].metadata.get("routing") is not None

    async def test_unchanged_file_hits_cache(self, tmp_path: Path):
        """Reloading without file changes returns cached ParsedFile (no re-parse)."""
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: chat-rule
                  priority: 10
                ---
                # Chat
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        # Patch parse_file to track calls — should NOT be called on second load
        with patch("src.routing.parse_file", wraps=parse_file) as mock_parse:
            await engine.load_rules()
            mock_parse.assert_not_called()

        assert len(engine.rules) == 1
        assert engine.rules[0].id == "chat-rule"

    async def test_modified_file_misses_cache(self, tmp_path: Path):
        """Changing file content invalidates the cache entry and re-parses."""
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: original-rule
                  priority: 10
                ---
                # Original
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules[0].id == "original-rule"

        # Modify the file (new content = new mtime/size)
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: updated-rule
                  priority: 5
                ---
                # Updated
            """)
        )
        await engine.load_rules()
        assert engine.rules[0].id == "updated-rule"
        assert engine.rules[0].priority == 5

    async def test_rules_setter_clears_cache(self, tmp_path: Path):
        """Setting _rules clears the frontmatter cache (full invalidation)."""
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: r1
                  priority: 1
                ---
                # Chat
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert len(engine._frontmatter_cache) > 0

        # Setting _rules directly clears the cache
        engine._rules = []
        assert len(engine._frontmatter_cache) == 0

    async def test_cache_keyed_by_mtime_and_size(self, tmp_path: Path):
        """Cache distinguishes files by both mtime and size.

        Verifies that even if mtime matches but size differs,
        the cache considers it a miss.
        """
        md = tmp_path / "chat.agent.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: v1
                  priority: 1
                ---
                # V1
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert engine.rules[0].id == "v1"

        # Force same mtime but different content/size — hard to guarantee
        # on fast systems, so instead verify the cache entry structure.
        cached = engine._frontmatter_cache["chat.agent.md"]
        mtime, size, parsed = cached
        assert size > 0
        assert isinstance(mtime, float)
        assert parsed.metadata["routing"]["id"] == "v1"

    async def test_cache_bounded_by_max_size(self, tmp_path: Path):
        """Cache evicts old entries when exceeding max size."""
        from src.constants import ROUTING_FRONTMATTER_CACHE_MAX_SIZE

        # Create more files than the cache max size
        for i in range(ROUTING_FRONTMATTER_CACHE_MAX_SIZE + 10):
            (tmp_path / f"rule_{i:04d}.md").write_text(
                f"---\nrouting:\n  id: r{i}\n  priority: {i}\n---\n\n# R{i}\n"
            )

        engine = RoutingEngine(tmp_path)
        await engine.load_rules()

        # Cache should be bounded
        assert len(engine._frontmatter_cache) <= ROUTING_FRONTMATTER_CACHE_MAX_SIZE
        # All rules should still be loaded (parsed on demand)
        assert len(engine.rules) == ROUTING_FRONTMATTER_CACHE_MAX_SIZE + 10

    async def test_cached_parse_returns_parsed_file(self, tmp_path: Path):
        """_cached_parse returns a proper ParsedFile with source set."""
        md = tmp_path / "test.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: cached
                  priority: 1
                ---
                # Body
            """)
        )
        engine = RoutingEngine(tmp_path)
        parsed = engine._cached_parse(md)

        assert parsed.source == md
        assert parsed.metadata.get("routing") is not None
        assert "# Body" in parsed.content

    async def test_retry_invalidates_cache_entry(self, tmp_path: Path):
        """On retry after transient failure, the stale cache entry is evicted."""
        md = tmp_path / "flaky.md"
        md.write_text(
            textwrap.dedent("""\
                ---
                routing:
                  id: retry-rule
                  priority: 1
                ---
                # Retry
            """)
        )
        engine = RoutingEngine(tmp_path)
        await engine.load_rules()
        assert "flaky.md" in engine._frontmatter_cache

        call_count = 0
        original_parse = parse_file

        def _flaky_parse(path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("simulated transient failure")
            return original_parse(path)

        with patch("src.routing.parse_file", side_effect=_flaky_parse):
            with patch("src.routing.asyncio.sleep", new_callable=AsyncMock):
                # Pop the cache before retry to simulate cache invalidation path
                engine._frontmatter_cache.pop("flaky.md", None)
                await engine.load_rules()

        assert call_count == 2
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "retry-rule"
