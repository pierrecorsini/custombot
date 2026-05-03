"""
routing.py — Message routing engine.

Routes incoming messages to appropriate instruction files based on
routing rules embedded as YAML frontmatter in .md instruction files.

The engine scans ``workspace/instructions/*.md``, parses the ``routing``
key from each file's frontmatter, and builds an in-memory rule table.
Messages are matched against rules in priority order (lower = first).

File-change detection uses OS-native watching (watchdog / inotify /
ReadDirectoryChanges) when available, with automatic fallback to
debounced mtime polling.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import xxhash

from src.channels.base import ChannelType, IncomingMessage
from src.constants import (
    ROUTING_MATCH_CACHE_MAX_SIZE,
    ROUTING_MATCH_CACHE_TTL_SECONDS,
    ROUTING_WATCH_DEBOUNCE_SECONDS,
)
from src.utils import BoundedOrderedDict
from src.utils.frontmatter import extract_routing_rules, parse_file

# Optional watchdog import — graceful degradation when not installed.
_HAS_WATCHDOG = False
try:
    from watchdog.events import FileSystemEventHandler, FileMovedEvent
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:
    pass

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
    channel_type: ChannelType | str
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
    # Lazily-compiled regex patterns (populated on first match attempt)
    _compiled_sender: Optional[re.Pattern] = field(default=None, repr=False, compare=False)
    _compiled_recipient: Optional[re.Pattern] = field(default=None, repr=False, compare=False)
    _compiled_channel: Optional[re.Pattern] = field(default=None, repr=False, compare=False)
    _compiled_content: Optional[re.Pattern] = field(default=None, repr=False, compare=False)
    # Pre-computed flag: True when all four match patterns are "*"
    _is_wildcard: bool = field(default=False, repr=False, compare=False)
    # Tracks whether regex patterns have been compiled yet
    _compiled: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Only compute cheap _is_wildcard flag; defer regex compilation to first match
        object.__setattr__(
            self,
            "_is_wildcard",
            self.sender == "*"
            and self.recipient == "*"
            and self.channel == "*"
            and self.content_regex == "*",
        )

    def _ensure_compiled(self) -> None:
        """Compile regex patterns on first match attempt (lazy initialization).

        Safe for concurrent use: ``_compile_pattern`` is deterministic, so
        overlapping calls produce identical results (idempotent writes).
        """
        if self._compiled:
            return
        object.__setattr__(self, "_compiled_sender", _compile_pattern(self.sender))
        object.__setattr__(self, "_compiled_recipient", _compile_pattern(self.recipient))
        object.__setattr__(self, "_compiled_channel", _compile_pattern(self.channel))
        object.__setattr__(self, "_compiled_content", _compile_pattern(self.content_regex))
        object.__setattr__(self, "_compiled", True)

    def __repr__(self) -> str:
        regex_preview = (
            self.content_regex[:20] + "..." if len(self.content_regex) > 20 else self.content_regex
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


def _merge_priority_sorted(
    a: list[RoutingRule], b: list[RoutingRule],
) -> list[RoutingRule]:
    """Merge two pre-sorted rule lists preserving priority order.

    Both input lists must already be sorted by ``priority`` ascending.
    Returns a merged list in priority order.  Uses a two-pointer merge
    (O(n+m)) instead of concatenation + sort (O((n+m) log(n+m))).
    """
    result: list[RoutingRule] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i].priority <= b[j].priority:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    result.extend(a[i:])
    result.extend(b[j:])
    return result


class RoutingEngine:
    """
    Engine for matching incoming messages to routing rules.

    Rules are discovered by scanning .md instruction files for YAML
    frontmatter containing a ``routing`` key. The engine loads all
    rules into memory and evaluates them in priority order.

    The engine auto-reloads rules when instruction files change on disk.
    When *watchdog* is available (default), an OS-native file watcher
    provides instant change detection with zero polling overhead.
    When watchdog is not installed, a debounced mtime check runs before
    each match as a fallback.

    The engine no longer depends on the Database for rule storage.
    Instead, it scans the instructions directory directly.
    """

    def __init__(
        self,
        instructions_dir: str | Path,
        *,
        use_watchdog: bool = True,
    ) -> None:
        """
        Initialize the routing engine.

        Args:
            instructions_dir: Path to the directory containing .md instruction files.
            use_watchdog: When True and watchdog is installed, use OS-native file
                watching for instant change detection. When False (or watchdog is
                unavailable), fall back to debounced mtime polling.
        """
        self._instructions_dir = Path(instructions_dir)
        self._rules_list: List[RoutingRule] = []
        self._file_mtimes: dict[str, float] = {}
        self._last_stale_check: float = 0.0
        # Pre-grouped rule index for faster matching.  Populated in
        # load_rules() when the rule list changes.  Keyed by channel
        # pattern — most messages target a single channel, so this
        # reduces the evaluation set by ~75% for typical multi-channel
        # rule sets.
        self._rules_by_channel: dict[str, list[RoutingRule]] = {}
        self._wildcard_rules: list[RoutingRule] = []
        self._regex_channel_rules: list[RoutingRule] = []
        # Pre-computed merged candidate lists per channel (built once on
        # rebuild, avoiding per-message list allocation in match_with_rule).
        self._candidates_by_channel: dict[str, list[RoutingRule]] = {}
        self._default_candidates: list[RoutingRule] = []
        # TTL-bounded LRU cache for match results (delegated to BoundedOrderedDict).
        # Key: (fromMe, toMe, sender_id, chat_id, channel_type, text[:100])
        # Value: (rule | None, instruction | None)
        self._match_cache: BoundedOrderedDict[Tuple, Tuple] = BoundedOrderedDict(
            max_size=ROUTING_MATCH_CACHE_MAX_SIZE,
            eviction="half",
            ttl=ROUTING_MATCH_CACHE_TTL_SECONDS,
        )

        # Watchdog-based file watching
        self._use_watchdog: bool = use_watchdog and _HAS_WATCHDOG
        self._dirty: bool = False
        self._observer: Optional[Observer] = None  # type: ignore[name-defined]

    # ── Watchdog file-watching helpers ─────────────────────────────────────

    def _mark_dirty(self) -> None:
        """Mark instruction files as changed (called from watchdog observer thread)."""
        self._dirty = True

    def _start_watcher(self) -> None:
        """Start the OS-native file watcher for the instructions directory."""
        if not self._use_watchdog or self._observer is not None:
            return
        if not self._instructions_dir.is_dir():
            return

        try:
            handler = _RoutingFileEventHandler(self)
            observer = Observer()  # type: ignore[name-defined]
            observer.schedule(handler, str(self._instructions_dir), recursive=False)
            observer.daemon = True
            observer.start()
            self._observer = observer
            log.debug("Watchdog observer started for %s", self._instructions_dir)
        except Exception as exc:
            log.warning(
                "Failed to start watchdog observer for %s, falling back to polling: %s",
                self._instructions_dir,
                exc,
            )
            self._observer = None
            self._use_watchdog = False

    def _stop_watcher(self) -> None:
        """Stop the file watcher if running."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None

    def close(self) -> None:
        """Stop the file watcher and release resources."""
        self._stop_watcher()

    @property
    def _rules(self) -> List[RoutingRule]:
        """Internal rules list (backed by _rules_list)."""
        return self._rules_list

    @_rules.setter
    def _rules(self, value: List[RoutingRule]) -> None:
        """Set rules, rebuild the channel index, and invalidate the match cache."""
        self._rules_list = value
        self._match_cache.clear()
        self._rebuild_channel_index(value)

    @property
    def rules(self) -> List[RoutingRule]:
        """Read-only access to loaded routing rules."""
        return self._rules_list

    @property
    def has_rules(self) -> bool:
        """Whether any routing rules are currently loaded."""
        return len(self._rules_list) > 0

    @property
    def instructions_dir(self) -> Path:
        """Read-only access to the instructions directory."""
        return self._instructions_dir

    def _rebuild_channel_index(self, rules: list[RoutingRule]) -> None:
        """Pre-group rules by channel pattern for faster matching.

        Builds three structures:
        - ``_rules_by_channel``: maps specific channel strings to their
          matching rules (e.g., ``{"whatsapp": [rule1, rule3]}``).
        - ``_wildcard_rules``: rules with ``channel="*"`` that match all
          channels — always evaluated.
        - ``_regex_channel_rules``: rules whose channel is a non-wildcard
          regex pattern (e.g., ``"te.*"``) — always evaluated alongside
          wildcard rules since they can match any channel string.

        Rules remain sorted by priority within each group.
        """
        by_channel: dict[str, list[RoutingRule]] = {}
        wildcard: list[RoutingRule] = []
        regex_channel: list[RoutingRule] = []
        for rule in rules:
            if rule.channel == "*":
                wildcard.append(rule)
            elif _compile_pattern(rule.channel) is not None:
                # Regex channel pattern — must be evaluated for every message
                regex_channel.append(rule)
            else:
                by_channel.setdefault(rule.channel, []).append(rule)
        self._rules_by_channel = by_channel
        self._wildcard_rules = wildcard
        self._regex_channel_rules = regex_channel
        # Pre-compute merged candidate lists so match_with_rule() can
        # do a single dict lookup instead of two _merge_priority_sorted
        # calls per message.
        self._candidates_by_channel = {
            channel: _merge_priority_sorted(
                _merge_priority_sorted(rules, wildcard),
                regex_channel,
            )
            for channel, rules in by_channel.items()
        }
        # Default candidates for channels not present in the index
        # (wildcard + regex-channel rules only).
        self._default_candidates = _merge_priority_sorted(wildcard, regex_channel)

    def _scan_file_mtimes(self) -> dict[str, float]:
        """Collect current mtimes for all .md files in the instructions directory.

        Uses ``os.scandir()`` instead of ``glob()`` for lower overhead:
        DirEntry objects cache stat results and avoid pattern-matching cost.
        """
        mtimes: dict[str, float] = {}
        if self._instructions_dir.is_dir():
            try:
                with os.scandir(self._instructions_dir) as entries:
                    for entry in entries:
                        if entry.is_file() and entry.name.endswith(".md"):
                            try:
                                mtimes[entry.name] = entry.stat().st_mtime
                            except OSError:
                                pass
            except OSError:
                # Directory may have been removed
                pass
        return mtimes

    def _is_stale(self) -> bool:
        """Check whether instruction files have changed since last load.

        When watchdog is active, returns the ``_dirty`` flag set by the
        OS-native file watcher — instant check with zero I/O overhead.

        When watchdog is unavailable, falls back to debounced mtime
        polling (``os.scandir`` + ``stat``).
        """
        if self._use_watchdog:
            dirty = self._dirty
            self._dirty = False
            return dirty
        # Fallback: debounced mtime polling
        now = time.monotonic()
        if now - self._last_stale_check < ROUTING_WATCH_DEBOUNCE_SECONDS:
            return False
        self._last_stale_check = now
        return self._scan_file_mtimes() != self._file_mtimes

    def load_rules(self) -> None:
        """
        Scan .md instruction files and extract routing rules from frontmatter.

        Loads rules from all .md files in the instructions directory that
        contain a ``routing`` key in their YAML frontmatter.
        Rules are sorted by priority (ascending) after loading.

        Call this at startup and whenever instruction files are updated.

        Graceful degradation: if a reload produces zero rules but rules
        previously existed, the old rule set is retained and a warning is
        logged.  This handles transient empty-file states (e.g. an editor
        that truncates before writing).  File mtimes are intentionally left
        stale so that the next debounced stale-check will retry the reload.
        """
        previous_rules = list(self._rules_list)
        rules: List[RoutingRule] = []

        if not self._instructions_dir.is_dir():
            log.warning("Instructions directory not found: %s", self._instructions_dir)
            self._rules = rules
            return

        for md_file in sorted(self._instructions_dir.glob("*.md"), key=lambda p: p.name):
            try:
                parsed = parse_file(md_file)
                rule_dicts = extract_routing_rules(parsed.metadata)
            except Exception as exc:
                # Retry once for transient parse failures (e.g. concurrent writes)
                log.debug("Transient parse failure for %s, retrying: %s", md_file.name, exc)
                time.sleep(0.1)
                try:
                    parsed = parse_file(md_file)
                    rule_dicts = extract_routing_rules(parsed.metadata)
                except Exception as retry_exc:
                    log.warning("Failed to parse %s after retry: %s", md_file.name, retry_exc)
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

        # Graceful degradation: keep previous rules when reload yields nothing.
        # Do NOT update _file_mtimes so the next stale-check retries the load.
        if len(rules) == 0 and len(previous_rules) > 0:
            log.warning(
                "Reload produced zero routing rules from %s — retaining "
                "previous rule set (%d rules). Instruction files may be "
                "temporarily empty during editing.",
                self._instructions_dir,
                len(previous_rules),
            )
            self._dirty = False
            return

        self._rules = rules
        self._file_mtimes = self._scan_file_mtimes()

        # Reset dirty flag after loading fresh state
        self._dirty = False

        # Start OS-native file watcher if not already running
        self._start_watcher()

        if len(rules) == 0:
            log.warning(
                "No routing rules loaded from %s — messages will not be "
                "matched. Add at least a 'chat.agent.md' with YAML routing "
                "frontmatter. See src/templates/instructions/ for examples.",
                self._instructions_dir,
            )
        else:
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

    def _cache_key(self, ctx: MatchingContext) -> Tuple:
        """Build a hashable cache key from message signature attributes.

        Uses xxhash over the full text to avoid collisions that occurred
        with the previous text[:100] truncation (long messages with
        identical prefixes would map to the same cache entry).
        """
        text_hash = xxhash.xxh64(ctx.text.encode("utf-8")).hexdigest()
        return (ctx.fromMe, ctx.toMe, ctx.sender_id, ctx.chat_id, ctx.channel_type, text_hash)

    def match_with_rule(
        self, msg: IncomingMessage
    ) -> tuple[Optional["RoutingRule"], Optional[str]]:
        """
        Match an incoming message and return both the rule and instruction.

        Same as match() but returns the full rule object for logging/debugging.
        Auto-reloads rules if instruction files have changed on disk.
        Results are cached for identical message signatures within a short TTL.

        Args:
            msg: The incoming message to match.

        Returns:
            Tuple of (rule, instruction_filename). Both are None if no match.
        """
        if self._is_stale():
            self.load_rules()

        ctx = MatchingContext.from_message(msg)
        cache_key = self._cache_key(ctx)

        # Check cache first
        cached = self._match_cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        # Evaluate rules using pre-computed channel-grouped index
        channel_str = str(ctx.channel_type)
        candidates = self._candidates_by_channel.get(channel_str)
        if candidates is not None:
            candidate_rules = candidates
        else:
            candidate_rules = self._default_candidates

        for rule in candidate_rules:
            if not rule.enabled:
                continue

            if rule.fromMe is not None and rule.fromMe != ctx.fromMe:
                continue

            if rule.toMe is not None and rule.toMe != ctx.toMe:
                continue

            # Wildcard-only rules skip all regex/exact matching — fromMe/toMe
            # (checked above) are the only discriminators.
            if not rule._is_wildcard:
                rule._ensure_compiled()
                if not _match_compiled(rule._compiled_sender, rule.sender, ctx.sender_id):
                    continue

                if not _match_compiled(rule._compiled_recipient, rule.recipient, ctx.chat_id):
                    continue

                if not _match_compiled(rule._compiled_channel, rule.channel, ctx.channel_type):
                    continue

                if not _match_compiled(rule._compiled_content, rule.content_regex, ctx.text):
                    continue

            result = (rule, rule.instruction)
            self._match_cache[cache_key] = result
            return result

        result = (None, None)
        self._match_cache[cache_key] = result
        return result


# ── Watchdog event handler ─────────────────────────────────────────────────


class _RoutingFileEventHandler(FileSystemEventHandler):  # type: ignore[name-defined]
    """Watchdog handler that marks the engine dirty when .md files change."""

    def __init__(self, engine: RoutingEngine) -> None:
        self._engine = engine

    def on_modified(self, event: Any) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".md"):
            self._engine._mark_dirty()

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".md"):
            self._engine._mark_dirty()

    def on_deleted(self, event: Any) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".md"):
            self._engine._mark_dirty()

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        # Check both source and destination for .md extension
        if isinstance(event, FileMovedEvent):  # type: ignore[name-defined]
            if event.src_path.endswith(".md") or event.dest_path.endswith(".md"):
                self._engine._mark_dirty()
        elif event.src_path.endswith(".md"):
            self._engine._mark_dirty()
