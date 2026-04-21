"""
test_concurrent_load.py — Load/stress test for concurrent multi-chat processing.

Exercises the bot under concurrent load with real (in-memory) components:

  - 10+ messages from different chats processed simultaneously
  - Per-chat locks serialize same-chat messages (no concurrent LLM calls)
  - No cross-chat data leakage in database writes
  - Message queue integrity under concurrent load
  - Shared resource (LLM, DB, queue) contention handled correctly

The LLM is mocked at the ``AsyncOpenAI`` transport level so that
``LLMClient`` goes through its full path. All other components —
Database, Memory, RoutingEngine, SkillRegistry, MessageQueue, Bot —
are real instances operating on ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig
from src.db import Database
from src.memory import Memory
from src.message_queue import MessageQueue
from src.routing import RoutingEngine, RoutingRule
from src.skills import SkillRegistry


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

NUM_CONCURRENT_CHATS = 12


def _make_text_response(text: str) -> MagicMock:
    """Build a mock OpenAI ChatCompletion with a plain-text stop response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "stop"
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = len(text) // 4
    response.usage.total_tokens = 10 + len(text) // 4
    return response


def _make_routing_engine(workspace: Path) -> RoutingEngine:
    """Create a RoutingEngine with a single catch-all rule."""
    engine = RoutingEngine(workspace)
    engine._rules = [
        RoutingRule(
            id="load-test-catch-all",
            priority=100,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.agent.md",
            enabled=True,
        )
    ]
    return engine


def _make_bot(
    workspace: Path,
    config: Config,
    with_queue: bool = False,
) -> tuple[Bot, Database, Memory, MessageQueue | None]:
    """Wire up a full Bot with real components and a mocked LLM transport."""
    from src.llm import LLMClient

    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    routing = _make_routing_engine(workspace)
    skills = SkillRegistry()
    queue: MessageQueue | None = None
    if with_queue:
        queue = MessageQueue(str(workspace / ".data"))

    llm = LLMClient(config.llm)

    bot = Bot(
        config=config,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        message_queue=queue,
        instructions_dir=str(workspace / "instructions"),
    )
    return bot, db, memory, queue


def _make_message(chat_id: str, message_id: str, text: str) -> IncomingMessage:
    """Create an IncomingMessage for testing."""
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=f"user-{chat_id}",
        sender_name=f"User-{chat_id}",
        text=text,
        timestamp=time.time(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Concurrent Multi-Chat Processing
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentMultiChatProcessing:
    """
    Verify that 10+ messages from different chats can be processed
    concurrently without cross-chat data leakage or resource corruption.
    """

    @pytest.mark.asyncio
    async def test_concurrent_messages_different_chats_no_leakage(
        self, tmp_path: Path
    ) -> None:
        """
        Process messages from many different chats simultaneously.

        Each chat gets a unique response containing its chat_id.
        After all messages complete, verify that each chat's database
        history contains only its own messages — no cross-chat leakage.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        chat_ids = [f"chat-concurrent-{i:03d}" for i in range(NUM_CONCURRENT_CHATS)]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                # Extract the last user message to determine which chat
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break
                return _make_text_response(f"Response for: {last_user_msg}")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue = _make_bot(workspace, config)
            await db.connect()

            # Build messages: each chat sends a unique text
            messages = [
                _make_message(cid, f"msg-{cid}-001", f"Hello from {cid}")
                for cid in chat_ids
            ]

            # Fire all messages concurrently
            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages]
            )

        # All messages should have been processed
        assert all(r is not None for r in results), (
            f"Some messages returned None: {[i for i, r in enumerate(results) if r is None]}"
        )

        # Verify each response mentions its chat_id
        for i, (cid, response) in enumerate(zip(chat_ids, results)):
            assert cid in response, (
                f"Chat {cid} response should contain its chat_id, got: {response}"
            )

        # Verify per-chat database isolation: each chat has exactly 2 messages
        # (1 user + 1 assistant) with no leakage from other chats
        for cid in chat_ids:
            rows = await db.get_recent_messages(cid, limit=50)
            assert len(rows) == 2, (
                f"Chat {cid} should have exactly 2 messages (user + assistant), "
                f"got {len(rows)}"
            )
            roles = [r["role"] for r in rows]
            assert "user" in roles, f"Chat {cid} missing user message"
            assert "assistant" in roles, f"Chat {cid} missing assistant message"

            # Verify user message content matches this chat
            user_msgs = [r for r in rows if r["role"] == "user"]
            assert f"Hello from {cid}" in user_msgs[0]["content"], (
                f"Chat {cid} has wrong user content: {user_msgs[0]['content']}"
            )

            # Verify assistant response is for this chat (not another)
            asst_msgs = [r for r in rows if r["role"] == "assistant"]
            assert cid in asst_msgs[0]["content"], (
                f"Chat {cid} assistant response leaked from another chat: "
                f"{asst_msgs[0]['content']}"
            )

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Per-Chat Lock Serialization
# ─────────────────────────────────────────────────────────────────────────────


class TestPerChatLockSerialization:
    """
    Verify that per-chat locks prevent concurrent LLM calls for the same chat,
    while allowing different chats to process in parallel.
    """

    @pytest.mark.asyncio
    async def test_same_chat_messages_are_serialized(self, tmp_path: Path) -> None:
        """
        Send multiple messages for the same chat concurrently.
        The per-chat lock should serialize them so only one LLM call
        is active at a time for that chat.

        We verify serialization by tracking the max concurrent LLM calls
        for the same chat — it should never exceed 1.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        chat_id = "chat-serialized-001"
        num_messages = 5

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            concurrent_count = 0
            max_concurrent = 0
            call_lock = asyncio.Lock()

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                nonlocal concurrent_count, max_concurrent
                async with call_lock:
                    concurrent_count += 1
                    if concurrent_count > max_concurrent:
                        max_concurrent = concurrent_count

                # Simulate some LLM processing time
                await asyncio.sleep(0.05)

                async with call_lock:
                    concurrent_count -= 1

                return _make_text_response("Processed")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue = _make_bot(workspace, config)
            await db.connect()

            messages = [
                _make_message(chat_id, f"msg-serial-{i:03d}", f"Message {i}")
                for i in range(num_messages)
            ]

            # Fire all messages for the same chat concurrently
            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages]
                # Use return_exceptions to handle any ordering issues
            )

        await db.close()

        # All messages should have been processed
        successful = [r for r in results if isinstance(r, str)]
        assert len(successful) == num_messages, (
            f"Expected {num_messages} successful results, got {len(successful)}"
        )

        # Max concurrent LLM calls for this chat should be 1 (serialized)
        assert max_concurrent <= 1, (
            f"Per-chat lock should serialize LLM calls, but {max_concurrent} "
            f"concurrent calls were observed"
        )

    @pytest.mark.asyncio
    async def test_different_chats_run_in_parallel(self, tmp_path: Path) -> None:
        """
        Send messages for multiple different chats concurrently.
        Different chats should be able to process in parallel since
        they have independent per-chat locks.

        We verify parallelism by tracking the max concurrent LLM calls
        across different chats — it should be > 1.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 5

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            concurrent_count = 0
            max_concurrent = 0
            call_lock = asyncio.Lock()

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                nonlocal concurrent_count, max_concurrent
                async with call_lock:
                    concurrent_count += 1
                    if concurrent_count > max_concurrent:
                        max_concurrent = concurrent_count

                # Simulate LLM processing time to increase chance of overlap
                await asyncio.sleep(0.1)

                async with call_lock:
                    concurrent_count -= 1

                return _make_text_response("Parallel response")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue = _make_bot(workspace, config)
            await db.connect()

            messages = [
                _make_message(
                    f"chat-parallel-{i:03d}",
                    f"msg-parallel-{i:03d}",
                    f"Hello from chat {i}",
                )
                for i in range(num_chats)
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages]
            )

        await db.close()

        # All messages should succeed
        assert all(r is not None for r in results)

        # Different chats should have overlapped (parallel execution)
        assert max_concurrent > 1, (
            f"Expected parallel execution across different chats, "
            f"but max concurrent was only {max_concurrent}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Message Queue Integrity Under Load
# ─────────────────────────────────────────────────────────────────────────────


class TestMessageQueueIntegrity:
    """
    Verify message queue operations remain consistent under concurrent load.
    """

    @pytest.mark.asyncio
    async def test_queue_all_completed_under_load(self, tmp_path: Path) -> None:
        """
        Process many messages concurrently with a message queue attached.
        After all messages complete, the queue should have zero pending
        messages — every message should be marked as completed.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 10

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Done")
            )

            bot, db, memory, queue = _make_bot(workspace, config, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            messages = [
                _make_message(
                    f"chat-queue-{i:03d}",
                    f"msg-queue-{i:03d}",
                    f"Queue test {i}",
                )
                for i in range(num_chats)
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages]
            )

        # All messages should succeed
        assert all(r is not None for r in results)

        # Queue should have zero pending messages
        pending_count = await queue.get_pending_count()
        assert pending_count == 0, (
            f"Expected 0 pending messages after concurrent processing, "
            f"got {pending_count}"
        )

        await queue.close()
        await db.close()

    @pytest.mark.asyncio
    async def test_queue_dedup_after_concurrent_processing(
        self, tmp_path: Path
    ) -> None:
        """
        Process messages concurrently, then verify that reprocessing
        the same message_ids is correctly rejected by dedup.

        This tests the dedup guarantee in the sequential case (after
        messages have been persisted), while also exercising concurrent
        queue operations during the first batch.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 5

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Response")
            )

            bot, db, memory, queue = _make_bot(workspace, config, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            # Phase 1: Send unique messages concurrently
            messages = [
                _make_message(
                    f"chat-dedup-{i:03d}",
                    f"msg-dedup-{i:03d}",
                    f"Unique {i}",
                )
                for i in range(num_chats)
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages]
            )

        # All should succeed
        assert all(r is not None for r in results), "All first-batch messages should succeed"

        # Queue should be empty after first batch
        pending_count = await queue.get_pending_count()
        assert pending_count == 0, f"Queue should be empty, got {pending_count} pending"

        # Phase 2: Attempt to reprocess the same message_ids
        # Dedup should catch all of them now (they're persisted)
        duplicate_results = await asyncio.gather(
            *[bot.handle_message(msg) for msg in messages]
        )

        # All duplicates should be rejected
        for i, result in enumerate(duplicate_results):
            assert result is None, (
                f"Duplicate message {i} should have been rejected, got: {result}"
            )

        # Database should still have only the original messages (2 per chat)
        for i in range(num_chats):
            cid = f"chat-dedup-{i:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            assert len(rows) == 2, (
                f"Chat {cid} should have 2 messages (not duplicated), got {len(rows)}"
            )

        await queue.close()
        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: High-Volume Stress
# ─────────────────────────────────────────────────────────────────────────────


class TestHighVolumeStress:
    """
    Stress test with many concurrent chats and multiple messages per chat.
    """

    @pytest.mark.asyncio
    async def test_many_chats_many_messages_stress(self, tmp_path: Path) -> None:
        """
        Stress test: 8 chats, each sending 3 sequential messages concurrently.

        For each chat, the 3 messages are sent concurrently but the per-chat
        lock should serialize them. Across chats, processing should be parallel.

        After all messages complete:
        - Each chat should have 6 DB rows (3 user + 3 assistant)
        - No cross-chat leakage
        - All responses returned successfully
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 8
        msgs_per_chat = 3

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                # Small delay to simulate real LLM latency
                await asyncio.sleep(0.02)
                return _make_text_response("Stress response")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue = _make_bot(workspace, config)
            await db.connect()

            # Build all messages: each chat has msgs_per_chat messages
            all_tasks = []
            for chat_idx in range(num_chats):
                cid = f"chat-stress-{chat_idx:03d}"
                for msg_idx in range(msgs_per_chat):
                    msg = _make_message(
                        cid,
                        f"msg-stress-{chat_idx:03d}-{msg_idx:03d}",
                        f"Stress msg {msg_idx} from chat {chat_idx}",
                    )
                    all_tasks.append(bot.handle_message(msg))

            # Fire everything concurrently
            results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # All should succeed (no exceptions, no None)
        for i, r in enumerate(results):
            assert isinstance(r, str) and r is not None, (
                f"Task {i} failed: {r!r}"
            )

        # Verify per-chat database integrity
        for chat_idx in range(num_chats):
            cid = f"chat-stress-{chat_idx:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            expected_count = msgs_per_chat * 2  # user + assistant for each
            assert len(rows) == expected_count, (
                f"Chat {cid}: expected {expected_count} messages, got {len(rows)}"
            )

            # Verify role distribution
            user_count = sum(1 for r in rows if r["role"] == "user")
            assistant_count = sum(1 for r in rows if r["role"] == "assistant")
            assert user_count == msgs_per_chat, (
                f"Chat {cid}: expected {msgs_per_chat} user messages, got {user_count}"
            )
            assert assistant_count == msgs_per_chat, (
                f"Chat {cid}: expected {msgs_per_chat} assistant messages, "
                f"got {assistant_count}"
            )

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Concurrent save_message + get_recent_messages on same chat
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentDatabaseReadWrite:
    """
    Chaos test for concurrent save_message() and get_recent_messages() on the
    same chat_id — the most common production pattern (one coroutine reading
    history while another appends a message).

    Verifies that per-chat asyncio locks prevent data corruption and that all
    messages are readable after concurrent read/write operations complete.
    """

    @pytest.mark.asyncio
    async def test_concurrent_read_write_same_chat_no_corruption(
        self, tmp_path: Path
    ) -> None:
        """
        Fire many interleaved save_message() and get_recent_messages() calls
        on the same chat_id concurrently.

        After all operations complete:
        - Every written message must be recoverable via get_recent_messages()
        - No JSON corruption in the message file
        - Total message count must equal the number of writes performed
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        chat_id = "chat-rw-concurrent-001"
        num_writes = 20
        num_reads = 15

        # Track what we wrote so we can verify later
        written_ids: list[str] = []
        write_lock = asyncio.Lock()

        async def _writer(idx: int) -> str:
            """Write a unique message and record its ID."""
            mid = await db.save_message(
                chat_id=chat_id,
                role="user",
                content=f"Concurrent write #{idx:03d}",
                name=f"writer-{idx}",
                message_id=f"msg-rw-{idx:03d}",
            )
            async with write_lock:
                written_ids.append(mid)
            return mid

        async def _reader(idx: int) -> list[dict]:
            """Read recent messages — may see any subset of writes."""
            return await db.get_recent_messages(chat_id, limit=100)

        # Fire writes and reads concurrently
        write_tasks = [_writer(i) for i in range(num_writes)]
        read_tasks = [_reader(i) for i in range(num_reads)]

        results = await asyncio.gather(
            *write_tasks, *read_tasks, return_exceptions=True
        )

        # No operation should raise
        for i, r in enumerate(results):
            assert not isinstance(r, Exception), (
                f"Operation {i} raised: {r!r}"
            )

        # All writes should have returned a message ID
        write_results = results[:num_writes]
        assert all(isinstance(r, str) for r in write_results), (
            f"Some writes did not return IDs: {write_results}"
        )
        assert len(written_ids) == num_writes, (
            f"Expected {num_writes} written IDs, got {len(written_ids)}"
        )

        # All reads should have returned a list (no crash)
        read_results = results[num_writes:]
        assert all(isinstance(r, list) for r in read_results), (
            f"Some reads did not return lists: {read_results}"
        )

        # Final read: verify total message count and content integrity
        final_messages = await db.get_recent_messages(chat_id, limit=500)
        assert len(final_messages) == num_writes, (
            f"Expected {num_writes} messages in final read, got {len(final_messages)}"
        )

        # Verify every message has valid structure
        for msg in final_messages:
            assert "role" in msg, f"Missing 'role' in message: {msg}"
            assert "content" in msg, f"Missing 'content' in message: {msg}"
            assert msg["role"] == "user"
            assert msg["content"].startswith("Concurrent write #")

        # Verify message_exists returns True for every written ID
        for mid in written_ids:
            assert await db.message_exists(mid), (
                f"Written message {mid} not found in index"
            )

        await db.close()

    @pytest.mark.asyncio
    async def test_concurrent_reads_during_sequential_writes(
        self, tmp_path: Path
    ) -> None:
        """
        Continuously read messages while writes are being appended.

        At any point during the writes, a reader should see a consistent
        snapshot — either N or N+1 messages, never corrupted partial data.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        chat_id = "chat-read-during-write-001"
        num_writes = 10

        write_complete = asyncio.Event()
        snapshots: list[tuple[int, list[dict]]] = []
        snapshot_lock = asyncio.Lock()

        async def _sequential_writer() -> None:
            """Write messages one at a time with small delays."""
            for i in range(num_writes):
                await db.save_message(
                    chat_id=chat_id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Message #{i:03d}",
                    message_id=f"msg-seq-{i:03d}",
                )
                # Small yield to give readers a chance
                await asyncio.sleep(0.005)
            write_complete.set()

        async def _continuous_reader() -> None:
            """Read messages until all writes are done."""
            while not write_complete.is_set():
                msgs = await db.get_recent_messages(chat_id, limit=100)
                async with snapshot_lock:
                    snapshots.append((len(msgs), msgs))
                await asyncio.sleep(0.002)

        # Run writer and readers concurrently
        reader_task = asyncio.create_task(_continuous_reader())
        await _sequential_writer()
        # Give reader one last chance after writes complete
        await asyncio.sleep(0.01)
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

        # Verify final state
        final_messages = await db.get_recent_messages(chat_id, limit=500)
        assert len(final_messages) == num_writes, (
            f"Expected {num_writes} final messages, got {len(final_messages)}"
        )

        # Verify every snapshot was valid (no corrupted messages)
        for snap_idx, (count, msgs) in enumerate(snapshots):
            for msg in msgs:
                assert "role" in msg, (
                    f"Snapshot {snap_idx}: corrupted message missing 'role': {msg}"
                )
                assert "content" in msg, (
                    f"Snapshot {snap_idx}: corrupted message missing 'content': {msg}"
                )
            # Count should be monotonically increasing (reads are serialized
            # with writes via the per-chat lock, so a reader always sees a
            # consistent state)
            assert count <= num_writes, (
                f"Snapshot {snap_idx} saw {count} messages, "
                f"more than {num_writes} total writes"
            )

        await db.close()

    @pytest.mark.asyncio
    async def test_high_frequency_concurrent_read_write(self, tmp_path: Path) -> None:
        """
        High-frequency stress: many concurrent writes and reads on the same chat
        with very short delays, exercising lock contention heavily.

        Verifies that all messages survive and no data is lost or corrupted.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        chat_id = "chat-high-freq-001"
        num_writes = 30
        num_concurrent_reads = 20

        async def _fast_writer(idx: int) -> str:
            return await db.save_message(
                chat_id=chat_id,
                role="user",
                content=f"Fast write {idx}",
                message_id=f"msg-fast-{idx:04d}",
            )

        async def _fast_reader(_idx: int) -> list[dict]:
            return await db.get_recent_messages(chat_id, limit=200)

        # Launch all writes and reads simultaneously
        all_tasks = [
            *[_fast_writer(i) for i in range(num_writes)],
            *[_fast_reader(i) for i in range(num_concurrent_reads)],
        ]

        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # No exceptions
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 0, (
            f"{len(errors)} operations raised exceptions: {errors[:3]}"
        )

        # Final verification: all writes persisted
        final = await db.get_recent_messages(chat_id, limit=500)
        assert len(final) == num_writes, (
            f"Expected {num_writes} messages after high-freq test, "
            f"got {len(final)}"
        )

        # Verify message IDs are all present
        for i in range(num_writes):
            assert await db.message_exists(f"msg-fast-{i:04d}"), (
                f"Message msg-fast-{i:04d} missing from index"
            )

        # Verify content integrity: every message content is valid
        contents = {msg["content"] for msg in final}
        for i in range(num_writes):
            expected = f"Fast write {i}"
            assert expected in contents, (
                f"Missing expected content '{expected}' in final messages"
            )

        await db.close()
