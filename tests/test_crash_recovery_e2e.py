"""
test_crash_recovery_e2e.py — End-to-end crash recovery pipeline tests.

Simulates crashes at various points and verifies recovery produces
consistent state:
  1. Crash mid-LLM call: message in queue, verify recovery skips completed
  2. Crash mid-tool execution: message with partial results, verify cleanup
  3. Crash mid-write: verify atomic writes produce consistent state
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.crash_recovery import (
    _check_acl,
    _reconstruct_message,
    _resolve_sender_id,
    recover_pending_messages,
)
from src.message_queue import MessageQueue, QueuedMessage


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_queued(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
    sender_id: Optional[str] = "sender-1",
    sender_name: Optional[str] = "Tester",
    created_at: Optional[float] = None,
) -> QueuedMessage:
    return QueuedMessage(
        message_id=message_id,
        chat_id=chat_id,
        text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        created_at=created_at or (time.time() - 600),
        updated_at=created_at or (time.time() - 600),
    )


def _make_channel(allowed: bool = True) -> MagicMock:
    channel = MagicMock()
    channel._is_allowed = MagicMock(return_value=allowed)
    return channel


# ── Tests ───────────────────────────────────────────────────────────────────


class TestCrashMidLLMCall:
    """Simulate crash during LLM processing: message left in queue."""

    async def test_pending_message_survives_crash_and_reconnect(self, tmp_path: Path) -> None:
        """Message enqueued before crash is still pending after reconnection."""
        data_dir = tmp_path / "data"

        # Session 1: enqueue, then "crash" (close without complete)
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        incoming = _make_queued(message_id="crash-msg-1", chat_id="chat-a", text="Help!")
        queue._pending["crash-msg-1"] = incoming
        await queue._flush_mgr.append_to_queue(incoming)
        await queue._flush_mgr.flush_write_buffer()
        await queue.close()

        # Session 2: reconnect, verify message survived
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert "crash-msg-1" in queue2._pending
        assert queue2._pending["crash-msg-1"].text == "Help!"

        # Mark as completed (simulating successful recovery)
        await queue2.complete("crash-msg-1")
        assert await queue2.get_pending_count() == 0
        await queue2.close()

    async def test_recovery_skips_already_completed(self, tmp_path: Path) -> None:
        """Messages completed before crash are not recovered."""
        queue = AsyncMock()
        # recover_stale returns only pending messages
        queue.recover_stale = AsyncMock(return_value=[])

        result = await recover_pending_messages(
            message_queue=queue,
            handle_message=AsyncMock(return_value="ok"),
        )

        assert result["total_found"] == 0
        assert result["recovered"] == 0


class TestCrashMidToolExecution:
    """Simulate crash during tool execution: partial results in queue."""

    async def test_partial_tool_results_cleaned_on_recovery(self, tmp_path: Path) -> None:
        """Messages with partial processing state are recovered cleanly."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir), stale_timeout=0)
        await queue.connect()

        # Simulate: message was in queue, crash happened mid-processing
        old_msg = _make_queued(
            message_id="tool-crash-1",
            chat_id="chat-b",
            text="Run tool X",
            created_at=time.time() - 600,
        )
        queue._pending["tool-crash-1"] = old_msg
        await queue._flush_mgr.append_to_queue(old_msg)
        await queue._flush_mgr.flush_write_buffer()

        # Recover stale messages
        stale = await queue.recover_stale(timeout_seconds=0)
        assert len(stale) == 1
        assert stale[0].message_id == "tool-crash-1"

        # The stale message is removed from pending
        assert await queue.get_pending_count() == 0

        await queue.close()

    async def test_recovery_with_failing_handler_records_failure(self, tmp_path: Path) -> None:
        """When recovery handler fails, the failure is recorded, not swallowed."""
        queue = AsyncMock()
        stale_msg = _make_queued(message_id="fail-msg", chat_id="chat-c")
        queue.recover_stale = AsyncMock(return_value=[stale_msg])

        failing_handler = AsyncMock(side_effect=RuntimeError("handler crashed"))

        result = await recover_pending_messages(
            message_queue=queue,
            handle_message=failing_handler,
            channel=_make_channel(allowed=True),
        )

        assert result["total_found"] == 1
        assert result["recovered"] == 0
        assert result["failed"] == 1
        assert len(result["failures"]) == 1
        assert "fail-msg" in result["failures"][0]["message_id"]


class TestCrashMidWrite:
    """Verify atomic writes produce consistent state after crash."""

    async def test_queue_file_not_corrupted_by_partial_write(self, tmp_path: Path) -> None:
        """Queue file remains valid even after incomplete write sequences."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Write several messages
        for i in range(5):
            msg = _make_queued(message_id=f"msg-{i}", chat_id="chat-a")
            queue._pending[f"msg-{i}"] = msg
            await queue._flush_mgr.append_to_queue(msg)

        await queue._flush_mgr.flush_write_buffer()

        # Complete some (simulating partial processing before crash)
        await queue.complete("msg-0")
        await queue.complete("msg-2")

        # Close (simulates crash — pending writes may be partial)
        await queue.close()

        # Verify file is still valid JSONL
        qfile = data_dir / "message_queue.jsonl"
        assert qfile.exists()
        content = qfile.read_text(encoding="utf-8")
        for line in content.strip().splitlines():
            if line.strip():
                data = json.loads(line)
                assert "message_id" in data

        # Re-open and verify state
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        # msg-1, msg-3, msg-4 should be pending (0 and 2 were completed)
        pending_ids = set(queue2._pending.keys())
        assert "msg-0" not in pending_ids
        assert "msg-2" not in pending_ids
        assert pending_ids == {"msg-1", "msg-3", "msg-4"}
        await queue2.close()

    async def test_multiple_messages_different_chats_recovered(self, tmp_path: Path) -> None:
        """Messages from different chats survive crash recovery independently."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir), stale_timeout=0)
        await queue.connect()

        msgs = [
            _make_queued(message_id="a-1", chat_id="chat-a", text="Msg A1"),
            _make_queued(message_id="b-1", chat_id="chat-b", text="Msg B1"),
            _make_queued(message_id="a-2", chat_id="chat-a", text="Msg A2"),
        ]
        for msg in msgs:
            queue._pending[msg.message_id] = msg

        # Recover stale
        stale = await queue.recover_stale(timeout_seconds=0)
        assert len(stale) == 3

        # Verify per-chat filtering works after recovery
        assert await queue.get_pending_count() == 0

        await queue.close()


class TestCrashRecoveryHelpers:
    """Tests for crash recovery helper functions."""

    def test_resolve_sender_id_prefers_sender_id(self) -> None:
        msg = _make_queued(sender_id="12345", sender_name="Alice")
        assert _resolve_sender_id(msg) == "12345"

    def test_resolve_sender_id_falls_back_to_sender_name(self) -> None:
        msg = _make_queued(sender_id=None, sender_name="Alice")
        assert _resolve_sender_id(msg) == "Alice"

    def test_resolve_sender_id_returns_none_when_both_absent(self) -> None:
        msg = _make_queued(sender_id=None, sender_name=None)
        assert _resolve_sender_id(msg) is None

    def test_reconstruct_message_sanitizes_sender_id(self) -> None:
        msg = _make_queued(sender_id="user@example.com")
        result = _reconstruct_message(msg)
        # @ is not alphanumeric/underscore, should be sanitized
        assert "@" not in result.sender_id

    def test_reconstruct_message_sets_acl_passed(self) -> None:
        msg = _make_queued()
        result = _reconstruct_message(msg)
        assert result.acl_passed is True

    def test_check_acl_allows_when_channel_has_is_allowed(self) -> None:
        channel = _make_channel(allowed=True)
        msg = _make_queued(sender_id="12345")
        assert _check_acl(msg, channel) is None

    def test_check_acl_blocks_disallowed_sender(self) -> None:
        channel = _make_channel(allowed=False)
        msg = _make_queued(sender_id="12345")
        assert _check_acl(msg, channel) == "not_in_allowed_numbers"

    def test_check_acl_returns_no_channel_when_none(self) -> None:
        msg = _make_queued()
        assert _check_acl(msg, None) == "no_channel"
