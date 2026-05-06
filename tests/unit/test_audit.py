"""
Tests for src/security/audit.py — Structured audit logging.

Covers:
- audit_log: structured event logging at various levels
- SkillAuditLogger: lifecycle, logging, hash_args, rotation, cleanup, close
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.security.audit import SkillAuditLogger, audit_log


# ─────────────────────────────────────────────────────────────────────────────
# audit_log
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditLog:
    """Tests for the audit_log() function."""

    def test_logs_at_warning_by_default(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            audit_log("test_event", {"key": "value"})
        assert "test_event" in caplog.text
        assert "key=value" in caplog.text

    def test_logs_with_custom_prefix(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            audit_log("evt", {"x": "1"}, prefix="MYPREFIX")
        assert "MYPREFIX" in caplog.text

    def test_logs_at_info_level(self, caplog):
        with caplog.at_level(logging.INFO, logger="security.audit"):
            audit_log("info_evt", {"a": "b"}, level=logging.INFO)
        assert "info_evt" in caplog.text

    def test_multiple_details(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            audit_log("multi", {"k1": "v1", "k2": "v2"})
        assert "k1=v1" in caplog.text
        assert "k2=v2" in caplog.text

    def test_empty_details(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            audit_log("empty", {})
        assert "AUDIT" in caplog.text

    def test_extra_fields_attached(self, caplog):
        with caplog.at_level(logging.WARNING, logger="security.audit"):
            audit_log("extra_test", {"foo": "bar"})
        record = caplog.records[0]
        assert getattr(record, "audit_audit", None) == "extra_test"


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class TestSkillAuditLoggerInit:
    """Tests for SkillAuditLogger initialization."""

    def test_creates_log_directory(self, tmp_path: Path):
        log_dir = tmp_path / "audit_logs"
        logger = SkillAuditLogger(log_dir)
        assert log_dir.is_dir()
        logger.close()

    def test_creates_nested_directory(self, tmp_path: Path):
        log_dir = tmp_path / "a" / "b" / "c"
        logger = SkillAuditLogger(log_dir)
        assert log_dir.is_dir()
        logger.close()

    def test_chain_hashes_disabled_by_default(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        assert logger._prev_hash is None
        logger.close()

    def test_chain_hashes_enabled(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path, chain_hashes=True)
        assert logger._prev_hash is not None
        assert len(logger._prev_hash) == 64
        logger.close()


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — log entries
# ─────────────────────────────────────────────────────────────────────────────


class TestSkillAuditLoggerLog:
    """Tests for SkillAuditLogger.log()."""

    def test_appends_jsonl_entry(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.log("chat1", "shell", "abc123", True, "ok")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["chat_id"] == "chat1"
        assert entry["skill_name"] == "shell"
        assert entry["args_hash"] == "abc123"
        assert entry["allowed"] is True
        assert entry["result_summary"] == "ok"
        logger.close()

    def test_entry_has_timestamp(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.log("c1", "skill", "h", True, "done")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert "timestamp" in entry
        assert "T" in entry["timestamp"]  # ISO format
        logger.close()

    def test_multiple_entries_append(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.log("c1", "s1", "h1", True, "a")
        logger.log("c2", "s2", "h2", False, "b")
        logger.log("c3", "s3", "h3", True, "c")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        logger.close()

    def test_denied_entry(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.log("c1", "shell", "h1", False, "blocked: path traversal")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert entry["allowed"] is False
        assert "blocked" in entry["result_summary"]
        logger.close()

    def test_chain_hash_included_when_enabled(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path, chain_hashes=True)
        logger.log("c1", "skill", "h1", True, "ok")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert "_prev_hash" in entry
        assert len(entry["_prev_hash"]) == 64
        logger.close()

    def test_chain_hash_updates_between_entries(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path, chain_hashes=True)
        logger.log("c1", "skill", "h1", True, "a")
        logger.log("c1", "skill", "h2", True, "b")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        e1 = json.loads(lines[0])
        e2 = json.loads(lines[1])
        assert e1["_prev_hash"] != e2["_prev_hash"]
        logger.close()

    def test_no_chain_hash_when_disabled(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path, chain_hashes=False)
        logger.log("c1", "skill", "h1", True, "ok")

        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        entry = json.loads(lines[0])
        assert "_prev_hash" not in entry
        logger.close()

    def test_log_after_close_is_noop(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.close()
        # Should not raise — _path is None
        logger.log("c1", "skill", "h1", True, "post-close")


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — hash_args
# ─────────────────────────────────────────────────────────────────────────────


class TestHashArgs:
    """Tests for SkillAuditLogger.hash_args()."""

    def test_returns_32_char_hex(self):
        result = SkillAuditLogger.hash_args("some args")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        assert SkillAuditLogger.hash_args("x") == SkillAuditLogger.hash_args("x")

    def test_different_inputs_different_hashes(self):
        assert SkillAuditLogger.hash_args("a") != SkillAuditLogger.hash_args("b")

    def test_empty_string(self):
        result = SkillAuditLogger.hash_args("")
        assert len(result) == 32


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — rotation
# ─────────────────────────────────────────────────────────────────────────────


class TestSkillAuditLoggerRotation:
    """Tests for file rotation when MAX_FILE_SIZE_BYTES is exceeded."""

    def test_rotation_shifts_files(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        # Set a tiny max size to trigger rotation
        logger.MAX_FILE_SIZE_BYTES = 100

        # Write enough entries to exceed 100 bytes
        for i in range(20):
            logger.log("c1", "skill", "h", True, f"entry-{i}")

        # The main audit.jsonl should have been rotated
        rotated = list(tmp_path.glob("audit.*.jsonl"))
        assert len(rotated) >= 1
        logger.close()

    def test_rotation_preserves_entries(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.MAX_FILE_SIZE_BYTES = 100

        for i in range(20):
            logger.log("c1", "skill", "h", True, f"entry-{i}")

        # Count total entries across all files
        total = 0
        for f in tmp_path.glob("audit*.jsonl"):
            total += len(f.read_text().strip().splitlines())
        assert total == 20
        logger.close()

    def test_max_rotated_files_enforced(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.MAX_FILE_SIZE_BYTES = 50
        logger.MAX_ROTATED_FILES = 3

        for i in range(100):
            logger.log("c1", "skill", "h", True, f"entry-{i}")

        rotated = sorted(tmp_path.glob("audit.*.jsonl"))
        assert len(rotated) <= logger.MAX_ROTATED_FILES
        logger.close()


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — cleanup_old_logs
# ─────────────────────────────────────────────────────────────────────────────


class TestCleanupOldLogs:
    """Tests for cleanup_old_logs() age and count pruning."""

    def test_removes_old_files(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)

        # Create a rotated file with an old mtime
        old_file = tmp_path / "audit.1.jsonl"
        old_file.write_text("old\n")
        old_age = time.time() - 86400 * 31  # 31 days old
        os.utime(old_file, (old_age, old_age))

        pruned = logger.cleanup_old_logs(max_age_days=30, max_files=100)
        assert pruned == 1
        assert not old_file.exists()
        logger.close()

    def test_keeps_recent_files(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)

        recent_file = tmp_path / "audit.1.jsonl"
        recent_file.write_text("recent\n")

        pruned = logger.cleanup_old_logs(max_age_days=30, max_files=100)
        assert pruned == 0
        assert recent_file.exists()
        logger.close()

    def test_prunes_by_count(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)

        # Create more files than the limit
        for i in range(1, 6):
            f = tmp_path / f"audit.{i}.jsonl"
            f.write_text(f"file-{i}\n")

        pruned = logger.cleanup_old_logs(max_age_days=999, max_files=2)
        assert pruned >= 3  # At least 3 should be removed (5 - 2 = 3)
        remaining = list(tmp_path.glob("audit.*.jsonl"))
        assert len(remaining) <= 2
        logger.close()

    def test_does_not_remove_active_file(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.log("c1", "s", "h", True, "active")

        active = tmp_path / "audit.jsonl"
        assert active.exists()

        logger.cleanup_old_logs(max_age_days=0, max_files=0)
        assert active.exists()
        logger.close()

    def test_returns_zero_on_empty_dir(self, tmp_path: Path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        logger = SkillAuditLogger(empty_dir)
        pruned = logger.cleanup_old_logs(max_age_days=1, max_files=0)
        assert pruned == 0
        logger.close()

    def test_handles_oserror_gracefully(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        # Create a file then make it unreadable
        f = tmp_path / "audit.1.jsonl"
        f.write_text("data\n")

        with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            pruned = logger.cleanup_old_logs(max_age_days=1, max_files=0)
        assert pruned == 0
        logger.close()


# ─────────────────────────────────────────────────────────────────────────────
# SkillAuditLogger — close
# ─────────────────────────────────────────────────────────────────────────────


class TestSkillAuditLoggerClose:
    """Tests for SkillAuditLogger.close() lifecycle."""

    def test_close_sets_path_to_none(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        assert logger._path is not None
        logger.close()
        assert logger._path is None

    def test_close_idempotent(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.close()
        logger.close()  # second close should not raise

    def test_log_after_close_is_noop(self, tmp_path: Path):
        logger = SkillAuditLogger(tmp_path)
        logger.close()
        # Should silently skip — no file to write to
        logger.log("c1", "skill", "h", True, "post-close")

        # No audit.jsonl should exist (close didn't create one)
        # But if it did exist from before close, verify no new entry
        audit_file = tmp_path / "audit.jsonl"
        if audit_file.exists():
            lines = audit_file.read_text().strip().splitlines()
            # All entries should be from before close
            for line in lines:
                entry = json.loads(line)
                assert entry["result_summary"] != "post-close"
