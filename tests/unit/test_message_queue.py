"""
Tests for src/message_queue.py — Persistent message queue for crash recovery.

Covers:
- QueuedMessage serialization/deserialization (to_dict, from_dict, from_incoming_message)
- MessageStatus enum
- MessageQueue enqueue / complete lifecycle
- Stale message recovery
- JSONL persistence and atomic writes
- Pending count tracking
- Per-chat filtering
- Context manager lifecycle (get_message_queue)
- Edge cases: empty queue, non-existent complete, concurrent access
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.message_queue import (
    MessageQueue,
    MessageStatus,
    QueueCorruptionResult,
    QueuedMessage,
    get_message_queue,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers: lightweight IncomingMessage stand-in
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeIncomingMessage:
    """Minimal stand-in for IncomingMessage used by from_incoming_message."""

    message_id: str
    chat_id: str
    text: str
    sender_id: str = "sender-1"
    sender_name: str = "Tester"
    channel_type: str = "whatsapp"
    # channel is not on the real IncomingMessage; from_incoming_message uses
    # getattr with a default, so including it lets us test the fallback path.
    channel: Optional[str] = None

    def __post_init__(self):
        pass


def make_incoming(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
    sender_id: str = "sender-1",
    sender_name: str = "Tester",
    channel: Optional[str] = "whatsapp",
) -> FakeIncomingMessage:
    """Factory for FakeIncomingMessage with sensible defaults."""
    return FakeIncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        channel=channel,
    )


def make_queued(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
    sender_id: Optional[str] = "sender-1",
    sender_name: Optional[str] = "Tester",
    channel: Optional[str] = "whatsapp",
    metadata: Optional[Dict[str, Any]] = None,
    status: MessageStatus = MessageStatus.PENDING,
    created_at: Optional[float] = None,
    updated_at: Optional[float] = None,
) -> QueuedMessage:
    """Factory for QueuedMessage with sensible defaults."""
    return QueuedMessage(
        message_id=message_id,
        chat_id=chat_id,
        text=text,
        sender_id=sender_id,
        sender_name=sender_name,
        channel=channel,
        metadata=metadata or {},
        status=status,
        created_at=created_at or time.time(),
        updated_at=updated_at or time.time(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. MessageStatus enum
# ═══════════════════════════════════════════════════════════════════════════════


class TestMessageStatus:
    """Tests for the MessageStatus enum."""

    def test_values(self):
        assert MessageStatus.PENDING == "pending"
        assert MessageStatus.COMPLETED == "completed"

    def test_is_str_enum(self):
        for member in MessageStatus:
            assert isinstance(member, str)
            assert isinstance(member, MessageStatus)

    def test_construct_from_string(self):
        assert MessageStatus("pending") is MessageStatus.PENDING
        assert MessageStatus("completed") is MessageStatus.COMPLETED

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            MessageStatus("unknown")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. QueuedMessage serialization / deserialization
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueuedMessageToDict:
    """Tests for QueuedMessage.to_dict()."""

    def test_round_trip(self):
        msg = make_queued()
        d = msg.to_dict()
        restored = QueuedMessage.from_dict(d)
        assert restored.message_id == msg.message_id
        assert restored.chat_id == msg.chat_id
        assert restored.text == msg.text
        assert restored.sender_id == msg.sender_id
        assert restored.sender_name == msg.sender_name
        assert restored.channel == msg.channel
        assert restored.metadata == msg.metadata
        assert restored.status == msg.status
        assert restored.created_at == msg.created_at
        assert restored.updated_at == msg.updated_at

    def test_status_serialized_as_string(self):
        msg = make_queued(status=MessageStatus.PENDING)
        d = msg.to_dict()
        assert d["status"] == "pending"
        assert isinstance(d["status"], str)

    def test_completed_status(self):
        msg = make_queued(status=MessageStatus.COMPLETED)
        d = msg.to_dict()
        assert d["status"] == "completed"
        restored = QueuedMessage.from_dict(d)
        assert restored.status == MessageStatus.COMPLETED

    def test_optional_fields_none(self):
        msg = QueuedMessage(message_id="x", chat_id="c", text="t")
        d = msg.to_dict()
        assert d["sender_name"] is None
        assert d["channel"] is None
        assert d["metadata"] == {}

    def test_metadata_preserved(self):
        meta = {"reply_to": "msg-0", "quoted_text": "original"}
        msg = make_queued(metadata=meta)
        d = msg.to_dict()
        assert d["metadata"] == meta

    def test_timestamps_are_floats(self):
        msg = make_queued()
        d = msg.to_dict()
        assert isinstance(d["created_at"], float)
        assert isinstance(d["updated_at"], float)

    def test_json_serializable(self):
        """to_dict() output must be JSON-serializable."""
        msg = make_queued(metadata={"key": "val", "num": 42})
        serialized = json.dumps(msg.to_dict())
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["message_id"] == "msg-1"


class TestQueuedMessageFromDict:
    """Tests for QueuedMessage.from_dict()."""

    def test_full_dict(self):
        now = time.time()
        data = {
            "message_id": "abc",
            "chat_id": "chat-2",
            "text": "hi",
            "sender_name": "Alice",
            "channel": "telegram",
            "metadata": {"foo": "bar"},
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
        msg = QueuedMessage.from_dict(data)
        assert msg.message_id == "abc"
        assert msg.chat_id == "chat-2"
        assert msg.text == "hi"
        assert msg.sender_name == "Alice"
        assert msg.channel == "telegram"
        assert msg.metadata == {"foo": "bar"}
        assert msg.status == MessageStatus.PENDING
        assert msg.created_at == now
        assert msg.updated_at == now

    def test_minimal_dict(self):
        """Only required fields provided; optional fields use defaults."""
        data = {"message_id": "m1", "chat_id": "c1", "text": "hello"}
        msg = QueuedMessage.from_dict(data)
        assert msg.sender_name is None
        assert msg.channel is None
        assert msg.metadata == {}
        assert msg.status == MessageStatus.PENDING
        # Timestamps are populated with time.time() defaults
        assert isinstance(msg.created_at, float)
        assert isinstance(msg.updated_at, float)

    def test_completed_status(self):
        data = {
            "message_id": "m1",
            "chat_id": "c1",
            "text": "hello",
            "status": "completed",
        }
        msg = QueuedMessage.from_dict(data)
        assert msg.status == MessageStatus.COMPLETED

    def test_missing_required_key_raises(self):
        with pytest.raises(KeyError):
            QueuedMessage.from_dict({"chat_id": "c1", "text": "hello"})

    def test_extra_keys_ignored(self):
        data = {
            "message_id": "m1",
            "chat_id": "c1",
            "text": "hello",
            "unknown_key": "value",
        }
        msg = QueuedMessage.from_dict(data)
        assert msg.message_id == "m1"

    def test_unicode_content(self):
        data = {
            "message_id": "m-unicode",
            "chat_id": "c-unicode",
            "text": "你好世界 🌍 Привет",
            "sender_name": "Ünïcödé",
        }
        msg = QueuedMessage.from_dict(data)
        assert msg.text == "你好世界 🌍 Привет"
        assert msg.sender_name == "Ünïcödé"


class TestQueuedMessageFromIncomingMessage:
    """Tests for QueuedMessage.from_incoming_message()."""

    def test_basic_conversion(self):
        incoming = make_incoming()
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.message_id == "msg-1"
        assert msg.chat_id == "chat-1"
        assert msg.text == "hello"
        assert msg.sender_id == "sender-1"
        assert msg.sender_name == "Tester"
        assert msg.channel == "whatsapp"
        assert msg.metadata == {}
        assert msg.status == MessageStatus.PENDING

    def test_status_is_pending_by_default(self):
        incoming = make_incoming()
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.status == MessageStatus.PENDING

    def test_timestamps_populated(self):
        incoming = make_incoming()
        msg = QueuedMessage.from_incoming_message(incoming)
        assert isinstance(msg.created_at, float)
        assert isinstance(msg.updated_at, float)
        assert msg.created_at > 0

    def test_empty_channel_type_preserved(self):
        """When IncomingMessage has empty channel_type, QueuedMessage.channel is empty."""
        incoming = FakeIncomingMessage(
            message_id="m1", chat_id="c1", text="hi", channel_type=""
        )
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.channel == ""

    def test_metadata_is_always_empty_dict(self):
        """IncomingMessage has no metadata attribute; from_incoming_message always uses {}."""
        incoming = make_incoming()
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.metadata == {}

    def test_with_real_incoming_message_fields(self):
        """Simulate real IncomingMessage that lacks channel/metadata attrs."""
        from types import SimpleNamespace

        # Real IncomingMessage doesn't have channel or metadata fields
        incoming = SimpleNamespace(
            message_id="real-1",
            chat_id="chat-99",
            text="world",
            sender_id="sender-99",
            sender_name="Bob",
            channel_type="whatsapp",
        )
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.sender_id == "sender-99"
        assert msg.channel == "whatsapp"
        assert msg.metadata == {}

    def test_channel_type_preserved_from_real_incoming_message(self):
        """Real IncomingMessage.channel_type is correctly captured in QueuedMessage.channel."""
        from src.channels.base import IncomingMessage

        incoming = IncomingMessage(
            message_id="real-channel-1",
            chat_id="chat-real",
            text="test message",
            sender_id="sender-real",
            sender_name="Alice",
            timestamp=1000000.0,
            channel_type="whatsapp",
        )
        queued = QueuedMessage.from_incoming_message(incoming)
        assert queued.channel == "whatsapp"
        assert queued.message_id == "real-channel-1"
        assert queued.chat_id == "chat-real"
        assert queued.sender_id == "sender-real"
        assert queued.sender_name == "Alice"
        assert queued.metadata == {}

    def test_default_empty_channel_type_preserved(self):
        """IncomingMessage with default empty channel_type results in empty QueuedMessage.channel."""
        from src.channels.base import IncomingMessage

        incoming = IncomingMessage(
            message_id="empty-ch-1",
            chat_id="chat-empty",
            text="test",
            sender_id="s1",
            sender_name="Bob",
            timestamp=1000000.0,
        )
        queued = QueuedMessage.from_incoming_message(incoming)
        assert queued.channel == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MessageQueue core operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestMessageQueueConnect:
    """Tests for MessageQueue.connect()."""

    async def test_creates_data_directory(self, tmp_path: Path):
        data_dir = tmp_path / "does_not_exist"
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert data_dir.exists()
        assert data_dir.is_dir()
        await queue.close()

    async def test_creates_nested_directories(self, tmp_path: Path):
        data_dir = tmp_path / "a" / "b" / "c"
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert data_dir.exists()
        await queue.close()

    async def test_loads_existing_pending(self, tmp_path: Path):
        # Pre-seed a queue file with a pending message
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        msg = make_queued(message_id="pre-existing", status=MessageStatus.PENDING)
        qfile.write_text(json.dumps(msg.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert "pre-existing" in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_skips_completed_on_load(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        completed_msg = make_queued(message_id="done-1", status=MessageStatus.COMPLETED)
        pending_msg = make_queued(message_id="pend-1", status=MessageStatus.PENDING)
        qfile.write_text(
            json.dumps(completed_msg.to_dict()) + "\n" + json.dumps(pending_msg.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert "done-1" not in queue._pending
        assert "pend-1" in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_handles_missing_queue_file(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert len(queue._pending) == 0
        await queue.close()

    async def test_handles_corrupt_jsonl_lines(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        valid_msg = make_queued(message_id="valid-1")
        qfile.write_text(
            "this is not json\n" + json.dumps(valid_msg.to_dict()) + "\n" + "\n",  # blank line
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()
        assert "valid-1" in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_sets_initialized_flag(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        assert queue._initialized is False
        await queue.connect()
        assert queue._initialized is True
        await queue.close()
        assert queue._initialized is False


class TestMessageQueueClose:
    """Tests for MessageQueue.close()."""

    async def test_persists_pending_on_close(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        incoming = make_incoming(message_id="persist-test")
        await queue.enqueue(incoming)
        await queue.close()

        # Verify file exists and contains the message
        qfile = data_dir / "message_queue.jsonl"
        assert qfile.exists()
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        data = json.loads(lines[-1])
        assert data["message_id"] == "persist-test"

    async def test_sets_initialized_false(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        assert queue._initialized is True
        await queue.close()
        assert queue._initialized is False


class TestMessageQueueEnqueue:
    """Tests for MessageQueue.enqueue()."""

    async def test_returns_queued_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        incoming = make_incoming(message_id="enq-1")
        result = await queue.enqueue(incoming)

        assert isinstance(result, QueuedMessage)
        assert result.message_id == "enq-1"
        assert result.status == MessageStatus.PENDING
        await queue.close()

    async def test_adds_to_pending_index(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="m1"))
        await queue.enqueue(make_incoming(message_id="m2"))

        assert await queue.get_pending_count() == 2
        assert "m1" in queue._pending
        assert "m2" in queue._pending
        await queue.close()

    async def test_appends_to_jsonl_file(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="a1"))
        await queue.enqueue(make_incoming(message_id="a2"))

        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["message_id"] == "a1"
        assert json.loads(lines[1])["message_id"] == "a2"
        await queue.close()

    async def test_duplicate_message_id_replaces(self, tmp_path: Path):
        """Enqueueing same message_id twice replaces the in-memory entry."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="dup", text="first"))
        await queue.enqueue(make_incoming(message_id="dup", text="second"))

        assert await queue.get_pending_count() == 1
        assert queue._pending["dup"].text == "second"
        await queue.close()


class TestMessageQueueTextTruncation:
    """Tests for text truncation during enqueue.

    Verifies that messages exceeding MAX_QUEUED_TEXT_LENGTH are truncated
    in the queue copy while preserving as much of the original content as
    possible.
    """

    async def test_short_message_not_truncated(self, tmp_path: Path):
        """Messages under the limit pass through unchanged."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        text = "short message"
        msg = await queue.enqueue(make_incoming(message_id="short-1", text=text))
        assert msg.text == text
        await queue.close()

    async def test_exactly_at_limit_not_truncated(self, tmp_path: Path):
        """Message exactly at MAX_QUEUED_TEXT_LENGTH is NOT truncated."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        text = "a" * MAX_QUEUED_TEXT_LENGTH
        msg = await queue.enqueue(make_incoming(message_id="exact-1", text=text))
        assert msg.text == text
        assert len(msg.text) == MAX_QUEUED_TEXT_LENGTH
        await queue.close()

    async def test_one_over_limit_is_truncated(self, tmp_path: Path):
        """Message one char over the limit is truncated."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        text = "a" * (MAX_QUEUED_TEXT_LENGTH + 1)
        msg = await queue.enqueue(make_incoming(message_id="over-1", text=text))
        assert len(msg.text) == MAX_QUEUED_TEXT_LENGTH
        assert msg.text.endswith("…[truncated]")
        await queue.close()

    async def test_truncation_preserves_start_of_message(self, tmp_path: Path):
        """Truncation keeps the beginning of the text intact."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        prefix = "IMPORTANT_HEADER:"
        text = prefix + "x" * (MAX_QUEUED_TEXT_LENGTH + 5000)
        msg = await queue.enqueue(make_incoming(message_id="prefix-1", text=text))
        assert msg.text.startswith(prefix)
        assert msg.text.endswith("…[truncated]")
        await queue.close()

    async def test_truncation_result_length_is_exact(self, tmp_path: Path):
        """Truncated text is exactly MAX_QUEUED_TEXT_LENGTH chars."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        for extra in [1, 100, 5000, 50_000]:
            text = "b" * (MAX_QUEUED_TEXT_LENGTH + extra)
            msg_id = f"len-{extra}"
            msg = await queue.enqueue(make_incoming(message_id=msg_id, text=text))
            assert len(msg.text) == MAX_QUEUED_TEXT_LENGTH, (
                f"Expected {MAX_QUEUED_TEXT_LENGTH} chars for extra={extra}, "
                f"got {len(msg.text)}"
            )
        await queue.close()

    async def test_truncation_survives_persistence_round_trip(self, tmp_path: Path):
        """Truncated text persists correctly through close/reconnect."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        text = "hello_" + "x" * (MAX_QUEUED_TEXT_LENGTH + 10000)
        await queue.enqueue(make_incoming(message_id="persist-trunc", text=text))
        truncated_text = queue._pending["persist-trunc"].text
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert "persist-trunc" in queue2._pending
        assert queue2._pending["persist-trunc"].text == truncated_text
        await queue2.close()

    async def test_unicode_truncation_preserves_valid_chars(self, tmp_path: Path):
        """Unicode text is truncated correctly without splitting multi-byte chars."""
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Use multi-byte Unicode characters to verify safe truncation
        text = "你好世界" * ((MAX_QUEUED_TEXT_LENGTH // 4) + 10)
        msg = await queue.enqueue(make_incoming(message_id="unicode-trunc", text=text))
        assert len(msg.text) == MAX_QUEUED_TEXT_LENGTH
        assert msg.text.endswith("…[truncated]")
        # Ensure no partial Unicode chars at the boundary
        msg.text.encode("utf-8")  # Should not raise
        await queue.close()

    async def test_empty_message_not_truncated(self, tmp_path: Path):
        """Empty string message is not affected."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        msg = await queue.enqueue(make_incoming(message_id="empty-1", text=""))
        assert msg.text == ""
        await queue.close()


class TestMessageQueueComplete:
    """Tests for MessageQueue.complete()."""

    async def test_completes_existing_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="c1"))

        result = await queue.complete("c1")
        assert result is True
        assert await queue.get_pending_count() == 0
        assert "c1" not in queue._pending
        await queue.close()

    async def test_returns_false_for_unknown_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        result = await queue.complete("nonexistent")
        assert result is False

    async def test_removes_from_jsonl_after_complete(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="x1"))

        qfile = data_dir / "message_queue.jsonl"
        lines_before = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines_before) >= 1

        await queue.complete("x1")

        lines_after = qfile.read_text(encoding="utf-8").strip().splitlines()
        # After complete, a completion marker is appended (append-only design)
        # The original enqueue line + completion marker should be present
        assert len(lines_after) == 2
        assert '"status": "completed"' in lines_after[1]
        await queue.close()

    async def test_double_complete_returns_false(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="d1"))

        assert await queue.complete("d1") is True
        assert await queue.complete("d1") is False
        await queue.close()

    async def test_complete_on_empty_queue(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        assert await queue.complete("nothing") is False
        await queue.close()


class TestMessageQueueEnqueueCompleteFlow:
    """Integration-style tests for the full enqueue → process → complete flow."""

    async def test_full_lifecycle(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Enqueue
        msg = await queue.enqueue(make_incoming(message_id="lc-1", chat_id="chat-A", text="hi"))
        assert msg.status == MessageStatus.PENDING
        assert await queue.get_pending_count() == 1

        # Complete
        assert await queue.complete("lc-1") is True
        assert await queue.get_pending_count() == 0

        await queue.close()

    async def test_multiple_messages_partial_complete(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="p1"))
        await queue.enqueue(make_incoming(message_id="p2"))
        await queue.enqueue(make_incoming(message_id="p3"))

        assert await queue.get_pending_count() == 3

        await queue.complete("p2")
        assert await queue.get_pending_count() == 2
        assert "p2" not in queue._pending
        assert "p1" in queue._pending
        assert "p3" in queue._pending

        await queue.close()

    async def test_recovery_after_crash_simulation(self, tmp_path: Path):
        """Simulate crash: connect, enqueue, close (no complete), then reconnect."""
        data_dir = tmp_path / "data"

        # First session: enqueue but don't complete (simulate crash)
        queue1 = MessageQueue(str(data_dir))
        await queue1.connect()
        await queue1.enqueue(make_incoming(message_id="crash-1"))
        await queue1.close()

        # Second session: reconnect and verify pending message survives
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        count = await queue2.get_pending_count()
        assert count == 1
        assert "crash-1" in queue2._pending

        # Now complete it
        assert await queue2.complete("crash-1") is True
        assert await queue2.get_pending_count() == 0
        await queue2.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Stale message recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestMessageQueueRecoverStale:
    """Tests for MessageQueue.recover_stale()."""

    async def test_no_stale_messages(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="fresh-1"))

        # All messages are fresh
        stale = await queue.recover_stale(timeout_seconds=300)
        assert stale == []
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_recovers_stale_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Create a message with an old updated_at timestamp
        old_time = time.time() - 600  # 10 minutes ago
        msg = make_queued(message_id="stale-1", updated_at=old_time)
        queue._pending["stale-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=300)
        assert len(stale) == 1
        assert stale[0].message_id == "stale-1"
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_uses_default_stale_timeout(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"), stale_timeout=60)
        await queue.connect()

        # Message 50 seconds old — not stale with 60s timeout
        msg = make_queued(message_id="fresh-1", updated_at=time.time() - 50)
        queue._pending["fresh-1"] = msg

        stale = await queue.recover_stale()  # no timeout_seconds arg
        assert len(stale) == 0

        # Now make it older than the default timeout
        queue._pending["fresh-1"].updated_at = time.time() - 120
        stale = await queue.recover_stale()
        assert len(stale) == 1
        await queue.close()

    async def test_custom_timeout_overrides_default(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"), stale_timeout=600)
        await queue.connect()

        # 30 seconds old — stale with custom 10s timeout, but not with 600s default
        msg = make_queued(message_id="custom-1", updated_at=time.time() - 30)
        queue._pending["custom-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=10)
        assert len(stale) == 1
        await queue.close()

    async def test_persists_after_recovery(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        old_msg = make_queued(message_id="stale-persist", updated_at=time.time() - 600)
        queue._pending["stale-persist"] = old_msg
        await queue._append_to_queue(old_msg)

        await queue.recover_stale(timeout_seconds=300)

        # Queue file should reflect that stale message was removed
        qfile = data_dir / "message_queue.jsonl"
        content = qfile.read_text(encoding="utf-8").strip()
        # File may be empty or have no lines with stale-persist
        if content:
            for line in content.splitlines():
                data = json.loads(line)
                assert data["message_id"] != "stale-persist"
        await queue.close()

    async def test_mixed_stale_and_fresh(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        stale_msg = make_queued(message_id="stale-1", updated_at=time.time() - 600)
        fresh_msg = make_queued(message_id="fresh-1", updated_at=time.time() - 10)
        queue._pending["stale-1"] = stale_msg
        queue._pending["fresh-1"] = fresh_msg

        stale = await queue.recover_stale(timeout_seconds=300)
        assert len(stale) == 1
        assert stale[0].message_id == "stale-1"
        assert await queue.get_pending_count() == 1
        assert "fresh-1" in queue._pending
        await queue.close()

    async def test_recover_on_empty_queue(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        stale = await queue.recover_stale(timeout_seconds=1)
        assert stale == []
        await queue.close()

    async def test_zero_timeout_recovers_all(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Message created slightly in the past so it's strictly older than cutoff
        msg = make_queued(message_id="recent-1", updated_at=time.time() - 1)
        queue._pending["recent-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=0)
        assert len(stale) == 1
        assert await queue.get_pending_count() == 0
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Pending count and per-chat filtering
# ═══════════════════════════════════════════════════════════════════════════════


class TestMessageQueuePendingCount:
    """Tests for MessageQueue.get_pending_count()."""

    async def test_empty_queue(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_after_enqueue(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="c1"))
        assert await queue.get_pending_count() == 1
        await queue.enqueue(make_incoming(message_id="c2"))
        assert await queue.get_pending_count() == 2
        await queue.close()

    async def test_after_complete(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="c1"))
        await queue.enqueue(make_incoming(message_id="c2"))
        await queue.complete("c1")
        assert await queue.get_pending_count() == 1
        await queue.close()


class TestMessageQueuePendingForChat:
    """Tests for MessageQueue.get_pending_for_chat()."""

    async def test_empty_queue(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        result = await queue.get_pending_for_chat("chat-A")
        assert result == []
        await queue.close()

    async def test_filters_by_chat_id(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="m1", chat_id="chat-A"))
        await queue.enqueue(make_incoming(message_id="m2", chat_id="chat-B"))
        await queue.enqueue(make_incoming(message_id="m3", chat_id="chat-A"))

        result = await queue.get_pending_for_chat("chat-A")
        ids = {m.message_id for m in result}
        assert ids == {"m1", "m3"}
        await queue.close()

    async def test_no_match_returns_empty(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="m1", chat_id="chat-A"))

        result = await queue.get_pending_for_chat("chat-Z")
        assert result == []
        await queue.close()

    async def test_completed_messages_excluded(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="m1", chat_id="chat-A"))
        await queue.enqueue(make_incoming(message_id="m2", chat_id="chat-A"))
        await queue.complete("m1")

        result = await queue.get_pending_for_chat("chat-A")
        ids = {m.message_id for m in result}
        assert ids == {"m2"}
        await queue.close()

    async def test_returns_queued_message_objects(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="m1", chat_id="chat-X"))

        result = await queue.get_pending_for_chat("chat-X")
        assert len(result) == 1
        assert isinstance(result[0], QueuedMessage)
        assert result[0].chat_id == "chat-X"
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Persistence & atomic writes
# ═══════════════════════════════════════════════════════════════════════════════


class TestMessageQueuePersistence:
    """Tests for JSONL persistence and atomic write behavior."""

    async def test_append_creates_file(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="a1"))

        qfile = data_dir / "message_queue.jsonl"
        assert qfile.exists()
        await queue.close()

    async def test_append_format_is_jsonl(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="a1"))
        await queue.enqueue(make_incoming(message_id="a2"))

        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        # Each line is valid JSON
        for line in lines:
            data = json.loads(line)
            assert "message_id" in data
        await queue.close()

    async def test_atomic_rewrite_on_complete(self, tmp_path: Path):
        """complete() appends completion marker (compaction happens at threshold)."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="r1"))
        await queue.enqueue(make_incoming(message_id="r2"))

        # Complete one — appends completion marker
        await queue.complete("r1")

        # File should contain 2 enqueue entries + 1 completion marker
        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["message_id"] == "r1"
        assert json.loads(lines[1])["message_id"] == "r2"
        assert '"status": "completed"' in lines[2]
        await queue.close()

    async def test_no_temp_file_left_after_persist(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="t1"))
        await queue.complete("t1")  # triggers _persist_pending

        tmp_file = data_dir / "message_queue.tmp"
        assert not tmp_file.exists()
        await queue.close()

    async def test_unicode_persistence(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        incoming = make_incoming(
            message_id="unicode-1",
            text="你好世界 🌍 Привет مرحبا",
            sender_name="Ünïcödé 名前",
        )
        await queue.enqueue(incoming)
        await queue.close()

        # Re-load and verify
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        msg = queue2._pending.get("unicode-1")
        assert msg is not None
        assert msg.text == "你好世界 🌍 Привет مرحبا"
        assert msg.sender_name == "Ünïcödé 名前"
        await queue2.close()

    async def test_persist_after_close_survives_reconnect(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="survive-1"))
        await queue.enqueue(make_incoming(message_id="survive-2"))
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 2
        assert "survive-1" in queue2._pending
        assert "survive-2" in queue2._pending
        await queue2.close()

    async def test_persist_writes_utf8(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="enc-1", text="emoji 🎉 test"))
        await queue.close()

        # Read raw bytes to verify encoding
        qfile = data_dir / "message_queue.jsonl"
        raw = qfile.read_bytes()
        # Should be valid UTF-8
        raw.decode("utf-8")
        assert "🎉".encode("utf-8") in raw


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Context manager lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetMessageQueue:
    """Tests for the get_message_queue async context manager."""

    async def test_connects_on_entry(self, tmp_path: Path):
        async with get_message_queue(str(tmp_path / "data")) as queue:
            assert queue._initialized is True

    async def test_closes_on_exit(self, tmp_path: Path):
        data_dir = str(tmp_path / "data")
        async with get_message_queue(data_dir) as queue:
            pass
        assert queue._initialized is False

    async def test_yields_connected_queue(self, tmp_path: Path):
        async with get_message_queue(str(tmp_path / "data")) as queue:
            assert isinstance(queue, MessageQueue)
            assert queue._initialized is True

    async def test_enqueue_inside_context(self, tmp_path: Path):
        async with get_message_queue(str(tmp_path / "data")) as queue:
            await queue.enqueue(make_incoming(message_id="ctx-1"))
            assert await queue.get_pending_count() == 1

    async def test_data_persists_after_context_exit(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        async with get_message_queue(str(data_dir)) as queue:
            await queue.enqueue(make_incoming(message_id="ctx-p1"))

        # Re-open and verify
        async with get_message_queue(str(data_dir)) as queue:
            assert await queue.get_pending_count() == 1
            assert "ctx-p1" in queue._pending

    async def test_custom_stale_timeout(self, tmp_path: Path):
        async with get_message_queue(str(tmp_path / "data"), stale_timeout=120) as queue:
            assert queue._stale_timeout == 120

    async def test_default_stale_timeout(self, tmp_path: Path):
        async with get_message_queue(str(tmp_path / "data")) as queue:
            assert queue._stale_timeout == MessageQueue.DEFAULT_STALE_TIMEOUT

    async def test_closes_on_exception(self, tmp_path: Path):
        data_dir = str(tmp_path / "data")
        queue_ref = None
        with pytest.raises(ValueError, match="test error"):
            async with get_message_queue(data_dir) as queue:
                queue_ref = queue
                raise ValueError("test error")
        # Queue should still be closed
        assert queue_ref._initialized is False


class TestMessageQueueAsyncContextManager:
    """Tests for MessageQueue used directly as async context manager."""

    async def test_enter_connects(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        async with queue:
            assert queue._initialized is True

    async def test_exit_closes(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        async with queue:
            pass
        assert queue._initialized is False

    async def test_exit_closes_on_exception(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        with pytest.raises(RuntimeError):
            async with queue:
                raise RuntimeError("boom")
        assert queue._initialized is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Edge cases & concurrency
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge-case tests for MessageQueue."""

    async def test_enqueue_after_close_reopens_implicitly(self, tmp_path: Path):
        """The queue doesn't block operations after close, but they work
        because _pending is in memory and file ops still function."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.close()
        # Enqueue still works — it's async but doesn't check _initialized
        msg = await queue.enqueue(make_incoming(message_id="post-close"))
        assert msg.message_id == "post-close"

    async def test_large_message_text(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        big_text = "x" * 100_000
        await queue.enqueue(make_incoming(message_id="big-1", text=big_text))
        # Text is truncated to MAX_QUEUED_TEXT_LENGTH during enqueue
        from src.constants import MAX_QUEUED_TEXT_LENGTH

        queued_text = queue._pending["big-1"].text
        assert len(queued_text) == MAX_QUEUED_TEXT_LENGTH
        assert queued_text.endswith("…[truncated]")
        await queue.close()

        # Verify truncated text survives round-trip
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert queue2._pending["big-1"].text == queued_text
        await queue2.close()

    async def test_special_characters_in_metadata(self, tmp_path: Path):
        """Metadata with special chars survives to_dict/from_dict round-trip."""
        meta = {
            "path": "C:\\Users\\test\\file.txt",
            "regex": ".*\\d+",
            "nested": {"key": 'val with "quotes"'},
        }
        msg = make_queued(message_id="special-1", metadata=meta)
        d = msg.to_dict()
        assert d["metadata"] == meta

        restored = QueuedMessage.from_dict(d)
        assert restored.metadata == meta

    async def test_empty_text_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="empty-text", text=""))
        assert queue._pending["empty-text"].text == ""
        await queue.close()

    async def test_empty_metadata(self, tmp_path: Path):
        """QueuedMessage with empty metadata persists correctly."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        msg = await queue.enqueue(make_incoming(message_id="no-meta"))
        assert msg.metadata == {}
        await queue.close()

    async def test_complete_preserves_other_messages_jsonl(self, tmp_path: Path):
        """After completing one message, other messages' data stays intact."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(
            make_incoming(
                message_id="keep-1",
                chat_id="cA",
                text="important data",
            )
        )
        await queue.enqueue(make_incoming(message_id="remove-1"))
        await queue.complete("remove-1")

        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        # 2 enqueue entries + 1 completion marker = 3 lines
        assert len(lines) == 3
        data = json.loads(lines[0])
        assert data["message_id"] == "keep-1"
        assert data["text"] == "important data"
        assert data["metadata"] == {}
        await queue.close()


class TestEagerEviction:
    """Tests for eager eviction of completed entries during _load_pending()."""

    async def test_evicts_completed_entries_on_load(self, tmp_path: Path):
        """Completed entries are removed from file after loading."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        pending_msg = make_queued(message_id="pend-1", status=MessageStatus.PENDING)
        completed_msg = make_queued(message_id="done-1", status=MessageStatus.COMPLETED)
        qfile.write_text(
            json.dumps(pending_msg.to_dict()) + "\n"
            + json.dumps(completed_msg.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # File should now only contain the pending entry
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["message_id"] == "pend-1"
        assert "pend-1" in queue._pending
        await queue.close()

    async def test_evicts_all_completed_leaves_empty_file(self, tmp_path: Path):
        """When all entries are completed, file is rewritten as empty."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        completed_a = make_queued(message_id="done-a", status=MessageStatus.COMPLETED)
        completed_b = make_queued(message_id="done-b", status=MessageStatus.COMPLETED)
        qfile.write_text(
            json.dumps(completed_a.to_dict()) + "\n"
            + json.dumps(completed_b.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        content = qfile.read_text(encoding="utf-8").strip()
        assert content == ""
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_no_eviction_when_all_pending(self, tmp_path: Path):
        """No rewrite occurs when there are no completed entries."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        msg1 = make_queued(message_id="p1", status=MessageStatus.PENDING)
        msg2 = make_queued(message_id="p2", status=MessageStatus.PENDING)
        original_content = (
            json.dumps(msg1.to_dict()) + "\n" + json.dumps(msg2.to_dict()) + "\n"
        )
        qfile.write_text(original_content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # File content unchanged (no eviction needed)
        assert qfile.read_text(encoding="utf-8") == original_content
        assert await queue.get_pending_count() == 2
        await queue.close()

    async def test_evicts_many_completed_entries(self, tmp_path: Path):
        """Large number of completed entries are evicted in one pass."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        lines: list[str] = []
        for i in range(50):
            msg = make_queued(message_id=f"done-{i}", status=MessageStatus.COMPLETED)
            lines.append(json.dumps(msg.to_dict()))
        # Add 3 pending
        for i in range(3):
            msg = make_queued(message_id=f"pend-{i}", status=MessageStatus.PENDING)
            lines.append(json.dumps(msg.to_dict()))
        qfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        remaining = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 3
        remaining_ids = {json.loads(line)["message_id"] for line in remaining}
        assert remaining_ids == {"pend-0", "pend-1", "pend-2"}
        await queue.close()

    async def test_eviction_preserves_pending_after_reconnect(self, tmp_path: Path):
        """Evicted file still loads correctly on subsequent connect."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        pending_msg = make_queued(message_id="survive-1", status=MessageStatus.PENDING)
        completed_msg = make_queued(message_id="gone-1", status=MessageStatus.COMPLETED)
        qfile.write_text(
            json.dumps(completed_msg.to_dict()) + "\n"
            + json.dumps(pending_msg.to_dict()) + "\n",
            encoding="utf-8",
        )

        # First connect triggers eviction
        queue = MessageQueue(str(data_dir))
        await queue.connect()
        await queue.close()

        # Second connect loads evicted file
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert "survive-1" in queue2._pending
        assert "gone-1" not in queue2._pending
        assert await queue2.get_pending_count() == 1
        await queue2.close()

    async def test_eviction_with_completion_markers(self, tmp_path: Path):
        """Completion markers (from append-only design) are also evicted."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        pending_msg = make_queued(message_id="p1", status=MessageStatus.PENDING)
        completion_marker = json.dumps({
            "message_id": "old-1",
            "status": "completed",
            "completed_at": time.time(),
        })
        qfile.write_text(
            json.dumps(pending_msg.to_dict()) + "\n" + completion_marker + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["message_id"] == "p1"
        await queue.close()


class TestConcurrentAccess:
    """Tests for concurrent/parallel access safety."""

    async def test_concurrent_enqueues(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Enqueue 50 messages concurrently
        tasks = [queue.enqueue(make_incoming(message_id=f"concurrent-{i}")) for i in range(50)]
        await asyncio.gather(*tasks)

        assert await queue.get_pending_count() == 50
        await queue.close()

    async def test_concurrent_enqueues_and_completes(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Enqueue 20 messages
        for i in range(20):
            await queue.enqueue(make_incoming(message_id=f"mix-{i}"))

        # Complete half concurrently
        complete_tasks = [queue.complete(f"mix-{i}") for i in range(10)]
        results = await asyncio.gather(*complete_tasks)
        assert all(r is True for r in results)
        assert await queue.get_pending_count() == 10
        await queue.close()

    async def test_concurrent_complete_same_message(self, tmp_path: Path):
        """Only one concurrent complete should succeed."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="race-1"))

        results = await asyncio.gather(
            queue.complete("race-1"),
            queue.complete("race-1"),
            queue.complete("race-1"),
        )
        # Exactly one should succeed; the rest return False
        assert results.count(True) == 1
        assert results.count(False) == 2
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Crash recovery edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrashRecoveryCorruptedJsonl:
    """Tests for recovery with a corrupted JSONL file.

    Verifies that _load_pending() handles various corruption scenarios
    gracefully, loading what it can and skipping what it cannot.
    """

    async def test_truncated_line_at_end(self, tmp_path: Path):
        """File ends with a truncated JSON line (partial write during crash)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="ok-1")
        truncated = '{"message_id": "truncated-1", "chat_id": "chat-1", "text'
        qfile.write_text(
            json.dumps(valid_msg.to_dict()) + "\n" + truncated + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "ok-1" in queue._pending
        assert "truncated-1" not in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_binary_garbage_interleaved(self, tmp_path: Path):
        """Binary/null bytes interleaved with valid JSONL lines."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="survives-1")
        lines = (
            json.dumps(valid_msg.to_dict()) + "\n"
            + "\x00\x01\x02\x03\n"
            + "NOT JSON AT ALL\n"
        )
        qfile.write_bytes(lines.encode("utf-8", errors="replace"))

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "survives-1" in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_missing_required_keys(self, tmp_path: Path):
        """Lines with valid JSON but missing required QueuedMessage keys."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="valid-1")
        missing_text = json.dumps({"message_id": "no-text", "chat_id": "c1"})
        missing_id = json.dumps({"chat_id": "c1", "text": "no id"})
        qfile.write_text(
            json.dumps(valid_msg.to_dict()) + "\n"
            + missing_text + "\n"
            + missing_id + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "valid-1" in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_all_lines_corrupt_loads_empty(self, tmp_path: Path):
        """When every line is corrupted, queue starts empty without error."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        qfile.write_text("garbage\n{broken json\n\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 0
        assert queue._pending == {}
        await queue.close()

    async def test_corrupt_file_with_valid_pending_interleaved(self, tmp_path: Path):
        """Mix of corrupt lines and valid pending entries in one file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        msg_a = make_queued(message_id="inter-a")
        msg_b = make_queued(message_id="inter-b")
        qfile.write_text(
            "CORRUPT LINE\n"
            + json.dumps(msg_a.to_dict()) + "\n"
            + "\n"
            + json.dumps(msg_b.to_dict()) + "\n"
            + "{'single': 'quoted'}\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 2
        assert "inter-a" in queue._pending
        assert "inter-b" in queue._pending
        await queue.close()

    async def test_duplicate_pending_entries_keeps_last(self, tmp_path: Path):
        """Same message_id appears twice with different text; last wins."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        first = make_queued(message_id="dup-1", text="first version")
        second = make_queued(message_id="dup-1", text="second version")
        qfile.write_text(
            json.dumps(first.to_dict()) + "\n"
            + json.dumps(second.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert queue._pending["dup-1"].text == "second version"
        await queue.close()

    async def test_very_large_single_line(self, tmp_path: Path):
        """A single massive JSON line (10MB) doesn't crash the loader."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        big_text = "x" * 10_000_000
        msg = make_queued(message_id="huge-1", text=big_text)
        qfile.write_text(json.dumps(msg.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "huge-1" in queue._pending
        assert queue._pending["huge-1"].text == big_text
        await queue.close()


class TestCrashRecoveryConcurrentCompaction:
    """Tests for compaction during concurrent enqueue/complete.

    Verifies that the append-only + threshold-compaction design remains
    consistent when enqueue and complete happen concurrently.
    """

    async def test_compaction_during_concurrent_completes(self, tmp_path: Path):
        """Trigger compaction threshold while completing messages concurrently."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 5  # lower threshold for test
        await queue.connect()

        # Enqueue 10 messages
        for i in range(10):
            await queue.enqueue(make_incoming(message_id=f"comp-{i}"))

        assert await queue.get_pending_count() == 10

        # Complete all concurrently — should trigger 2 compactions (at 5th and 10th)
        results = await asyncio.gather(
            *[queue.complete(f"comp-{i}") for i in range(10)]
        )
        assert all(r is True for r in results)
        assert await queue.get_pending_count() == 0
        assert queue._completed_since_compact == 0

        # Verify file is clean (no stale pending entries)
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 0
        await queue2.close()

    async def test_concurrent_enqueue_and_complete(self, tmp_path: Path):
        """Enqueue and complete messages concurrently from multiple coroutines."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Phase 1: enqueue 20 messages
        for i in range(20):
            await queue.enqueue(make_incoming(message_id=f"eac-{i}"))

        # Phase 2: concurrently enqueue 10 more while completing first 10
        enq_tasks = [
            queue.enqueue(make_incoming(message_id=f"eac-new-{i}"))
            for i in range(10)
        ]
        comp_tasks = [queue.complete(f"eac-{i}") for i in range(10)]

        await asyncio.gather(*enq_tasks, *comp_tasks)

        # 10 original + 10 new = 20 pending
        assert await queue.get_pending_count() == 20
        await queue.close()

    async def test_compaction_preserves_uncompleted_messages(self, tmp_path: Path):
        """After compaction, uncompleted messages survive intact on disk."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 3
        await queue.connect()

        for i in range(6):
            await queue.enqueue(
                make_incoming(
                    message_id=f"keep-{i}",
                    chat_id=f"chat-{i % 2}",
                    text=f"message {i}",
                )
            )

        # Complete 3 to trigger compaction
        for i in range(3):
            await queue.complete(f"keep-{i}")

        # Remaining 3 should survive close + reconnect
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 3
        for i in range(3, 6):
            msg = queue2._pending.get(f"keep-{i}")
            assert msg is not None
            assert msg.text == f"message {i}"
            assert msg.metadata == {}
        await queue2.close()

    async def test_compaction_during_concurrent_stale_recovery(self, tmp_path: Path):
        """Stale recovery and concurrent completes don't corrupt the queue."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 2
        await queue.connect()

        # Add stale messages (old timestamps)
        old_time = time.time() - 600
        for i in range(3):
            msg = make_queued(message_id=f"stale-{i}", updated_at=old_time)
            queue._pending[f"stale-{i}"] = msg

        # Add fresh messages
        for i in range(3):
            await queue.enqueue(make_incoming(message_id=f"fresh-{i}"))

        # Concurrently recover stale and complete fresh messages
        gather_results = await asyncio.gather(
            queue.recover_stale(timeout_seconds=300),
            *[queue.complete(f"fresh-{i}") for i in range(3)],
        )

        stale_results = gather_results[0]
        complete_results = gather_results[1:]

        assert len(stale_results) == 3
        assert all(r is True for r in complete_results)
        assert await queue.get_pending_count() == 0
        await queue.close()


class TestCrashRecoveryStaleBoundary:
    """Tests for stale timeout with messages exactly at the boundary.

    Verifies that the strict < comparison in recover_stale() correctly
    classifies messages at the exact cutoff edge.
    """

    async def test_message_exactly_at_timeout_is_stale(self, tmp_path: Path):
        """Message with updated_at exactly at cutoff is stale (< not <=)."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        timeout = 300
        # Set updated_at exactly at the cutoff point
        exact_cutoff_time = time.time() - timeout
        msg = make_queued(message_id="exact-1", updated_at=exact_cutoff_time)
        queue._pending["exact-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=timeout)
        # The comparison is updated_at < cutoff, so exact cutoff is NOT stale
        # But floating point timing means we need to verify the actual behavior
        # In practice: time.time() - timeout will be < the cutoff computed
        # at the moment of the check, due to elapsed time between set and check
        assert len(stale) == 1
        await queue.close()

    async def test_message_just_before_timeout_is_not_stale(self, tmp_path: Path):
        """Message slightly newer than cutoff is NOT stale."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        timeout = 300
        # 1ms newer than the cutoff
        just_before_cutoff = time.time() - timeout + 0.001
        msg = make_queued(message_id="almost-1", updated_at=just_before_cutoff)
        queue._pending["almost-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=timeout)
        assert len(stale) == 0
        assert "almost-1" in queue._pending
        await queue.close()

    async def test_message_just_after_timeout_is_stale(self, tmp_path: Path):
        """Message slightly older than cutoff IS stale."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        timeout = 300
        # 1ms older than the cutoff
        just_after_cutoff = time.time() - timeout - 0.001
        msg = make_queued(message_id="just-over-1", updated_at=just_after_cutoff)
        queue._pending["just-over-1"] = msg

        stale = await queue.recover_stale(timeout_seconds=timeout)
        assert len(stale) == 1
        assert stale[0].message_id == "just-over-1"
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_boundary_messages_mixed_with_clear_cases(self, tmp_path: Path):
        """Mix of clearly stale, boundary, and clearly fresh messages."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        timeout = 300

        # Clearly stale (10 minutes over)
        queue._pending["old-stale"] = make_queued(
            message_id="old-stale", updated_at=time.time() - timeout - 600
        )

        # Exactly at boundary (will be stale due to execution time)
        queue._pending["boundary"] = make_queued(
            message_id="boundary", updated_at=time.time() - timeout
        )

        # Clearly fresh (just enqueued)
        queue._pending["fresh"] = make_queued(
            message_id="fresh", updated_at=time.time() - 1
        )

        stale = await queue.recover_stale(timeout_seconds=timeout)
        stale_ids = {m.message_id for m in stale}

        assert "old-stale" in stale_ids
        assert "boundary" in stale_ids
        assert "fresh" not in stale_ids
        assert "fresh" in queue._pending
        await queue.close()

    async def test_zero_timeout_with_boundary_timestamps(self, tmp_path: Path):
        """Zero timeout classifies even recent messages as stale."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Messages from 1 second ago — stale with 0 timeout
        queue._pending["one-sec-ago"] = make_queued(
            message_id="one-sec-ago", updated_at=time.time() - 1
        )

        # Message from 0.001 seconds ago — also stale
        queue._pending["ms-ago"] = make_queued(
            message_id="ms-ago", updated_at=time.time() - 0.001
        )

        stale = await queue.recover_stale(timeout_seconds=0)
        assert len(stale) == 2
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_huge_timeout_recovers_nothing(self, tmp_path: Path):
        """Very large timeout means even old messages are not stale."""
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        queue._pending["recent-1"] = make_queued(
            message_id="recent-1", updated_at=time.time() - 1
        )

        stale = await queue.recover_stale(timeout_seconds=999_999_999)
        assert len(stale) == 0
        assert "recent-1" in queue._pending
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Concurrent enqueue/complete race conditions
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentEnqueueCompleteRaceConditions:
    """Tests for concurrent enqueue/complete race conditions.

    The queue uses asyncio.Lock for coroutine-safe access. These tests
    verify that the lock correctly serializes operations when multiple
    coroutines contend for the same chat or overlap enqueue/complete cycles.

    Covers the most likely production failure modes:
    (a) two coroutines enqueueing for the same chat simultaneously
    (b) completing a message while another coroutine is loading pending
    (c) compaction triggered during concurrent completions
    """

    async def test_same_chat_concurrent_enqueues_no_lost_messages(self, tmp_path: Path):
        """Two coroutines enqueueing messages for the same chat simultaneously.

        All messages must appear in the pending index and the JSONL file
        without corruption or data loss.
        """
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # 30 messages all for the same chat, enqueued concurrently
        tasks = [
            queue.enqueue(
                make_incoming(message_id=f"same-chat-{i}", chat_id="chat-X", text=f"msg-{i}")
            )
            for i in range(30)
        ]
        await asyncio.gather(*tasks)

        # Every message must be present in pending
        assert await queue.get_pending_count() == 30
        for i in range(30):
            assert f"same-chat-{i}" in queue._pending
            assert queue._pending[f"same-chat-{i}"].chat_id == "chat-X"
            assert queue._pending[f"same-chat-{i}"].text == f"msg-{i}"

        # Per-chat filter must return all 30
        chat_pending = await queue.get_pending_for_chat("chat-X")
        assert len(chat_pending) == 30

        # JSONL file must have 30 lines, all valid
        qfile = tmp_path / "data" / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 30
        for line in lines:
            data = json.loads(line)
            assert data["chat_id"] == "chat-X"

        await queue.close()

    async def test_same_chat_interleaved_enqueue_and_complete(self, tmp_path: Path):
        """Enqueue and complete for the same chat overlap via gather.

        Simulates a fast-paced conversation where messages arrive while
        earlier messages are still being completed.
        """
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Pre-enqueue 20 messages for the same chat
        for i in range(20):
            await queue.enqueue(
                make_incoming(message_id=f"inter-{i}", chat_id="chat-Y")
            )

        # Concurrently: complete first 10, enqueue 5 new ones
        complete_tasks = [queue.complete(f"inter-{i}") for i in range(10)]
        enqueue_tasks = [
            queue.enqueue(
                make_incoming(message_id=f"inter-new-{i}", chat_id="chat-Y")
            )
            for i in range(5)
        ]

        results = await asyncio.gather(*complete_tasks, *enqueue_tasks)

        # All completes should succeed
        assert all(r is True for r in results[:10])

        # Remaining: 10 original + 5 new = 15
        assert await queue.get_pending_count() == 15
        chat_pending = await queue.get_pending_for_chat("chat-Y")
        assert len(chat_pending) == 15

        await queue.close()

    async def test_complete_during_load_pending(self, tmp_path: Path):
        """Completing a message while _load_pending is running.

        Simulates a race where a second queue instance connects (triggers
        _load_pending) while the first instance is completing messages.
        Since asyncio.Lock serializes access, the complete must wait for
        _load_pending to finish and then correctly reflect state.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        # Pre-seed the file with pending messages
        msgs = [make_queued(message_id=f"load-{i}") for i in range(10)]
        qfile.write_text(
            "".join(json.dumps(m.to_dict()) + "\n" for m in msgs),
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Verify all loaded
        assert await queue.get_pending_count() == 10

        # Simulate concurrent: start loading a fresh queue while completing
        # messages on the existing one. The lock ensures serialization.
        queue2 = MessageQueue(str(data_dir))

        # Start connect (which calls _load_pending) and completes concurrently
        connect_task = asyncio.create_task(queue2.connect())
        # Give connect a chance to start (it acquires the lock for _load_pending)
        await asyncio.sleep(0)

        # Complete messages on the first queue — will serialize after connect's lock
        complete_results = await asyncio.gather(
            *[queue.complete(f"load-{i}") for i in range(5)]
        )

        # Wait for connect to finish
        await connect_task

        # Queue2 should have loaded pending messages (what was on disk at connect time)
        # The completes on queue1 happen independently since they are different instances
        # but share the same file. The key assertion: no crash, no corruption.
        assert queue2._initialized is True

        # Clean up
        await queue.close()
        await queue2.close()

    async def test_complete_while_pending_load_holds_lock(self, tmp_path: Path):
        """A complete call must wait for an in-progress _load_pending to finish.

        Verifies that complete() doesn't bypass the lock and corrupt state
        when _load_pending is running.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        # Pre-seed with many pending messages to make _load_pending non-trivial
        lines = []
        for i in range(100):
            msg = make_queued(message_id=f"lock-{i}")
            lines.append(json.dumps(msg.to_dict()))
        qfile.write_text("\n".join(lines) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))

        # Manually trigger _load_pending and race a complete against it
        # Since we haven't called connect() yet, we call _load_pending directly
        load_task = asyncio.create_task(queue._load_pending())

        # Immediately try to complete a message (should wait for load)
        # Since queue is not fully initialized, we manually add a pending entry
        # to simulate the race condition
        queue._pending["manual-1"] = make_queued(message_id="manual-1")

        complete_task = asyncio.create_task(queue.complete("manual-1"))

        # Both should complete without error
        await load_task
        result = await complete_task

        assert result is True
        assert "manual-1" not in queue._pending
        # All 100 from file should be loaded
        assert len(queue._pending) == 100

    async def test_compaction_during_concurrent_completes_preserves_pending(self, tmp_path: Path):
        """Compaction triggered by concurrent completions preserves remaining pending messages.

        When multiple completes fire simultaneously and one triggers compaction
        (_persist_pending), the other completes must not lose track of still-pending
        messages. This is the most common production failure mode.
        """
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 5
        await queue.connect()

        # Enqueue 15 messages for different chats
        for i in range(15):
            await queue.enqueue(
                make_incoming(
                    message_id=f"race-{i}",
                    chat_id=f"chat-{i % 3}",
                    text=f"text-{i}",
                )
            )

        assert await queue.get_pending_count() == 15

        # Complete 10 concurrently (2 compactions at thresholds 5 and 10)
        # while 5 messages remain pending
        results = await asyncio.gather(
            *[queue.complete(f"race-{i}") for i in range(10)]
        )

        assert all(r is True for r in results)
        assert await queue.get_pending_count() == 5

        # Remaining 5 must be intact
        for i in range(10, 15):
            msg = queue._pending.get(f"race-{i}")
            assert msg is not None
            assert msg.text == f"text-{i}"
            assert msg.chat_id == f"chat-{i % 3}"

        # Verify on-disk consistency via close + reconnect
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 5
        for i in range(10, 15):
            assert f"race-{i}" in queue2._pending
            assert queue2._pending[f"race-{i}"].text == f"text-{i}"
        await queue2.close()

    async def test_compaction_with_overlapping_completes_different_chats(self, tmp_path: Path):
        """Compaction while completing messages from different chats concurrently.

        Ensures per-chat data integrity is maintained when compaction rewrites
        the file while completions from different chats race.
        """
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 3
        await queue.connect()

        # 6 messages: 2 per chat across 3 chats
        for chat in ("A", "B", "C"):
            for j in range(2):
                await queue.enqueue(
                    make_incoming(
                        message_id=f"multi-{chat}-{j}",
                        chat_id=f"chat-{chat}",
                        text=f"msg-{chat}-{j}",
                    )
                )

        # Complete all messages from chat A and B concurrently (4 completes → 1 compaction)
        complete_ids = [f"multi-{c}-{j}" for c in ("A", "B") for j in range(2)]
        results = await asyncio.gather(*[queue.complete(mid) for mid in complete_ids])

        assert all(r is True for r in results)
        assert await queue.get_pending_count() == 2  # chat C's 2 messages

        # Chat C messages must be intact
        chat_c = await queue.get_pending_for_chat("chat-C")
        assert len(chat_c) == 2
        chat_c_ids = {m.message_id for m in chat_c}
        assert chat_c_ids == {"multi-C-0", "multi-C-1"}

        # Persist + reconnect to verify disk consistency
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 2
        chat_c_reloaded = await queue2.get_pending_for_chat("chat-C")
        assert len(chat_c_reloaded) == 2
        await queue2.close()

    async def test_concurrent_completes_with_compaction_file_consistency(self, tmp_path: Path):
        """Rapid concurrent completes cause multiple compactions; file stays consistent.

        Stress test: many completes fire at once, multiple compaction thresholds
        are crossed, and the file must remain a valid JSONL document containing
        only pending entries after compaction.
        """
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 4
        await queue.connect()

        # Enqueue 20 messages
        for i in range(20):
            await queue.enqueue(make_incoming(message_id=f"stress-{i}"))

        # Complete all 20 concurrently — triggers 5 compactions
        results = await asyncio.gather(
            *[queue.complete(f"stress-{i}") for i in range(20)]
        )
        assert all(r is True for r in results)
        assert await queue.get_pending_count() == 0

        # File must be empty (no stale entries) after compaction of all messages
        qfile = data_dir / "message_queue.jsonl"
        content = qfile.read_text(encoding="utf-8").strip()
        # After all are completed and final compaction runs, file should be empty
        # (compaction writes only pending messages, and there are none)
        assert content == ""

        await queue.close()

    async def test_enqueue_during_compaction_does_not_lose_message(self, tmp_path: Path):
        """An enqueue arriving during an active compaction must not be lost.

        Compaction (_persist_pending) snapshots pending messages, writes to disk,
        but a concurrent enqueue could add a new message. The lock ensures the
        enqueue waits for compaction to finish, so the new message must appear
        in both the pending index and the file.
        """
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 2
        await queue.connect()

        # Enqueue 3 messages
        for i in range(3):
            await queue.enqueue(make_incoming(message_id=f"cpenq-{i}"))

        # Complete 2 to trigger compaction, while simultaneously enqueueing a new one
        compaction_tasks = [
            queue.complete(f"cpenq-{i}") for i in range(2)
        ]
        enqueue_task = queue.enqueue(
            make_incoming(message_id="cpenq-new", chat_id="chat-Z", text="late arrival")
        )

        await asyncio.gather(*compaction_tasks, enqueue_task)

        # cpenq-2 (still pending) + cpenq-new = 2 pending
        assert await queue.get_pending_count() == 2
        assert "cpenq-2" in queue._pending
        assert "cpenq-new" in queue._pending
        assert queue._pending["cpenq-new"].text == "late arrival"

        # Verify persistence
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert await queue2.get_pending_count() == 2
        assert "cpenq-2" in queue2._pending
        assert "cpenq-new" in queue2._pending
        assert queue2._pending["cpenq-new"].chat_id == "chat-Z"
        await queue2.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Property-based tests for QueuedMessage serialization round-trip
# ═══════════════════════════════════════════════════════════════════════════════


# Hypothesis strategy for generating valid MessageStatus values
_status_strategy = st.sampled_from(list(MessageStatus))

# Strategy for arbitrary metadata dicts (JSON-compatible values only)
_metadata_strategy = st.dictionaries(
    keys=st.text(min_size=0, max_size=20),
    values=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(min_size=0, max_size=50),
    ),
    max_size=5,
)

# Core strategy for generating QueuedMessage instances
_queued_message_strategy = st.builds(
    QueuedMessage,
    message_id=st.text(min_size=1, max_size=200),
    chat_id=st.text(min_size=1, max_size=200),
    text=st.text(min_size=0, max_size=5000),
    sender_id=st.one_of(st.none(), st.text(min_size=0, max_size=200)),
    sender_name=st.one_of(st.none(), st.text(min_size=0, max_size=200)),
    channel=st.one_of(st.none(), st.text(min_size=0, max_size=50)),
    metadata=_metadata_strategy,
    status=_status_strategy,
    created_at=st.floats(
        min_value=0, max_value=2**53, allow_nan=False, allow_infinity=False
    ),
    updated_at=st.floats(
        min_value=0, max_value=2**53, allow_nan=False, allow_infinity=False
    ),
)


class TestQueuedMessagePropertyRoundTrip:
    """Property-based tests verifying to_dict/from_dict is an identity transform.

    Uses Hypothesis to generate arbitrary QueuedMessage instances and verifies
    that serializing to a dict and deserializing back preserves all fields.
    """

    @given(msg=_queued_message_strategy)
    @settings(max_examples=200)
    def test_round_trip_preserves_all_fields(self, msg: QueuedMessage):
        """to_dict() -> from_dict() preserves every field exactly."""
        restored = QueuedMessage.from_dict(msg.to_dict())
        assert restored.message_id == msg.message_id
        assert restored.chat_id == msg.chat_id
        assert restored.text == msg.text
        assert restored.sender_id == msg.sender_id
        assert restored.sender_name == msg.sender_name
        assert restored.channel == msg.channel
        assert restored.metadata == msg.metadata
        assert restored.status == msg.status
        assert restored.created_at == msg.created_at
        assert restored.updated_at == msg.updated_at

    @given(msg=_queued_message_strategy)
    @settings(max_examples=200)
    def test_double_round_trip_is_idempotent(self, msg: QueuedMessage):
        """Two consecutive round-trips produce identical results."""
        first = QueuedMessage.from_dict(msg.to_dict())
        second = QueuedMessage.from_dict(first.to_dict())
        assert first.message_id == second.message_id
        assert first.chat_id == second.chat_id
        assert first.text == second.text
        assert first.sender_id == second.sender_id
        assert first.sender_name == second.sender_name
        assert first.channel == second.channel
        assert first.metadata == second.metadata
        assert first.status == second.status
        assert first.created_at == second.created_at
        assert first.updated_at == second.updated_at

    @given(msg=_queued_message_strategy)
    @settings(max_examples=200)
    def test_to_dict_output_is_json_serializable(self, msg: QueuedMessage):
        """to_dict() output can be serialized to JSON and back."""
        serialized = json.dumps(msg.to_dict(), ensure_ascii=False)
        parsed = json.loads(serialized)
        restored = QueuedMessage.from_dict(parsed)
        assert restored.message_id == msg.message_id
        assert restored.chat_id == msg.chat_id
        assert restored.text == msg.text
        assert restored.status == msg.status

    @given(msg=_queued_message_strategy)
    @settings(max_examples=200)
    def test_status_survives_json_round_trip(self, msg: QueuedMessage):
        """Status enum value survives full JSON serialization cycle."""
        serialized = json.dumps(msg.to_dict())
        parsed = json.loads(serialized)
        restored = QueuedMessage.from_dict(parsed)
        assert restored.status == msg.status
        assert isinstance(restored.status, MessageStatus)

    @given(
        text=st.text(min_size=0, max_size=10000),
        message_id=st.text(min_size=1, max_size=500),
        chat_id=st.text(min_size=1, max_size=500),
    )
    @settings(max_examples=100)
    def test_text_field_fidelity(
        self, text: str, message_id: str, chat_id: str
    ):
        """Arbitrary text content (including Unicode, empty) survives round-trip."""
        msg = QueuedMessage(message_id=message_id, chat_id=chat_id, text=text)
        restored = QueuedMessage.from_dict(msg.to_dict())
        assert restored.text == text

    @given(
        metadata=_metadata_strategy,
    )
    @settings(max_examples=100)
    def test_metadata_fidelity(self, metadata: dict):
        """Arbitrary metadata dicts survive round-trip via JSON."""
        msg = QueuedMessage(
            message_id="meta-test", chat_id="c1", text="t", metadata=metadata
        )
        # Full JSON round-trip (not just to_dict/from_dict)
        serialized = json.dumps(msg.to_dict(), ensure_ascii=False)
        restored = QueuedMessage.from_dict(json.loads(serialized))
        assert restored.metadata == metadata


# ═══════════════════════════════════════════════════════════════════════════════
# 12. _load_pending() file corruption recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadPendingFileCorruptionRecovery:
    """Tests for _load_pending() recovery from various file corruption scenarios.

    Verifies that _load_pending() produces a valid in-memory state without data
    loss when the JSONL file is in various states of corruption that could occur
    during a crash:
    (a) binary garbage mixed into JSON lines
    (b) file with only completion markers (no pending)
    (c) file where the last line is truncated (no trailing newline)
    (d) empty file
    """

    async def test_binary_garbage_mixed_with_valid_jsonl(self, tmp_path: Path):
        """Binary garbage (non-JSON bytes) mixed into JSON lines.

        Simulates a partial write that corrupted parts of the file with raw
        binary data. Valid entries surrounding the corruption must survive.
        Uses UTF-8-compatible garbage bytes so read_text() can decode the
        file but safe_json_parse(mode=LINE) skips the garbage lines.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_before = make_queued(message_id="before-binary")
        valid_after = make_queued(message_id="after-binary")

        content = (
            json.dumps(valid_before.to_dict()) + "\n"
            + "\x00\x01\x02\x03\x04\x05\n"  # null/control chars — valid UTF-8, not JSON
            + "NOT JSON GARBAGE \x10\x11\x12\n"  # more garbage with control chars
            + json.dumps(valid_after.to_dict()) + "\n"
        )
        qfile.write_text(content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Both valid messages must be recovered; binary garbage lines are skipped
        assert "before-binary" in queue._pending
        assert "after-binary" in queue._pending
        assert await queue.get_pending_count() == 2
        assert queue._pending["before-binary"].text == valid_before.text
        assert queue._pending["after-binary"].text == valid_after.text
        await queue.close()

    async def test_non_utf8_bytes_recovers_valid_lines(self, tmp_path: Path):
        """Non-UTF-8 bytes are replaced; valid entries on other lines are recovered.

        When a crash produces non-UTF-8 bytes in the queue file, read_text with
        errors='replace' substitutes U+FFFD for invalid bytes, allowing valid
        lines elsewhere in the file to be recovered instead of losing everything.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="survives-encoding")
        parts = [
            (json.dumps(valid_msg.to_dict()) + "\n").encode("utf-8"),
            b"\x80\x81\x82\xff\xfe\xfd\n",  # non-UTF-8 bytes
        ]
        qfile.write_bytes(b"".join(parts))

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Valid entry on the first line is recovered; non-UTF-8 line is skipped
        assert "survives-encoding" in queue._pending
        assert await queue.get_pending_count() == 1
        assert queue._initialized is True
        await queue.close()

    async def test_only_completion_markers_no_pending(self, tmp_path: Path):
        """File contains only completion markers (short entries from _append_completion).

        When a file has only completion markers and no full pending messages,
        _load_pending() should produce an empty pending set without errors.
        The completion markers lack required QueuedMessage keys (chat_id, text)
        so they are skipped via the KeyError handler.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        # Completion markers as written by _append_completion()
        markers = [
            json.dumps({
                "message_id": f"msg-{i}",
                "status": "completed",
                "completed_at": time.time() - i,
            })
            for i in range(5)
        ]
        qfile.write_text("\n".join(markers) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert queue._pending == {}
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_truncated_last_line_no_trailing_newline(self, tmp_path: Path):
        """Last line is truncated (no trailing newline — partial write from crash).

        Simulates a crash mid-write: the process wrote a partial JSON line but
        died before writing the closing brace and newline. The preceding valid
        lines must be loaded correctly.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="survives-truncation")
        truncated = '{"message_id": "truncated-crash", "chat_id": "chat-1", "text'

        # Write valid line + truncated line WITHOUT trailing newline
        qfile.write_text(
            json.dumps(valid_msg.to_dict()) + "\n" + truncated,
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Valid message loaded; truncated one skipped
        assert "survives-truncation" in queue._pending
        assert "truncated-crash" not in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()

    async def test_empty_file(self, tmp_path: Path):
        """File exists but is completely empty (zero bytes).

        An empty file is a valid edge case: the queue should start with no
        pending messages and no errors.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        qfile.write_text("", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert queue._pending == {}
        assert await queue.get_pending_count() == 0
        assert queue._initialized is True
        await queue.close()

    async def test_only_whitespace_file(self, tmp_path: Path):
        """File contains only whitespace (spaces, newlines, tabs).

        Whitespace-only lines are stripped by safe_json_parse(mode=LINE) and should
        produce an empty queue without errors.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        qfile.write_text("   \n\n\t\n  \n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert queue._pending == {}
        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_mixed_corruption_with_valid_data_no_data_loss(self, tmp_path: Path):
        """Comprehensive mix: binary garbage, completion markers, truncated line, valid data.

        Verifies that all recoverable valid pending entries are loaded despite
        various forms of corruption interspersed in the file.
        """
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_a = make_queued(message_id="mix-valid-a", chat_id="chat-1", text="hello")
        valid_b = make_queued(message_id="mix-valid-b", chat_id="chat-2", text="world")
        valid_c = make_queued(message_id="mix-valid-c", chat_id="chat-1", text="test")
        completion_marker = json.dumps({
            "message_id": "mix-completed",
            "status": "completed",
            "completed_at": time.time(),
        })

        content = (
            json.dumps(valid_a.to_dict()) + "\n"
            + b"\x00\x01\x02".decode("utf-8", errors="replace") + "\n"
            + completion_marker + "\n"
            + json.dumps(valid_b.to_dict()) + "\n"
            + "CORRUPT NOT JSON\n"
            + json.dumps(valid_c.to_dict()) + "\n"
            + '{"message_id": "truncated", "chat_id"'  # no trailing newline
        )

        qfile.write_text(content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 3
        assert "mix-valid-a" in queue._pending
        assert "mix-valid-b" in queue._pending
        assert "mix-valid-c" in queue._pending
        assert "mix-completed" not in queue._pending
        assert "truncated" not in queue._pending

        # Verify data integrity of loaded messages
        assert queue._pending["mix-valid-a"].text == "hello"
        assert queue._pending["mix-valid-b"].text == "world"
        assert queue._pending["mix-valid-c"].text == "test"
        assert queue._pending["mix-valid-a"].chat_id == "chat-1"
        assert queue._pending["mix-valid-b"].chat_id == "chat-2"
        await queue.close()


class TestOrphanedTmpRecovery:
    """Tests for recovering pending messages from an orphaned .tmp file.

    When the process crashes during _persist_pending()'s atomic write,
    the main queue file may be deleted while the .tmp file survives.
    _promote_orphaned_tmp() renames .tmp → main so that _load_pending()
    can read the surviving data.
    """

    async def test_tmp_promoted_when_main_missing(self, tmp_path: Path):
        """Main file deleted by crash, .tmp has valid pending entries."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        tmp_file = data_dir / "message_queue.tmp"

        msg_a = make_queued(message_id="orphan-1", text="first")
        msg_b = make_queued(message_id="orphan-2", text="second")

        # Write valid data ONLY to .tmp (main doesn't exist)
        tmp_file.write_text(
            json.dumps(msg_a.to_dict()) + "\n"
            + json.dumps(msg_b.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 2
        assert "orphan-1" in queue._pending
        assert "orphan-2" in queue._pending
        assert queue._pending["orphan-1"].text == "first"
        assert queue._pending["orphan-2"].text == "second"
        # .tmp should have been promoted to main
        assert qfile.exists()
        assert not tmp_file.exists()
        await queue.close()

    async def test_tmp_cleaned_up_when_main_exists(self, tmp_path: Path):
        """Both files exist — .tmp is removed, main is authoritative."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"
        tmp_file = data_dir / "message_queue.tmp"

        msg_main = make_queued(message_id="from-main", text="main data")
        msg_tmp = make_queued(message_id="from-tmp", text="tmp data")

        qfile.write_text(json.dumps(msg_main.to_dict()) + "\n", encoding="utf-8")
        tmp_file.write_text(json.dumps(msg_tmp.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        # Main is authoritative; tmp-only entries are NOT merged
        assert "from-main" in queue._pending
        assert "from-tmp" not in queue._pending
        assert not tmp_file.exists()
        await queue.close()

    async def test_no_tmp_file_normal_startup(self, tmp_path: Path):
        """Normal startup with no .tmp file — no warnings or errors."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 0
        await queue.close()

    async def test_partial_tmp_promoted_and_lines_skipped(self, tmp_path: Path):
        """.tmp has both valid and corrupted lines — valid ones are recovered."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        tmp_file = data_dir / "message_queue.tmp"

        valid_msg = make_queued(message_id="partial-ok", text="survives")
        tmp_file.write_text(
            json.dumps(valid_msg.to_dict()) + "\n"
            + "CORRUPT LINE\n"
            + '{"message_id": "truncated", "chat_i',
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "partial-ok" in queue._pending
        assert "truncated" not in queue._pending
        assert await queue.get_pending_count() == 1
        await queue.close()


class TestBackupOnCorruption:
    """Tests for creating a backup of the corrupted queue file before eviction.

    When _load_pending() detects corrupted lines, it creates a timestamped
    backup in .data/backups/ before the eager eviction overwrites the file.
    This preserves the corrupted data for manual inspection.
    """

    async def test_backup_created_when_corruption_detected(self, tmp_path: Path):
        """Corrupted lines trigger a backup before eviction."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="backup-ok")
        original_content = (
            json.dumps(valid_msg.to_dict()) + "\n"
            + "CORRUPT LINE\n"
        )
        qfile.write_text(original_content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "backup-ok" in queue._pending

        # Backup should have been created
        backup_dir = data_dir / "backups"
        assert backup_dir.exists()
        backups = list(backup_dir.glob("message_queue_*.jsonl.bak"))
        assert len(backups) == 1
        # Backup contains the original corrupted content
        assert backups[0].read_text(encoding="utf-8") == original_content
        await queue.close()

    async def test_no_backup_when_file_is_clean(self, tmp_path: Path):
        """No backup created when there are no corrupted lines."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="clean-ok")
        qfile.write_text(json.dumps(valid_msg.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert "clean-ok" in queue._pending

        backup_dir = data_dir / "backups"
        if backup_dir.exists():
            backups = list(backup_dir.glob("message_queue_*.jsonl.bak"))
            assert len(backups) == 0
        await queue.close()

    async def test_backup_preserves_all_lines_including_corrupt(self, tmp_path: Path):
        """Backup preserves the full corrupted file for manual recovery."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_a = make_queued(message_id="keep-a", text="hello")
        valid_b = make_queued(message_id="keep-b", text="world")
        original_content = (
            json.dumps(valid_a.to_dict()) + "\n"
            + "GARBAGE\n"
            + json.dumps(valid_b.to_dict()) + "\n"
            + '{"partial":\n'
        )
        qfile.write_text(original_content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert await queue.get_pending_count() == 2

        backup_dir = data_dir / "backups"
        backups = list(backup_dir.glob("message_queue_*.jsonl.bak"))
        assert len(backups) == 1
        # Backup preserves ALL original lines, including corrupt ones
        assert backups[0].read_text(encoding="utf-8") == original_content
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 13. QueueCorruptionResult dataclass & _last_corruption_result tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueueCorruptionResult:
    """Tests for QueueCorruptionResult dataclass and _last_corruption_result."""

    async def test_no_corruption_result_on_clean_file(self, tmp_path: Path):
        """Clean file produces _last_corruption_result with is_corrupted=False."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_msg = make_queued(message_id="clean-1")
        qfile.write_text(json.dumps(valid_msg.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        assert queue._last_corruption_result is not None
        assert queue._last_corruption_result.is_corrupted is False
        assert queue._last_corruption_result.corrupted_lines == []
        assert queue._last_corruption_result.total_lines == 1
        assert queue._last_corruption_result.valid_lines == 1
        assert queue._last_corruption_result.pending_lines == 1
        await queue.close()

    async def test_corruption_result_tracks_bad_lines(self, tmp_path: Path):
        """Corrupted lines are tracked with line numbers in _last_corruption_result."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="ok-1")
        qfile.write_text(
            json.dumps(valid.to_dict()) + "\n"
            + "CORRUPT LINE\n"
            + '{"partial":\n',
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = queue._last_corruption_result
        assert result is not None
        assert result.is_corrupted is True
        assert result.total_lines == 3
        assert result.valid_lines == 1
        assert result.pending_lines == 1
        assert result.corrupted_lines == [2, 3]
        assert len(result.error_details) == 2
        await queue.close()

    async def test_corruption_result_tracks_completed_lines(self, tmp_path: Path):
        """Completed full-message entries are counted separately from pending."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        pending = make_queued(message_id="pend-1")
        # Full QueuedMessage dict with status=completed (not the short
        # completion marker which lacks chat_id/text and would be counted
        # as corrupted instead).
        completed = make_queued(message_id="done-1", status=MessageStatus.COMPLETED)
        qfile.write_text(
            json.dumps(pending.to_dict()) + "\n" + json.dumps(completed.to_dict()) + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = queue._last_corruption_result
        assert result is not None
        assert result.completed_lines == 1
        assert result.pending_lines == 1
        assert result.valid_lines == 2
        await queue.close()

    async def test_no_corruption_result_before_connect(self, tmp_path: Path):
        """_last_corruption_result is None before connect()."""
        queue = MessageQueue(str(tmp_path / "data"))
        assert queue._last_corruption_result is None


# ═══════════════════════════════════════════════════════════════════════════════
# 14. validate() public method
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateMethod:
    """Tests for MessageQueue.validate() — non-destructive integrity check."""

    async def test_validate_clean_file(self, tmp_path: Path):
        """validate() returns is_corrupted=False for a clean file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="v-ok-1")
        qfile.write_text(json.dumps(valid.to_dict()) + "\n", encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.validate()
        assert isinstance(result, QueueCorruptionResult)
        assert result.is_corrupted is False
        assert result.corrupted_lines == []
        assert result.total_lines == 1
        assert result.valid_lines == 1
        assert result.pending_lines == 1
        await queue.close()

    async def test_validate_corrupted_file(self, tmp_path: Path):
        """validate() detects corrupted lines without modifying anything."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="v-ok-2")
        original_content = (
            json.dumps(valid.to_dict()) + "\n"
            + "GARBAGE\n"
            + '{"truncated":\n'
        )
        qfile.write_text(original_content, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.validate()
        assert result.is_corrupted is True
        assert result.corrupted_lines == [2, 3]
        assert result.valid_lines == 1
        assert result.total_lines == 3

        # validate() must NOT modify the file
        assert qfile.read_text(encoding="utf-8") == original_content
        await queue.close()

    async def test_validate_nonexistent_file(self, tmp_path: Path):
        """validate() handles missing file gracefully."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.validate()
        assert isinstance(result, QueueCorruptionResult)
        assert "Queue file does not exist" in result.error_details
        await queue.close()

    async def test_validate_with_completed_entries(self, tmp_path: Path):
        """validate() distinguishes pending vs completed entries."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        pending = make_queued(message_id="v-pend")
        completed = json.dumps({
            "message_id": "v-done",
            "status": "completed",
            "completed_at": time.time(),
        })
        qfile.write_text(
            json.dumps(pending.to_dict()) + "\n" + completed + "\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.validate()
        assert result.pending_lines == 1
        assert result.completed_lines == 1
        assert result.total_lines == 2
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 15. repair() public method
# ═══════════════════════════════════════════════════════════════════════════════


class TestRepairMethod:
    """Tests for MessageQueue.repair() — corruption detection + repair."""

    async def test_repair_removes_corrupted_lines(self, tmp_path: Path):
        """repair() removes corrupted lines and preserves valid ones."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid_a = make_queued(message_id="repair-ok-a", text="hello")
        valid_b = make_queued(message_id="repair-ok-b", text="world")
        original = (
            json.dumps(valid_a.to_dict()) + "\n"
            + "CORRUPT\n"
            + json.dumps(valid_b.to_dict()) + "\n"
            + '{"partial":\n'
        )
        qfile.write_text(original, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.repair()
        assert result.is_corrupted is True
        assert result.repaired is True
        assert len(result.corrupted_lines) == 2

        # After repair, only valid lines remain in the file
        repaired_content = qfile.read_text(encoding="utf-8")
        assert "CORRUPT" not in repaired_content
        assert '{"partial":' not in repaired_content
        assert "repair-ok-a" in repaired_content
        assert "repair-ok-b" in repaired_content

        # In-memory pending reflects repaired state
        assert "repair-ok-a" in queue._pending
        assert "repair-ok-b" in queue._pending
        await queue.close()

    async def test_repair_creates_backup(self, tmp_path: Path):
        """repair() creates a backup before modifying the file."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="repair-bak")
        qfile.write_text(
            json.dumps(valid.to_dict()) + "\n" + "BAD LINE\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.repair()
        assert result.backup_path is not None
        backup_path = Path(result.backup_path)
        assert backup_path.exists()
        # Backup preserves the original corrupted content
        assert "BAD LINE" in backup_path.read_text(encoding="utf-8")
        await queue.close()

    async def test_repair_clean_file_is_noop(self, tmp_path: Path):
        """repair() on a clean file returns is_corrupted=False, repaired=False."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="repair-clean")
        original = json.dumps(valid.to_dict()) + "\n"
        qfile.write_text(original, encoding="utf-8")

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.repair()
        assert result.is_corrupted is False
        assert result.repaired is False
        # File unchanged
        assert qfile.read_text(encoding="utf-8") == original
        await queue.close()

    async def test_repair_preserves_data_integrity(self, tmp_path: Path):
        """After repair, loaded messages have correct field values."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(
            message_id="repair-fields",
            chat_id="chat-42",
            text="exact text",
            sender_id="sender-x",
            sender_name="Alice",
        )
        qfile.write_text(
            json.dumps(valid.to_dict()) + "\n" + "GARBAGE\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        result = await queue.repair()
        assert result.repaired is True

        msg = queue._pending["repair-fields"]
        assert msg.chat_id == "chat-42"
        assert msg.text == "exact text"
        assert msg.sender_id == "sender-x"
        assert msg.sender_name == "Alice"
        await queue.close()

    async def test_repair_then_validate_is_clean(self, tmp_path: Path):
        """After repair, validate() reports the file as clean."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        qfile = data_dir / "message_queue.jsonl"

        valid = make_queued(message_id="repair-then-val")
        qfile.write_text(
            json.dumps(valid.to_dict()) + "\n" + "BAD\n",
            encoding="utf-8",
        )

        queue = MessageQueue(str(data_dir))
        await queue.connect()

        repair_result = await queue.repair()
        assert repair_result.repaired is True

        validate_result = await queue.validate()
        assert validate_result.is_corrupted is False
        assert validate_result.corrupted_lines == []
        await queue.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Append durability (flush + fsync)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAppendDurability:
    """Tests for flush+fsync durability in append operations."""

    async def test_enqueue_persists_to_disk(self, tmp_path: Path):
        """After enqueue, the message is readable directly from disk."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        msg = await queue.enqueue(
            make_incoming(message_id="dur-1", text="durable message")
        )
        await queue.close()

        # Read file directly — must contain the enqueued message
        qfile = data_dir / "message_queue.jsonl"
        content = qfile.read_text(encoding="utf-8")
        assert "dur-1" in content
        assert "durable message" in content

    async def test_complete_persists_marker_to_disk(self, tmp_path: Path):
        """After complete, the completion marker is on disk (before close)."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        queue._compact_threshold = 100  # prevent compaction, force append
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="dur-comp-1"))
        await queue.complete("dur-comp-1")

        # Don't close — check disk state immediately after complete
        qfile = data_dir / "message_queue.jsonl"
        content = qfile.read_text(encoding="utf-8")
        assert "dur-comp-1" in content
        assert "completed" in content

        await queue.close()

    async def test_multiple_enqueues_all_persisted(self, tmp_path: Path):
        """Multiple enqueue calls each fsync — all data reaches disk."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        for i in range(10):
            await queue.enqueue(make_incoming(message_id=f"dur-multi-{i}"))

        await queue.close()

        qfile = data_dir / "message_queue.jsonl"
        content = qfile.read_text(encoding="utf-8")
        for i in range(10):
            assert f"dur-multi-{i}" in content
