"""
tests/unit/test_diagnose_orphan_dirs.py — Tests for orphaned workspace directory detection.

Covers: missing .chat_id, empty JSONL, missing JSONL, mixed healthy/orphan
directories, and edge cases (no workspace, no subdirs, non-directory entries).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.diagnose import CheckResult, check_orphaned_workspace_dirs


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace layout and return the workspace root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "whatsapp_data").mkdir()
    (workspace / ".data" / "messages").mkdir(parents=True)
    return workspace


def _make_healthy_chat(
    workspace: Path, dirname: str, chat_id: str = "12345_at_s_whatsapp_net"
) -> Path:
    """Create a healthy chat directory with .chat_id and a non-empty JSONL."""
    chat_dir = workspace / "whatsapp_data" / dirname
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / ".chat_id").write_text(chat_id, encoding="utf-8")

    jsonl = workspace / ".data" / "messages" / f"{dirname}.jsonl"
    jsonl.write_text(
        json.dumps({"role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )
    return chat_dir


# ── No workspace / empty cases ───────────────────────────────────────────────


class TestNoWorkspace:
    def test_skips_when_workspace_missing(self, tmp_path: Path):
        with patch("src.diagnose.WORKSPACE_DIR", str(tmp_path / "nonexistent")):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True
        assert "Skipped" in result.message

    def test_skips_when_whatsapp_data_missing(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True
        assert "Skipped" in result.message

    def test_no_chat_directories(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True
        assert "No chat directories" in result.message


# ── Healthy directories ──────────────────────────────────────────────────────


class TestHealthyDirectories:
    def test_single_healthy_dir(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        _make_healthy_chat(workspace, "chat_123")
        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True
        assert "healthy" in result.message.lower()

    def test_multiple_healthy_dirs(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        _make_healthy_chat(workspace, "chat_a")
        _make_healthy_chat(workspace, "chat_b")
        _make_healthy_chat(workspace, "chat_c")
        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True
        assert "3" in result.message


# ── Orphaned directories ─────────────────────────────────────────────────────


class TestMissingOriginFile:
    def test_missing_chat_id_file(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        # Create a chat dir WITHOUT .chat_id
        chat_dir = workspace / "whatsapp_data" / "orphan_1"
        chat_dir.mkdir()
        # But give it a valid JSONL so only .chat_id is missing
        jsonl = workspace / ".data" / "messages" / "orphan_1.jsonl"
        jsonl.write_text(
            json.dumps({"role": "user", "content": "hi"}) + "\n",
            encoding="utf-8",
        )

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is False
        assert "1 orphaned" in result.message
        assert any("missing .chat_id" in o for o in result.details["orphans"])


class TestEmptyJsonl:
    def test_empty_jsonl_file(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        chat_dir = workspace / "whatsapp_data" / "empty_jsonl"
        chat_dir.mkdir()
        (chat_dir / ".chat_id").write_text("test_id", encoding="utf-8")

        # Create an empty JSONL
        jsonl = workspace / ".data" / "messages" / "empty_jsonl.jsonl"
        jsonl.write_text("", encoding="utf-8")

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is False
        assert any("empty JSONL" in o for o in result.details["orphans"])


class TestMissingJsonl:
    def test_missing_jsonl_file(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        chat_dir = workspace / "whatsapp_data" / "no_jsonl"
        chat_dir.mkdir()
        (chat_dir / ".chat_id").write_text("test_id", encoding="utf-8")
        # No JSONL created at all

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is False
        assert any("no corresponding JSONL" in o for o in result.details["orphans"])


class TestMultipleIssues:
    def test_dir_with_both_missing_chat_id_and_empty_jsonl(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        chat_dir = workspace / "whatsapp_data" / "double_bad"
        chat_dir.mkdir()
        # No .chat_id
        # Empty JSONL
        jsonl = workspace / ".data" / "messages" / "double_bad.jsonl"
        jsonl.write_text("", encoding="utf-8")

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is False
        orphan_detail = result.details["orphans"][0]
        assert "missing .chat_id" in orphan_detail
        assert "empty JSONL" in orphan_detail


# ── Mixed healthy / orphan ────────────────────────────────────────────────────


class TestMixedDirectories:
    def test_healthy_and_orphan_mix(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        _make_healthy_chat(workspace, "healthy_1")

        # Orphan: no .chat_id, no JSONL
        orphan_dir = workspace / "whatsapp_data" / "orphan_1"
        orphan_dir.mkdir()

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is False
        assert "1 orphaned" in result.message
        assert "scanned 2" in result.message

    def test_ignores_files_in_whatsapp_data(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        _make_healthy_chat(workspace, "healthy_1")
        # A stray file (not a directory)
        (workspace / "whatsapp_data" / "notes.txt").write_text("ignore me")

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.passed is True


# ── Output format ────────────────────────────────────────────────────────────


class TestOutputFormat:
    def test_singular_noun_for_single_orphan(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        orphan_dir = workspace / "whatsapp_data" / "only_one"
        orphan_dir.mkdir()

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert "directory" in result.message  # singular

    def test_plural_noun_for_multiple_orphans(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        for name in ("orphan_a", "orphan_b"):
            (workspace / "whatsapp_data" / name).mkdir()

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert "directories" in result.message  # plural

    def test_details_capped_at_ten(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        # Create 15 orphan directories
        for i in range(15):
            (workspace / "whatsapp_data" / f"orphan_{i:02d}").mkdir()

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert result.details["total_orphans"] == 15
        assert len(result.details["orphans"]) == 10

    def test_hint_in_details_when_orphans_found(self, tmp_path: Path):
        workspace = _make_workspace(tmp_path)
        (workspace / "whatsapp_data" / "orphan").mkdir()

        with patch("src.diagnose.WORKSPACE_DIR", str(workspace)):
            result = check_orphaned_workspace_dirs(Path("config.json"))
        assert "hint" in result.details
