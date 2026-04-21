"""
tests/unit/test_workspace_integrity.py — Tests for startup workspace integrity checks.

Covers: data dir verification, stale temp cleanup, JSONL spot-checks,
SQLite lock detection, and auto-repair behaviour.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from src.workspace_integrity import (
    IntegrityResult,
    _check_data_dir,
    _check_sqlite_not_locked,
    _clean_stale_temps,
    _run_sync_checks,
    _spot_check_jsonl,
    check_workspace_integrity,
)


# ── _check_data_dir ──────────────────────────────────────────────────────────


class TestCheckDataDir:
    def test_creates_missing_dir(self, tmp_path: Path):
        data_dir = tmp_path / "workspace" / ".data"
        result = IntegrityResult()
        _check_data_dir(data_dir, result)
        assert data_dir.exists()
        assert "Created missing directory" in result.repaired[0]

    def test_existing_writable_dir_is_ok(self, tmp_path: Path):
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        result = IntegrityResult()
        _check_data_dir(data_dir, result)
        assert not result.warnings
        assert not result.errors

    def test_non_writable_dir_reports_error(self, tmp_path: Path):
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        # Make non-writable (skip on Windows where chmod is unreliable)
        if os.name != "nt":
            data_dir.chmod(0o444)
            result = IntegrityResult()
            _check_data_dir(data_dir, result)
            assert result.errors
            data_dir.chmod(0o755)  # Restore for cleanup
        else:
            pytest.skip("chmod not reliable on Windows")


# ── _clean_stale_temps ───────────────────────────────────────────────────────


class TestCleanStaleTemps:
    def test_removes_old_tmp_files(self, tmp_path: Path):
        stale = tmp_path / "workspace" / "stale.tmp"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old data")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(stale, (old_time, old_time))

        result = IntegrityResult()
        _clean_stale_temps(tmp_path / "workspace", result)
        assert not stale.exists()
        assert any("1 stale" in r for r in result.repaired)

    def test_keeps_recent_tmp_files(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        recent = workspace / "recent.tmp"
        recent.write_text("new data")

        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        assert recent.exists()
        assert not result.repaired

    def test_no_tmp_files_is_ok(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        assert not result.repaired

    def test_removes_stale_temps_in_nested_subdirs(self, tmp_path: Path):
        """Stale .tmp files from crashed atomic writes in nested dirs are removed."""
        workspace = tmp_path / "workspace"
        deep_dir = workspace / ".data" / "messages"
        deep_dir.mkdir(parents=True)

        stale = deep_dir / "write_atomic_abc123.tmp"
        stale.write_text("partial write data")
        old_time = time.time() - 7200  # 2 hours ago
        os.utime(stale, (old_time, old_time))

        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        assert not stale.exists()
        assert any("1 stale" in r for r in result.repaired)

    def test_file_just_under_cutoff_is_kept(self, tmp_path: Path):
        """A .tmp file just under the 1-hour boundary is NOT removed."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        boundary = workspace / "boundary.tmp"
        boundary.write_text("just under 1 hour old")
        # Set mtime to 1 second newer than the cutoff (should be kept)
        from src.constants import WORKSPACE_STALE_TEMP_MAX_AGE_HOURS
        just_under = time.time() - (WORKSPACE_STALE_TEMP_MAX_AGE_HOURS * 3600) + 1
        os.utime(boundary, (just_under, just_under))

        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        assert boundary.exists()
        assert not result.repaired

    def test_multiple_stale_temps_across_dirs(self, tmp_path: Path):
        """Multiple stale .tmp files across subdirectories are all removed."""
        workspace = tmp_path / "workspace"
        dir_a = workspace / ".data" / "messages"
        dir_b = workspace / ".data" / "backups"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        old_time = time.time() - 7200
        stale_files = []
        for i, parent in enumerate([dir_a, dir_b, workspace]):
            f = parent / f"crash_{i}.tmp"
            f.write_text(f"stale data {i}")
            os.utime(f, (old_time, old_time))
            stale_files.append(f)

        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        for f in stale_files:
            assert not f.exists()
        assert any("3 stale" in r for r in result.repaired)

    def test_tmp_directory_is_not_removed(self, tmp_path: Path):
        """.tmp directories (not files) are left intact."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        tmp_dir = workspace / "temp_work.tmp"
        tmp_dir.mkdir()
        old_time = time.time() - 7200
        os.utime(tmp_dir, (old_time, old_time))

        result = IntegrityResult()
        _clean_stale_temps(workspace, result)
        assert tmp_dir.exists()
        assert not result.repaired

    def test_unremovable_file_reports_warning(self, tmp_path: Path):
        """A stale .tmp file that cannot be deleted logs a warning."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        stale = workspace / "locked.tmp"
        stale.write_text("stuck")
        old_time = time.time() - 7200
        os.utime(stale, (old_time, old_time))

        if os.name == "nt":
            pytest.skip("chmod not reliable on Windows")

        stale.chmod(0o444)
        # Also make parent non-writable to prevent unlink
        workspace.chmod(0o555)

        try:
            result = IntegrityResult()
            _clean_stale_temps(workspace, result)
            assert result.warnings
        finally:
            workspace.chmod(0o755)
            stale.chmod(0o644)


# ── _spot_check_jsonl ────────────────────────────────────────────────────────


class TestSpotCheckJsonl:
    def test_valid_jsonl_no_warnings(self, tmp_path: Path):
        messages = tmp_path / "messages"
        messages.mkdir()
        msg_file = messages / "chat-123.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "hi", "id": "1", "timestamp": 1.0}) + "\n",
            json.dumps({"role": "assistant", "content": "hello", "id": "2", "timestamp": 2.0}) + "\n",
        ]
        msg_file.write_text("".join(lines))

        result = IntegrityResult()
        _spot_check_jsonl(messages, result)
        assert not result.warnings

    def test_corrupt_first_line_detected(self, tmp_path: Path):
        messages = tmp_path / "messages"
        messages.mkdir()
        msg_file = messages / "chat-bad.jsonl"
        msg_file.write_text("NOT JSON\n")

        result = IntegrityResult()
        _spot_check_jsonl(messages, result)
        assert result.warnings
        assert any("chat-bad" in w for w in result.warnings)

    def test_corrupt_last_line_detected(self, tmp_path: Path):
        messages = tmp_path / "messages"
        messages.mkdir()
        msg_file = messages / "chat-tail.jsonl"
        # Write a file larger than 1024 bytes with corrupt last line
        good_line = json.dumps({"role": "user", "content": "x" * 200, "id": "1", "timestamp": 1.0}) + "\n"
        content = good_line * 10 + "BROKEN LAST LINE"
        msg_file.write_text(content)

        result = IntegrityResult()
        _spot_check_jsonl(messages, result)
        assert result.warnings
        assert any("chat-tail" in w for w in result.warnings)

    def test_empty_file_skipped(self, tmp_path: Path):
        messages = tmp_path / "messages"
        messages.mkdir()
        (messages / "empty.jsonl").write_text("")

        result = IntegrityResult()
        _spot_check_jsonl(messages, result)
        assert not result.warnings

    def test_nonexistent_dir_is_ok(self, tmp_path: Path):
        result = IntegrityResult()
        _spot_check_jsonl(tmp_path / "nope", result)
        assert not result.warnings


# ── _check_sqlite_not_locked ─────────────────────────────────────────────────


class TestCheckSqliteNotLocked:
    def test_unlocked_db_is_ok(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INT)")
        conn.commit()
        conn.close()

        result = IntegrityResult()
        _check_sqlite_not_locked(db_path, result)
        assert not result.warnings

    def test_nonexistent_db_is_ok(self, tmp_path: Path):
        result = IntegrityResult()
        _check_sqlite_not_locked(tmp_path / "nope.db", result)
        assert not result.warnings


# ── check_workspace_integrity (async) ────────────────────────────────────────


class TestCheckWorkspaceIntegrity:
    @pytest.mark.asyncio
    async def test_clean_workspace_passes(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        (workspace / ".data" / "messages").mkdir(parents=True)

        result = await check_workspace_integrity(workspace)
        assert not result.has_issues

    @pytest.mark.asyncio
    async def test_stale_temp_cleaned(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / ".data" / "messages").mkdir(parents=True)

        stale = workspace / "stale.tmp"
        stale.write_text("old")
        old_time = time.time() - 7200
        os.utime(stale, (old_time, old_time))

        result = await check_workspace_integrity(workspace)
        assert not stale.exists()
        assert any("stale" in r for r in result.repaired)

    @pytest.mark.asyncio
    async def test_missing_data_dir_auto_created(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        result = await check_workspace_integrity(workspace)
        assert (workspace / ".data").exists()
        assert any("Created" in r for r in result.repaired)
