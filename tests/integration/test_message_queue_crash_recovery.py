"""
test_message_queue_crash_recovery.py — Integration test for crash recovery
with a partially-written JSONL file.

Verifies that _load_pending() recovers all valid entries from a queue file
whose last line was truncated (simulating a crash mid-write), and that the
corruption is logged for observability.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.message_queue import MessageQueue, MessageStatus, get_message_queue
from tests.unit.test_message_queue import FakeIncomingMessage, make_incoming


class TestCrashRecoveryPartialWrite:
    """Integration test: enqueue via API → persist → corrupt file → reconnect → verify."""

    async def test_recovers_valid_entries_after_truncated_write(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Full lifecycle: enqueue, persist, truncate last line, reconnect, verify recovery.

        Simulates a production crash:
        1. Messages enqueued and persisted via the real API.
        2. Process crashes mid-write, leaving a truncated JSON line.
        3. On restart, _load_pending() recovers all valid entries and logs corruption.
        """
        data_dir = tmp_path / "data"
        messages = [
            ("crash-1", "chat-A", "first message"),
            ("crash-2", "chat-B", "second message"),
            ("crash-3", "chat-A", "third message"),
        ]

        # Phase 1: enqueue messages and persist to disk
        async with get_message_queue(str(data_dir)) as queue:
            for msg_id, chat_id, text in messages:
                await queue.enqueue(
                    FakeIncomingMessage(message_id=msg_id, chat_id=chat_id, text=text)
                )

        # Phase 2: simulate crash — append a truncated JSON line
        qfile = data_dir / "message_queue.jsonl"
        original = qfile.read_text(encoding="utf-8")
        truncated = '{"message_id": "crash-mid-write", "chat_id": "chat-C", "text'
        qfile.write_text(original + truncated, encoding="utf-8")

        # Phase 3: reconnect and verify recovery
        with caplog.at_level(logging.WARNING, logger="src.message_queue"):
            async with get_message_queue(str(data_dir)) as queue2:
                assert await queue2.get_pending_count() == 3

                for msg_id, chat_id, text in messages:
                    assert msg_id in queue2._pending
                    assert queue2._pending[msg_id].chat_id == chat_id
                    assert queue2._pending[msg_id].text == text

                assert "crash-mid-write" not in queue2._pending

        # Corruption recovery must be logged
        assert any(
            "recovered" in r.message and "corrupted" in r.message
            for r in caplog.records
        ), f"Expected corruption recovery log, got: {[r.message for r in caplog.records]}"

    async def test_corruption_result_tracks_truncated_line(self, tmp_path: Path):
        """_last_corruption_result records line number of the truncated entry."""
        data_dir = tmp_path / "data"

        async with get_message_queue(str(data_dir)) as queue:
            await queue.enqueue(make_incoming(message_id="pre-1", text="hello"))
            await queue.enqueue(make_incoming(message_id="pre-2", text="world"))

        # Append truncated line as the 3rd line
        qfile = data_dir / "message_queue.jsonl"
        original = qfile.read_text(encoding="utf-8")
        qfile.write_text(original + '{"message_id": "broken", "chat_id": "x"', encoding="utf-8")

        async with get_message_queue(str(data_dir)) as queue2:
            result = queue2._last_corruption_result
            assert result is not None
            assert result.is_corrupted is True
            assert 3 in result.corrupted_lines
            assert result.valid_lines == 2
            assert result.pending_lines == 2

    async def test_survives_reconnect_after_recovery(self, tmp_path: Path):
        """Queue file after crash recovery survives a second clean reconnect."""
        data_dir = tmp_path / "data"

        async with get_message_queue(str(data_dir)) as queue:
            await queue.enqueue(make_incoming(message_id="survive-1", text="data"))
            await queue.enqueue(make_incoming(message_id="survive-2", text="more"))

        # Corrupt last line
        qfile = data_dir / "message_queue.jsonl"
        original = qfile.read_text(encoding="utf-8")
        qfile.write_text(original + "GARBAGE LINE\n", encoding="utf-8")

        # First reconnect triggers recovery + eager eviction
        async with get_message_queue(str(data_dir)) as queue2:
            assert await queue2.get_pending_count() == 2

        # Second reconnect on the cleaned file should work cleanly
        async with get_message_queue(str(data_dir)) as queue3:
            assert await queue3.get_pending_count() == 2
            assert "survive-1" in queue3._pending
            assert "survive-2" in queue3._pending
            assert queue3._last_corruption_result is not None
            assert queue3._last_corruption_result.is_corrupted is False
