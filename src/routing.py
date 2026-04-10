"""
routing.py — Message routing engine.

Routes incoming messages to appropriate instruction files based on
routing rules embedded as YAML frontmatter in .md instruction files.

The engine scans ``workspace/instructions/*.md``, parses the ``routing``
key from each file's frontmatter, and builds an in-memory rule table.
Messages are matched against rules in priority order (lower = first).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.channels.base import IncomingMessage
from src.utils.frontmatter import extract_routing_rules, parse_file

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class MatchingContext:
    """
    Immutable context extracted from an IncomingMessage for rule matching.

    Frozen dataclass: cannot be modified after creation. Enables use as
    dict keys and in sets. Thread-safe by design.

    Encapsulates all message attributes used by routing rules,
    making matching logic pure and easily testable without
    requiring full IncomingMessage instances.

    Attributes:
        sender_id: The sender's phone/user ID.
        chat_id: The conversation/group identifier.
        channel_type: Channel identifier (e.g., 'whatsapp', 'telegram').
        text: The message body text.
        fromMe: True if message was sent by the bot user.
        toMe: True if message was sent directly to the bot user.
    """

    sender_id: str
    chat_id: str
    channel_type: str
    text: str
    fromMe: bool = False
    toMe: bool = False

    def __repr__(self) -> str:
        text_preview = self.text[:30] + "..." if len(self.text) > 30 else self.text
        return (
            f"MatchingContext(sender={self.sender_id!r}, chat={self.chat_id!r}, "
            f"channel={self.channel_type!r}, text={text_preview!r}, "
            f"fromMe={self.fromMe}, toMe={self.toMe})"
        )

    @classmethod
    def from_message(cls, msg: IncomingMessage) -> MatchingContext:
        """
        Extract matching context from an incoming message.

        Args:
            msg: The incoming message to extract context from.

        Returns:
            A new MatchingContext instance with extracted values.
        """
        return cls(
            sender_id=msg.sender_id,
            chat_id=msg.chat_id,
            channel_type=msg.channel_type,
            text=msg.text,
            fromMe=msg.fromMe,
            toMe=msg.toMe,
        )


def _compile_pattern(pattern: str) -> Optional[re.Pattern]:
    """Pre-compile a routing pattern to a regex, or None if not regex."""
    if pattern in ("*", ""):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _match_compiled(compiled: Optional[re.Pattern], pattern: str, value: str) -> bool:
    """
    Match a pre-compiled pattern against a value.

    Falls back to exact string match if regex compilation failed.
    """
    if pattern == "*":
        return True
    if pattern == "":
        return value == ""
    if compiled is not None and compiled.match(value):
        return True
    # Exact string match (fallback for uncompileable patterns)
    return pattern == value


@dataclass(slots=True, frozen=True)
class RoutingRule:
    """
    Immutable routing rule that matches messages and maps to an instruction file.

    Frozen dataclass: cannot be modified after creation. Enables use as
    dict keys and in sets. Thread-safe by design.

    Uses slots=True for memory efficiency when many routing rules are loaded.
    Reduces memory footprint per rule instance and speeds up attribute access.

    Attributes:
        id: Unique identifier for the rule.
        priority: Lower values = higher priority (evaluated first).
        sender: Sender pattern ('*' matches all, or specific sender_id).
        recipient: Recipient pattern ('*' matches all, or specific recipient).
        channel: Channel type pattern ('*' matches all, e.g., 'whatsapp').
        content_regex: Regex pattern to match against message text ('*' matches all).
        instruction: Filename of the instruction file to use (e.g., 'chat.agent.md').
        enabled: Whether this rule is active.
        fromMe: Match messages from bot user (True=only fromMe, False=only not fromMe, None=all).
        toMe: Match direct messages to bot (True=only direct, False=only group, None=all).
        skillExecVerbose: Control tool execution display:
            '' (default) — no tool output shown.
            'summary' — append tool list at bottom of response.
            'full' — stream tool usage in real-time as messages.
        showErrors: Show errors to user when True.
    """

    id: str
    priority: int
    sender: str
    recipient: str
    channel: str
    content_regex: str
    instruction: str
    enabled: bool = True
    fromMe: Optional[bool] = None
    toMe: Optional[bool] = None
    skillExecVerbose: str = ""
    showErrors: bool = True
    # Pre-compiled regex patterns (computed once at construction)
    _compiled_sender: Optional[re.Pattern] = field(
        default=None, repr=False, compare=False
    )
    _compiled_recipient: Optional[re.Pattern] = field(
        default=None, repr=False, compare=False
    )
    _compiled_channel: Optional[re.Pattern] = field(
        default=None, repr=False, compare=False
    )
    _compiled_content: Optional[re.Pattern] = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # frozen=True requires object.__setattr__ to modify fields
        object.__setattr__(self, "_compiled_sender", _compile_pattern(self.sender))
        object.__setattr__(
            self, "_compiled_recipient", _compile_pattern(self.recipient)
        )
        object.__setattr__(self, "_compiled_channel", _compile_pattern(self.channel))
        object.__setattr__(
            self, "_compiled_content", _compile_pattern(self.content_regex)
        )

    def __repr__(self) -> str:
        regex_preview = (
            self.content_regex[:20] + "..."
            if len(self.content_regex) > 20
            else self.content_regex
        )
        status = "ON" if self.enabled else "OFF"
        return (
            f"RoutingRule(id={self.id!r}, pri={self.priority}, sender={self.sender!r}, "
            f"channel={self.channel!r}, regex={regex_preview!r}, instruction={self.instruction!r}, "
            f"{status})"
        )


def _rule_from_dict(rule_dict: Dict[str, Any], filename: str) -> RoutingRule:
    """Build a RoutingRule from a parsed frontmatter dict + source filename."""
    # Backward compat: showSkillExec: true → skillExecVerbose: "summary"
    legacy = rule_dict.get("showSkillExec")
    if legacy is True:
        verbose_val = "summary"
    else:
        verbose_val = rule_dict.get("skillExecVerbose", "")
    return RoutingRule(
        id=rule_dict.get("id", Path(filename).stem),
        priority=rule_dict.get("priority", 0),
        sender=rule_dict.get("sender", "*"),
        recipient=rule_dict.get("recipient", "*"),
        channel=rule_dict.get("channel", "*"),
        content_regex=rule_dict.get("content_regex", "*"),
        instruction=filename,
        enabled=rule_dict.get("enabled", True),
        fromMe=rule_dict.get("fromMe"),
        toMe=rule_dict.get("toMe"),
        skillExecVerbose=verbose_val,
        showErrors=rule_dict.get("showErrors", True),
    )


class RoutingEngine:
    """
    Engine for matching incoming messages to routing rules.

    Rules are discovered by scanning .md instruction files for YAML
    frontmatter containing a ``routing`` key. The engine loads all
    rules into memory and evaluates them in priority order.

    The engine no longer depends on the Database for rule storage.
    Instead, it scans the instructions directory directly.
    """

    def __init__(self, instructions_dir: str | Path) -> None:
        """
        Initialize the routing engine.

        Args:
            instructions_dir: Path to the directory containing .md instruction files.
        """
        self._instructions_dir = Path(instructions_dir)
        self._rules: List[RoutingRule] = []

    @property
    def rules(self) -> List[RoutingRule]:
        """Read-only access to loaded routing rules."""
        return self._rules

    @property
    def instructions_dir(self) -> Path:
        """Read-only access to the instructions directory."""
        return self._instructions_dir

    def load_rules(self) -> None:
        """
        Scan .md instruction files and extract routing rules from frontmatter.

        Loads rules from all .md files in the instructions directory that
        contain a ``routing`` key in their YAML frontmatter.
        Rules are sorted by priority (ascending) after loading.

        Call this at startup and whenever instruction files are updated.
        """
        rules: List[RoutingRule] = []

        if not self._instructions_dir.is_dir():
            log.warning("Instructions directory not found: %s", self._instructions_dir)
            self._rules = rules
            return

        for md_file in sorted(self._instructions_dir.glob("*.md")):
            try:
                parsed = parse_file(md_file)
                rule_dicts = extract_routing_rules(parsed.metadata)
            except Exception as exc:
                log.warning("Failed to parse %s: %s", md_file.name, exc)
                continue

            for rule_dict in rule_dicts:
                try:
                    rule = _rule_from_dict(rule_dict, md_file.name)
                    rules.append(rule)
                    log.debug(
                        "Loaded routing rule '%s' (priority=%d) from %s",
                        rule.id,
                        rule.priority,
                        md_file.name,
                    )
                except Exception as exc:
                    log.warning("Invalid routing rule in %s: %s", md_file.name, exc)

        # Sort by priority ascending
        rules.sort(key=lambda r: r.priority)
        self._rules = rules

        log.info(
            "Loaded %d routing rule(s) from %s",
            len(rules),
            self._instructions_dir,
        )

    def refresh_rules(self) -> None:
        """
        Reload routing rules by re-scanning instruction files.

        Convenience method that calls load_rules(). Use this when
        instruction files are modified at runtime.
        """
        self.load_rules()

    def match(self, msg: IncomingMessage) -> Optional[str]:
        """
        Match an incoming message against loaded routing rules.

        Pure function: evaluates rules in priority order without side effects.
        Returns the instruction filename from the first matching rule, or None.

        Args:
            msg: The incoming message to match.

        Returns:
            Instruction filename (e.g., 'chat.agent.md') if a rule matches,
            otherwise None.
        """
        _, instruction = self.match_with_rule(msg)
        return instruction

    def match_with_rule(
        self, msg: IncomingMessage
    ) -> tuple[Optional["RoutingRule"], Optional[str]]:
        """
        Match an incoming message and return both the rule and instruction.

        Same as match() but returns the full rule object for logging/debugging.

        Args:
            msg: The incoming message to match.

        Returns:
            Tuple of (rule, instruction_filename). Both are None if no match.
        """
        ctx = MatchingContext.from_message(msg)

        for rule in self._rules:
            if not rule.enabled:
                continue

            if rule.fromMe is not None and rule.fromMe != ctx.fromMe:
                continue

            if rule.toMe is not None and rule.toMe != ctx.toMe:
                continue

            if not _match_compiled(rule._compiled_sender, rule.sender, ctx.sender_id):
                continue

            if not _match_compiled(
                rule._compiled_recipient, rule.recipient, ctx.chat_id
            ):
                continue

            if not _match_compiled(
                rule._compiled_channel, rule.channel, ctx.channel_type
            ):
                continue

            if not _match_compiled(
                rule._compiled_content, rule.content_regex, ctx.text
            ):
                continue

            return rule, rule.instruction

        return None, None
