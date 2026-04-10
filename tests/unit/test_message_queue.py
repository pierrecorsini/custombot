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

from src.message_queue import (
    MessageQueue,
    MessageStatus,
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
    sender_name: str = "Tester"
    channel_type: str = "whatsapp"
    # These are not on the real IncomingMessage; from_incoming_message uses
    # getattr with defaults, so including them lets us test the fallback path.
    channel: Optional[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def make_incoming(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
    sender_name: str = "Tester",
    channel: Optional[str] = "whatsapp",
    metadata: Optional[Dict[str, Any]] = None,
) -> FakeIncomingMessage:
    """Factory for FakeIncomingMessage with sensible defaults."""
    return FakeIncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        text=text,
        sender_name=sender_name,
        channel=channel,
        metadata=metadata or {},
    )


def make_queued(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
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

    def test_missing_channel_attribute_uses_default(self):
        """When IncomingMessage has no channel attr, getattr returns None."""
        incoming = make_incoming(channel=None)
        incoming.channel = None  # explicit
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.channel is None

    def test_missing_metadata_attribute_uses_default(self):
        incoming = make_incoming(metadata=None)
        incoming.metadata = None
        msg = QueuedMessage.from_incoming_message(incoming)
        # getattr returns None which is NOT the default {} — this tests actual behavior
        assert msg.metadata is None

    def test_custom_metadata_preserved(self):
        incoming = make_incoming(metadata={"reply_to": "msg-0"})
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.metadata == {"reply_to": "msg-0"}

    def test_with_real_incoming_message_fields(self):
        """Simulate real IncomingMessage that lacks channel/metadata attrs."""
        # Create a simple namespace-based object without channel/metadata
        incoming = FakeIncomingMessage(
            message_id="real-1",
            chat_id="chat-99",
            text="world",
            sender_name="Bob",
            channel_type="whatsapp",
            channel=None,
            metadata=None,
        )
        # Remove attrs to simulate real IncomingMessage (which doesn't have them)
        del incoming.channel
        del incoming.metadata
        msg = QueuedMessage.from_incoming_message(incoming)
        assert msg.channel is None
        assert msg.metadata == {}


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
            json.dumps(completed_msg.to_dict())
            + "\n"
            + json.dumps(pending_msg.to_dict())
            + "\n",
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
            "this is not json\n"
            + json.dumps(valid_msg.to_dict())
            + "\n"
            + "\n",  # blank line
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
        # After complete + persist, no pending messages remain in file
        assert len(lines_after) == 0
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
        msg = await queue.enqueue(
            make_incoming(message_id="lc-1", chat_id="chat-A", text="hi")
        )
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

        # Very recent message — still stale with timeout=0
        msg = make_queued(message_id="recent-1", updated_at=time.time())
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
        """complete() triggers _persist_pending which does atomic rewrite."""
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        await queue.enqueue(make_incoming(message_id="r1"))
        await queue.enqueue(make_incoming(message_id="r2"))

        # Complete one — triggers atomic rewrite
        await queue.complete("r1")

        # File should contain only r2
        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["message_id"] == "r2"
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
        async with get_message_queue(
            str(tmp_path / "data"), stale_timeout=120
        ) as queue:
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
        assert queue._pending["big-1"].text == big_text
        await queue.close()

        # Verify it survives round-trip
        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert queue2._pending["big-1"].text == big_text
        await queue2.close()

    async def test_special_characters_in_metadata(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        meta = {
            "path": "C:\\Users\\test\\file.txt",
            "regex": ".*\\d+",
            "nested": {"key": 'val with "quotes"'},
        }
        await queue.enqueue(make_incoming(message_id="special-1", metadata=meta))
        await queue.close()

        queue2 = MessageQueue(str(data_dir))
        await queue2.connect()
        assert queue2._pending["special-1"].metadata == meta
        await queue2.close()

    async def test_empty_text_message(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        await queue.enqueue(make_incoming(message_id="empty-text", text=""))
        assert queue._pending["empty-text"].text == ""
        await queue.close()

    async def test_empty_metadata(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()
        msg = await queue.enqueue(make_incoming(message_id="no-meta", metadata={}))
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
                metadata={"x": 1},
            )
        )
        await queue.enqueue(make_incoming(message_id="remove-1"))
        await queue.complete("remove-1")

        qfile = data_dir / "message_queue.jsonl"
        lines = qfile.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["message_id"] == "keep-1"
        assert data["text"] == "important data"
        assert data["metadata"]["x"] == 1
        await queue.close()


class TestConcurrentAccess:
    """Tests for concurrent/parallel access safety."""

    async def test_concurrent_enqueues(self, tmp_path: Path):
        queue = MessageQueue(str(tmp_path / "data"))
        await queue.connect()

        # Enqueue 50 messages concurrently
        tasks = [
            queue.enqueue(make_incoming(message_id=f"concurrent-{i}"))
            for i in range(50)
        ]
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
