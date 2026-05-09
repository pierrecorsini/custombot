"""
test_routing_watchdog.py — Integration tests for RoutingEngine auto-reload.

Verifies the full hot-reload pipeline:
  1. Polling mode: modifying an .md file on disk causes ``match_with_rule()``
     to detect the change via ``_is_stale()`` (mtime polling) and reload rules.
  2. Watchdog mode (when available): OS-native file watcher sets the dirty
     flag, triggering automatic rule reload on the next match.

These tests exercise real filesystem I/O and the complete
  file-change → _is_stale() → load_rules() → fresh rules
pipeline without mocking internal methods.
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import patch

import pytest

from src.channels.base import ChannelType, IncomingMessage
from src.constants import ROUTING_WATCH_DEBOUNCE_SECONDS
from src.routing import RoutingEngine, _HAS_WATCHDOG
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Short debounce for fast polling tests (real constant is 5s).
_POLL_DEBOUNCE = 0.05


def _md_content(rule_id: str, priority: int = 1, body: str = "# Rule") -> str:
    """Return a minimal .md file with a single routing rule."""
    return f"---\nrouting:\n  id: {rule_id}\n  priority: {priority}\n---\n\n{body}\n"


def _make_msg(text: str = "hello") -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    return IncomingMessage(
        message_id="msg-001",
        chat_id="chat-001",
        sender_id="5511999990000",
        sender_name="Alice",
        text=text,
        timestamp=1700000000.0,
        channel_type=ChannelType.WHATSAPP,
    )


def _write_md(path: Path, rule_id: str, priority: int = 1) -> None:
    """Write a routing .md file."""
    path.write_text(_md_content(rule_id, priority), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Polling-mode integration tests (work on all platforms)
# ─────────────────────────────────────────────────────────────────────────────


class TestPollingAutoReload:
    """Integration tests for auto-reload via mtime polling (use_watchdog=False).

    These tests exercise the real filesystem: write .md files, modify them on
    disk, and verify that ``match_with_rule()`` detects the changes and reloads
    rules automatically — without any internal mocking.
    """

    @pytest.fixture()
    def engine(self, tmp_path: Path) -> RoutingEngine:
        """Create a polling-mode engine with a short debounce interval."""
        with patch("src.routing.ROUTING_WATCH_DEBOUNCE_SECONDS", _POLL_DEBOUNCE):
            # Also patch the imported constant used by _is_stale()
            engine = RoutingEngine(tmp_path, use_watchdog=False)
            engine.load_rules()
            yield engine

    def _force_debounce(self, engine: RoutingEngine) -> None:
        """Reset last stale check so the next _is_stale() will scan mtimes."""
        engine._last_stale_check = 0.0

    # ── File modification triggers reload ──────────────────────────────────

    def test_file_modification_triggers_auto_reload(
        self, tmp_path: Path, engine: RoutingEngine
    ) -> None:
        """
        Modifying an .md instruction file on disk causes match_with_rule()
        to detect the change via _is_stale() (mtime polling) and reload
        rules automatically.
        """
        md = tmp_path / "route.md"
        _write_md(md, "v1")

        # Initial load
        engine.load_rules()
        msg = _make_msg()

        rule1, inst1 = engine.match_with_rule(msg)
        assert rule1 is not None
        assert rule1.id == "v1"

        # Modify file on disk
        _write_md(md, "v2")

        # Advance past debounce and match again
        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule2, inst2 = engine.match_with_rule(msg)
        assert rule2 is not None
        assert rule2.id == "v2"

    # ── New file creation triggers reload ──────────────────────────────────

    def test_new_file_triggers_auto_reload(self, tmp_path: Path, engine: RoutingEngine) -> None:
        """Creating a new .md file with routing rules is detected on next match."""
        msg = _make_msg()

        # Initially no rules
        assert engine.match(msg) is None

        # Create a new instruction file
        _write_md(tmp_path / "new_rule.md", "new-rule")

        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None
        assert rule.id == "new-rule"
        assert inst == "new_rule.md"

    # ── File deletion triggers reload ──────────────────────────────────────

    def test_file_deletion_triggers_auto_reload(
        self, tmp_path: Path, engine: RoutingEngine
    ) -> None:
        """Deleting an .md file removes its rules on the next match."""
        md = tmp_path / "gone.md"
        _write_md(md, "temporary")

        engine.load_rules()
        msg = _make_msg()
        assert engine.match(msg) == "gone.md"

        # Delete the file
        md.unlink()

        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule, inst = engine.match_with_rule(msg)
        assert rule is None
        assert inst is None

    # ── Full lifecycle: create → modify → delete → recreate ───────────────

    def test_full_lifecycle_via_polling(self, tmp_path: Path, engine: RoutingEngine) -> None:
        """Full lifecycle through polling: create, match, modify, re-match,
        delete, verify gone, recreate, verify restored."""
        msg = _make_msg()

        # Phase 1: No rules
        assert engine.match(msg) is None

        # Phase 2: Create file
        md = tmp_path / "lifecycle.md"
        _write_md(md, "step1")
        engine.load_rules()

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step1"

        # Phase 3: Modify file
        _write_md(md, "step2")
        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step2"

        # Phase 4: Delete file
        md.unlink()
        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule, inst = engine.match_with_rule(msg)
        assert rule is None and inst is None

        # Phase 5: Recreate file
        _write_md(md, "step5")
        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step5"

    # ── Priority ordering changes after reload ────────────────────────────

    def test_priority_ordering_updated_after_file_change(
        self, tmp_path: Path, engine: RoutingEngine
    ) -> None:
        """
        When two files define competing rules, modifying one file's priority
        causes match_with_rule() to return the differently-ordered rule.
        """
        a = tmp_path / "alpha.md"
        b = tmp_path / "beta.md"
        _write_md(a, "alpha", priority=10)
        _write_md(b, "beta", priority=5)

        engine.load_rules()
        msg = _make_msg()

        # beta (pri=5) should win
        rule, _ = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "beta"

        # Change alpha to higher priority (lower number)
        _write_md(a, "alpha", priority=1)

        self._force_debounce(engine)
        time.sleep(_POLL_DEBOUNCE + 0.02)

        # alpha (pri=1) should now win
        rule, _ = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "alpha"


# ─────────────────────────────────────────────────────────────────────────────
# Real watchdog integration tests (OS-native file watcher)
# ─────────────────────────────────────────────────────────────────────────────


class TestRealWatchdogAutoReload:
    """Integration tests with the real OS-native watchdog observer.

    These exercises the complete pipeline:
        filesystem event → _RoutingFileEventHandler → _mark_dirty()
        → _is_stale() → load_rules() → fresh rules

    Skipped on Windows (ReadDirectoryChangesW latency causes flakes)
    and when watchdog is not installed.
    """

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    def test_watchdog_detects_file_modification(self, tmp_path: Path) -> None:
        """Real watchdog observer detects .md file modification and triggers
        auto-reload through match_with_rule()."""
        md = tmp_path / "route.md"
        _write_md(md, "v1")

        engine = RoutingEngine(tmp_path, use_watchdog=True)
        engine.load_rules()
        assert engine._observer is not None, "Watchdog observer should be running"

        msg = _make_msg()

        # Initial match
        rule1, _ = engine.match_with_rule(msg)
        assert rule1 is not None and rule1.id == "v1"

        # Modify file on disk — watchdog should detect this
        _write_md(md, "v2")

        # Wait for watchdog to set the dirty flag
        dirty_event = threading.Event()
        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal() -> None:
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=10.0), (
            "Watchdog observer did not detect file modification within 10s"
        )

        # match_with_rule() should auto-reload and return the updated rule
        rule2, _ = engine.match_with_rule(msg)
        assert rule2 is not None and rule2.id == "v2"

        engine.close()

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    def test_watchdog_detects_new_file(self, tmp_path: Path) -> None:
        """Real watchdog observer detects a newly created .md file and triggers
        auto-reload so the new rule is available."""
        engine = RoutingEngine(tmp_path, use_watchdog=True)
        engine.load_rules()
        assert engine._observer is not None, "Watchdog observer should be running"

        msg = _make_msg()
        assert engine.match(msg) is None

        # Create a new instruction file
        _write_md(tmp_path / "new.md", "new-rule")

        # Wait for watchdog to detect the new file
        dirty_event = threading.Event()
        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal() -> None:
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=10.0), (
            "Watchdog observer did not detect new file within 10s"
        )

        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "new-rule"
        assert inst == "new.md"

        engine.close()

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    def test_watchdog_detects_file_deletion(self, tmp_path: Path) -> None:
        """Real watchdog observer detects .md file deletion and triggers
        auto-reload so the removed rule is no longer matched."""
        md = tmp_path / "removable.md"
        _write_md(md, "temporary")

        engine = RoutingEngine(tmp_path, use_watchdog=True)
        engine.load_rules()
        assert engine._observer is not None

        msg = _make_msg()
        assert engine.match(msg) == "removable.md"

        # Delete the file
        md.unlink()

        # Wait for watchdog to detect the deletion
        dirty_event = threading.Event()
        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal() -> None:
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=10.0), (
            "Watchdog observer did not detect file deletion within 10s"
        )

        rule, inst = engine.match_with_rule(msg)
        assert rule is None and inst is None

        engine.close()

    @pytest.mark.skipif(
        not _HAS_WATCHDOG,
        reason="watchdog package not installed",
    )
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ReadDirectoryChangesW has high latency; test flakes",
    )
    def test_watchdog_full_lifecycle(self, tmp_path: Path) -> None:
        """Full lifecycle with real watchdog: create → modify → delete → recreate."""
        engine = RoutingEngine(tmp_path, use_watchdog=True)
        engine.load_rules()
        assert engine._observer is not None

        msg = _make_msg()

        # Phase 1: No rules
        assert engine.match(msg) is None

        # Phase 2: Create file
        md = tmp_path / "lifecycle.md"
        _write_md(md, "step1")

        self._wait_for_dirty(engine, timeout=10.0)
        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step1"

        # Phase 3: Modify file
        _write_md(md, "step2")
        self._wait_for_dirty(engine, timeout=10.0)
        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step2"

        # Phase 4: Delete file
        md.unlink()
        self._wait_for_dirty(engine, timeout=10.0)
        rule, inst = engine.match_with_rule(msg)
        assert rule is None and inst is None

        # Phase 5: Recreate file
        _write_md(md, "step5")
        self._wait_for_dirty(engine, timeout=10.0)
        rule, inst = engine.match_with_rule(msg)
        assert rule is not None and rule.id == "step5"

        engine.close()

    @staticmethod
    def _wait_for_dirty(engine: RoutingEngine, timeout: float = 10.0) -> None:
        """Wait for the watchdog to set the dirty flag, using an Event."""
        dirty_event = threading.Event()
        original_mark_dirty = engine._mark_dirty

        def _mark_dirty_and_signal() -> None:
            original_mark_dirty()
            dirty_event.set()

        engine._mark_dirty = _mark_dirty_and_signal  # type: ignore[assignment]

        assert dirty_event.wait(timeout=timeout), (
            f"Watchdog observer did not detect change within {timeout}s"
        )
