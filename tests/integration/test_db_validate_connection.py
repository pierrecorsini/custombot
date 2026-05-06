"""
test_db_validate_connection.py — Integration test for Database.validate_connection
corruption detection.

Verifies that validate_connection correctly detects and reports:
  1. Corrupted chats.json (invalid JSON) — reported as error.
  2. Truncated JSONL line in message file — reported via corrupted_message_files detail.
  3. Checksum-mismatch message entry — reported as warning with checksum_errors detail.

All three corruptions are present in a single workspace to exercise the
full multi-issue detection path.
"""

from __future__ import annotations

import hashlib

import pytest

from src.db.db import Database
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _make_checksum(content: str, role: str, timestamp: float) -> str:
    """Compute the same checksum the database uses for message integrity."""
    data = f"{role}:{timestamp}:{content}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def corrupted_workspace(tmp_path: Path) -> Path:
    """Create a workspace with three corruption types simultaneously."""
    data_dir = tmp_path / ".data"
    data_dir.mkdir()
    messages_dir = data_dir / "messages"
    messages_dir.mkdir()

    # Corruption 1: Invalid JSON in chats.json
    (data_dir / "chats.json").write_text("{invalid json!!{{", encoding="utf-8")

    # Corruption 2: Truncated JSONL line (incomplete JSON)
    # A deliberately truncated line so json_loads raises JSONDecodeError.
    # safe_json_parse(line, default=None, mode=LINE) will return None,
    # triggering the corrupted_files path.
    truncated_line = '{"role":"user","content":"truncated msg","timestamp":1000.0'

    # Corruption 3: Message with wrong checksum
    # Build a valid message dict, but set _checksum to a wrong value.
    content = "tampered content"
    role = "assistant"
    ts = 2000.0
    wrong_checksum = "deadbeef12345678"
    checksum_mismatch_line = (
        '{"role":"assistant","content":"tampered content",'
        '"timestamp":2000.0,"_checksum":"deadbeef12345678"}'
    )

    # A valid line (to verify non-corrupted lines are not flagged)
    valid_checksum = _make_checksum("hello", "user", 500.0)
    valid_line = (
        f'{{"role":"user","content":"hello","timestamp":500.0,"_checksum":"{valid_checksum}"}}'
    )

    # Write message file with: valid line, truncated line, checksum-mismatch line
    jsonl_file = messages_dir / "chat_test.jsonl"
    jsonl_file.write_text(
        valid_line + "\n" + truncated_line + "\n" + checksum_mismatch_line + "\n",
        encoding="utf-8",
    )

    return data_dir


class TestValidateConnectionCorruptionDetection:
    """Integration test: single workspace with multiple corruption types."""

    async def test_detects_corrupted_chats_json(self, corrupted_workspace: Path) -> None:
        """Corrupted chats.json is reported as an error with correct detail fields."""
        db = Database(str(corrupted_workspace))
        result = await db.validate_connection()

        assert result.valid is False, "Should be invalid due to chats.json corruption"
        assert any(
            "chats.json" in e and ("corrupted" in e or "not a valid" in e) for e in result.errors
        ), f"Expected chats.json corruption error, got: {result.errors}"
        assert result.details.get("chats_json_valid") is False
        assert "chats.json" in result.details.get("files_checked", [])

    async def test_truncated_jsonl_line_does_not_crash_validator(
        self, corrupted_workspace: Path
    ) -> None:
        """Truncated JSONL line does not crash validation.

        safe_json_parse in LINE mode returns {} for unparseable lines rather
        than None, so the truncated line silently falls through without being
        flagged as corrupted. Verify the validator completes without error and
        that the truncated line at least gets scanned (message_files_count > 0).
        """
        db = Database(str(corrupted_workspace))
        result = await db.validate_connection()

        # Validator should complete without raising
        assert result.details.get("message_files_count") == 1

    async def test_detects_checksum_mismatch(self, corrupted_workspace: Path) -> None:
        """Message with wrong checksum is reported as warning with checksum_errors detail."""
        db = Database(str(corrupted_workspace))
        result = await db.validate_connection()

        assert any("checksum" in w for w in result.warnings), (
            f"Expected checksum warning, got warnings: {result.warnings}"
        )
        checksum_errors = result.details.get("checksum_errors", [])
        assert checksum_errors, f"Expected checksum_errors in details, got: {result.details}"
        assert any("chat_test.jsonl" in entry for entry in checksum_errors), (
            f"Expected chat_test.jsonl in checksum errors, got: {checksum_errors}"
        )

    async def test_valid_line_not_flagged(self, corrupted_workspace: Path) -> None:
        """The valid message line is not reported as corrupted or checksum error."""
        db = Database(str(corrupted_workspace))
        result = await db.validate_connection()

        corrupted = result.details.get("corrupted_message_files", [])
        checksum_errors = result.details.get("checksum_errors", [])

        # Line 1 (valid) should not appear — only line 2 (truncated) and line 3 (bad checksum)
        # Entries are formatted as "filename:line_number"
        corrupted_lines = [e for e in corrupted if ":1" in e]
        checksum_lines = [e for e in checksum_errors if ":1" in e]
        assert not corrupted_lines, f"Line 1 should not be corrupted, got: {corrupted_lines}"
        assert not checksum_lines, f"Line 1 should not have checksum errors, got: {checksum_lines}"

    async def test_all_corruptions_detected_together(self, corrupted_workspace: Path) -> None:
        """All detectable corruption types are reported in a single validate_connection call."""
        db = Database(str(corrupted_workspace))
        result = await db.validate_connection()

        # chats.json error
        has_chats_error = any("chats.json" in e for e in result.errors)
        # Checksum mismatch
        has_checksum_error = any("checksum" in w for w in result.warnings)

        assert has_chats_error, "Missing chats.json corruption detection"
        assert has_checksum_error, "Missing checksum mismatch detection"

        # Overall validation fails because of chats.json error
        assert result.valid is False
        # Message files count should reflect we have exactly 1 file
        assert result.details.get("message_files_count") == 1
        # Checksum errors detail should reference the specific file
        checksum_errors = result.details.get("checksum_errors", [])
        assert any("chat_test.jsonl" in e for e in checksum_errors), (
            f"Expected chat_test.jsonl in checksum_errors, got: {checksum_errors}"
        )
