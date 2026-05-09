"""
test_message_queue_crash_recovery.py — Integration tests for MessageQueue.

Covers:
- Crash recovery with a partially-written JSONL file.
- Concurrent flush and enqueue under the swap-buffers flush loop.
- Persistence corruption recovery (CRC32 mismatch, garbled data, validate→repair).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from src.message_queue import MessageQueue, MessageStatus, get_message_queue
from src.message_queue_persistence import _encode_record
from tests.unit.test_message_queue import FakeIncomingMessage, make_incoming
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest
    from pathlib import Path


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
        assert any("recovered" in r.message and "corrupted" in r.message for r in caplog.records), (
            f"Expected corruption recovery log, got: {[r.message for r in caplog.records]}"
        )

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


class TestConcurrentFlushAndEnqueue:
    """Integration test for concurrent flush and enqueue.

    Verifies that the swap-buffers flush loop (_flush_loop) correctly
    persists messages to disk when enqueue calls arrive concurrently
    from multiple coroutines. No data should be lost or corrupted.
    """

    async def test_parallel_enqueues_flushed_to_disk(self, tmp_path: Path):
        """Messages enqueued in parallel are persisted after flush cycle completes."""
        data_dir = tmp_path / "data"
        msg_count = 50

        async with get_message_queue(str(data_dir)) as queue:
            # Enqueue from multiple parallel coroutines
            tasks = [
                queue.enqueue(
                    FakeIncomingMessage(
                        message_id=f"flush-{i}",
                        chat_id=f"chat-{i % 5}",
                        text=f"concurrent message {i}",
                    )
                )
                for i in range(msg_count)
            ]
            await asyncio.gather(*tasks)

            assert await queue.get_pending_count() == msg_count

            # Wait for the flush loop to drain the buffer
            await asyncio.sleep(0.2)

        # After close, verify all messages on disk via fresh reconnect
        async with get_message_queue(str(data_dir)) as queue2:
            assert await queue2.get_pending_count() == msg_count
            for i in range(msg_count):
                msg = queue2._pending.get(f"flush-{i}")
                assert msg is not None, f"Missing flush-{i}"
                assert msg.text == f"concurrent message {i}"

    async def test_flush_loop_drains_without_data_loss(self, tmp_path: Path):
        """Flush loop swap-buffers pattern loses no messages under burst traffic."""
        data_dir = tmp_path / "data"

        async with get_message_queue(str(data_dir)) as queue:
            # Burst: enqueue in waves separated by flush intervals
            for wave in range(3):
                tasks = [
                    queue.enqueue(
                        FakeIncomingMessage(
                            message_id=f"wave{wave}-{i}",
                            chat_id="chat-burst",
                            text=f"wave {wave} msg {i}",
                        )
                    )
                    for i in range(20)
                ]
                await asyncio.gather(*tasks)
                # Give flush loop time to swap and drain between waves
                await asyncio.sleep(0.1)

            assert await queue.get_pending_count() == 60

        # Verify all 60 messages survived on disk
        async with get_message_queue(str(data_dir)) as queue2:
            assert await queue2.get_pending_count() == 60
            for wave in range(3):
                for i in range(20):
                    assert f"wave{wave}-{i}" in queue2._pending

    async def test_jsonl_file_valid_after_concurrent_flush(self, tmp_path: Path):
        """On-disk JSONL is well-formed after concurrent enqueue + flush."""
        data_dir = tmp_path / "data"

        async with get_message_queue(str(data_dir)) as queue:
            tasks = [
                queue.enqueue(
                    FakeIncomingMessage(
                        message_id=f"jsonl-{i}",
                        chat_id="chat-jsonl",
                        text=f"msg {i}",
                    )
                )
                for i in range(30)
            ]
            await asyncio.gather(*tasks)
            await asyncio.sleep(0.15)

        # Parse the file manually — every line must be valid JSON
        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 30

        parsed_ids = set()
        for line in lines:
            data = json.loads(line)
            assert "message_id" in data
            assert "status" in data
            if data["status"] == "pending":
                parsed_ids.add(data["message_id"])

        # All 30 enqueued IDs must appear as pending in the file
        for i in range(30):
            assert f"jsonl-{i}" in parsed_ids


class TestPersistenceCorruptionRecovery:
    """Integration tests for persistence-layer corruption recovery.

    Verifies that the queue recovers gracefully when the JSONL file
    contains CRC32 checksum mismatches, garbled binary data, or mixed
    valid/corrupted entries.  Exercises the load_pending() → validate()
    → repair() → load_valid_lines_sync() recovery pipeline.
    """

    async def test_skips_crc32_checksum_mismatch_lines(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Lines with valid CRC format but wrong checksum are skipped and logged."""
        data_dir = tmp_path / "data"
        qfile = data_dir / "message_queue.jsonl"

        # Write a valid entry, a CRC-mismatch entry, then another valid entry
        valid1 = _encode_record(
            make_incoming(message_id="ok-1", text="first").__dict__
        )
        valid2 = _encode_record(
            make_incoming(message_id="ok-2", text="last").__dict__
        )

        # Build a CRC-guarded line with a deliberately wrong checksum
        payload = base64.b64encode(b"anything").decode("ascii")
        bad_crc_line = f"deadbeef:{payload}"

        data_dir.mkdir(parents=True, exist_ok=True)
        qfile.write_text(
            f"{valid1}\n{bad_crc_line}\n{valid2}\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="src.message_queue_persistence"):
            async with get_message_queue(str(data_dir)) as queue:
                assert await queue.get_pending_count() == 2
                assert "ok-1" in queue._pending
                assert "ok-2" in queue._pending

                result = queue._last_corruption_result
                assert result is not None
                assert result.is_corrupted is True
                assert 2 in result.checksum_mismatches
                assert result.valid_lines == 2

        assert any(
            "recovered" in r.message and "corrupted" in r.message
            for r in caplog.records
        )

    async def test_skips_garbled_binary_lines(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Completely garbled binary lines are skipped without crashing."""
        data_dir = tmp_path / "data"
        qfile = data_dir / "message_queue.jsonl"

        valid = _encode_record(
            make_incoming(message_id="survive-1", text="intact").__dict__
        )
        garbage = "\x00\x01\x02\xff\xfe\xfdNOT_BASE64_AT_ALL!!!"

        data_dir.mkdir(parents=True, exist_ok=True)
        qfile.write_text(
            f"{valid}\n{garbage}\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="src.message_queue"):
            async with get_message_queue(str(data_dir)) as queue:
                assert await queue.get_pending_count() == 1
                assert "survive-1" in queue._pending

                result = queue._last_corruption_result
                assert result is not None
                assert result.is_corrupted is True
                assert 2 in result.corrupted_lines

    async def test_validate_repair_removes_corrupted_lines(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """validate() on a corrupted file detects issues; repair() removes bad lines."""
        data_dir = tmp_path / "data"

        # Enqueue 3 valid messages
        async with get_message_queue(str(data_dir)) as queue:
            await queue.enqueue(make_incoming(message_id="keep-1", text="first"))
            await queue.enqueue(make_incoming(message_id="keep-2", text="second"))
            await queue.enqueue(make_incoming(message_id="keep-3", text="third"))

        # Corrupt: insert garbled lines between valid entries
        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

        corrupted_lines = [lines[0], "GARBAGE_CORRUPT_LINE", lines[1], "deadbeef:AAAA", lines[2]]
        qfile.write_text("\n".join(corrupted_lines) + "\n", encoding="utf-8")

        # Validate the file directly BEFORE queue auto-eviction cleans it
        from src.message_queue_persistence import QueuePersistence
        persistence = QueuePersistence(qfile)
        detection = persistence.validate_sync()
        assert detection.is_corrupted is True
        assert len(detection.corrupted_lines) + len(detection.checksum_mismatches) == 2

        # Now connect the queue — load_pending auto-evicts corrupt lines
        with caplog.at_level(logging.WARNING, logger="src.message_queue_persistence"):
            async with get_message_queue(str(data_dir)) as queue:
                # Auto-eviction recovered the 3 valid entries
                assert await queue.get_pending_count() == 3
                for mid in ("keep-1", "keep-2", "keep-3"):
                    assert mid in queue._pending

                result = queue._last_corruption_result
                assert result is not None
                assert result.is_corrupted is True
                assert result.valid_lines == 3

        # Backup file was created during auto-eviction
        backup_dir = data_dir / "backups"
        assert backup_dir.exists()
        backups = list(backup_dir.glob("*.jsonl.bak"))
        assert len(backups) >= 1

    async def test_reconnect_after_auto_eviction_has_no_corruption(
        self, tmp_path: Path
    ):
        """After load_pending auto-evicts corrupt lines, a fresh reconnect is clean."""
        data_dir = tmp_path / "data"

        async with get_message_queue(str(data_dir)) as queue:
            await queue.enqueue(make_incoming(message_id="clean-1", text="data"))

        # Corrupt with a bad line
        qfile = data_dir / "message_queue.jsonl"
        original = qfile.read_text(encoding="utf-8")
        qfile.write_text(original + "CORRUPT_LINE_HERE\n", encoding="utf-8")

        # First reconnect: load_pending auto-evicts the corrupt line
        async with get_message_queue(str(data_dir)) as queue:
            assert await queue.get_pending_count() == 1
            assert "clean-1" in queue._pending
            assert queue._last_corruption_result is not None
            assert queue._last_corruption_result.is_corrupted is True

        # Second reconnect on the auto-cleaned file should report no corruption
        async with get_message_queue(str(data_dir)) as queue:
            assert await queue.get_pending_count() == 1
            assert "clean-1" in queue._pending
            assert queue._last_corruption_result is not None
            assert queue._last_corruption_result.is_corrupted is False
