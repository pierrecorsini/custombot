"""
test_chaos_concurrent.py — Chaos/stress tests for concurrent message processing.

Exercises the bot under concurrent load with **forced failures** to verify:

  - Per-chat locks correctly isolate failures to individual chats
  - Dedup service rejects duplicate messages under concurrent submission
  - Message queue remains consistent when processing fails mid-flight
  - Recovery from stale pending messages works after partial failures
  - No cross-chat data leakage even when some chats fail

The LLM is mocked at the ``AsyncOpenAI`` transport level so that
``LLMClient`` goes through its full path. All other components —
Database, Memory, RoutingEngine, SkillRegistry, MessageQueue, Bot —
are real instances operating on ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot, BotConfig
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig
from src.core.dedup import DeduplicationService
from src.db import Database
from src.llm import LLMClient
from src.memory import Memory
from src.message_queue import MessageQueue
from src.routing import RoutingEngine, RoutingRule
from src.skills import SkillRegistry

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

NUM_CHAOS_CHATS = 12


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
            id="chaos-test-catch-all",
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
) -> tuple[Bot, Database, Memory, MessageQueue | None, DeduplicationService]:
    """Wire up a full Bot with real components and a mocked LLM transport."""
    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    routing = _make_routing_engine(workspace)
    skills = SkillRegistry()
    queue: MessageQueue | None = None
    if with_queue:
        queue = MessageQueue(str(workspace / ".data"))

    llm = LLMClient(config.llm)
    dedup = DeduplicationService(db=db)

    bot_config = BotConfig(
        max_tool_iterations=config.llm.max_tool_iterations,
        memory_max_history=config.memory_max_history,
        system_prompt_prefix=config.llm.system_prompt_prefix,
        stream_response=config.llm.stream_response,
    )

    bot = Bot(
        config=bot_config,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        message_queue=queue,
        instructions_dir=str(workspace / "instructions"),
        dedup=dedup,
    )
    return bot, db, memory, queue, dedup


def _make_message(chat_id: str, message_id: str, text: str) -> IncomingMessage:
    """Create an IncomingMessage for testing."""
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=f"user-{chat_id}",
        sender_name=f"User-{chat_id}",
        text=text,
        timestamp=time.time(),
        acl_passed=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Concurrent processing with forced LLM failures
# ─────────────────────────────────────────────────────────────────────────────


class TestChaosConcurrentWithFailures:
    """
    Fire messages from 10+ chats concurrently where some chats experience
    LLM failures.  Verify that:
      - Successful chats complete normally and persist both user + assistant messages
      - Failed chats don't corrupt other chats' data
      - Queue has zero pending for succeeded chats and retains pending for failed ones
    """

    @pytest.mark.asyncio
    async def test_partial_llm_failures_isolated_per_chat(self, tmp_path: Path) -> None:
        """
        12 chats: even-indexed chats succeed, odd-indexed chats get LLM errors.

        After all tasks settle:
          - Each even chat has 2 DB rows (user + assistant)
          - Each odd chat has at most 1 row (user only, or none)
          - No cross-chat leakage
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        chat_ids = [f"chat-chaos-{i:03d}" for i in range(NUM_CHAOS_CHATS)]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                # Determine which chat by looking at the user message
                for cid in chat_ids:
                    if cid in last_user_msg:
                        idx = chat_ids.index(cid)
                        if idx % 2 == 1:
                            raise RuntimeError(f"LLM failure for {cid}")
                        return _make_text_response(f"OK-{cid}")

                return _make_text_response("OK-unknown")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            messages = [
                _make_message(cid, f"msg-fail-{cid}-001", f"Hello from {cid}") for cid in chat_ids
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages],
                return_exceptions=True,
            )

        # Even chats succeed, odd chats raise
        for i, (cid, result) in enumerate(zip(chat_ids, results)):
            if i % 2 == 0:
                assert not isinstance(result, Exception), (
                    f"Even chat {cid} should succeed, got exception: {result}"
                )
                assert result is not None, f"Even chat {cid} returned None"
            else:
                # Odd chats either raise or return None
                assert isinstance(result, (Exception, type(None))), (
                    f"Odd chat {cid} should fail, got: {result}"
                )

        # Verify DB isolation
        for i, cid in enumerate(chat_ids):
            rows = await db.get_recent_messages(cid, limit=50)
            if i % 2 == 0:
                # Successful chat: user + assistant
                assert len(rows) == 2, (
                    f"Successful chat {cid} should have 2 messages, got {len(rows)}"
                )
            else:
                # Failed chat: only the user message was saved (or nothing if
                # the save happened before the LLM call and the error propagated)
                assert len(rows) <= 1, f"Failed chat {cid} should have ≤1 messages, got {len(rows)}"

        await db.close()

    @pytest.mark.asyncio
    async def test_queue_retains_pending_on_failure(self, tmp_path: Path) -> None:
        """
        With a message queue attached, enqueue 10 messages from different
        chats where 3 chats' LLM calls fail.

        After all tasks settle:
          - Queue should have exactly 3 pending messages (the failed ones)
          - Succeeded chats have completed queue entries
          - No queue corruption
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 10
        fail_indices = {2, 5, 8}
        chat_ids = [f"chat-queue-chaos-{i:03d}" for i in range(num_chats)]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                for idx, cid in enumerate(chat_ids):
                    if cid in last_user_msg:
                        if idx in fail_indices:
                            raise ConnectionError(f"LLM unreachable for {cid}")
                        return _make_text_response(f"Done-{cid}")

                return _make_text_response("Done")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            messages = [
                _make_message(cid, f"msg-qc-{cid}-001", f"Queue chaos from {cid}")
                for cid in chat_ids
            ]

            await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages],
                return_exceptions=True,
            )

        # Queue should have pending entries for the 3 failed chats
        pending_count = await queue.get_pending_count()
        assert pending_count == len(fail_indices), (
            f"Expected {len(fail_indices)} pending messages, got {pending_count}"
        )

        # Verify the pending messages are exactly the failed ones
        pending_ids: set[str] = set()
        for idx in range(num_chats):
            cid = chat_ids[idx]
            pending_for_chat = await queue.get_pending_for_chat(cid)
            for pm in pending_for_chat:
                pending_ids.add(pm.message_id)

        expected_pending = {f"msg-qc-chat-queue-chaos-{i:03d}-001" for i in fail_indices}
        assert pending_ids == expected_pending, (
            f"Pending IDs mismatch: expected {expected_pending}, got {pending_ids}"
        )

        # Successful chats should have DB entries
        for idx in range(num_chats):
            if idx not in fail_indices:
                rows = await db.get_recent_messages(chat_ids[idx], limit=50)
                assert len(rows) == 2, (
                    f"Successful chat {chat_ids[idx]} should have 2 messages, got {len(rows)}"
                )

        await queue.close()
        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Dedup under concurrent duplicate submissions
# ─────────────────────────────────────────────────────────────────────────────


class TestChaosDedupConcurrency:
    """
    Verify that the dedup service correctly rejects duplicate messages
    when the same message_id is submitted concurrently from multiple
    coroutines.
    """

    @pytest.mark.asyncio
    async def test_duplicate_message_ids_rejected_after_persist(self, tmp_path: Path) -> None:
        """
        Submit one message, let it complete and persist, then submit 4
        duplicates with the same message_id concurrently.

        Dedup uses ``db.message_exists()`` which is an async check against
        the persisted message-ID index.  Concurrent *first-time* submissions
        of the same ID all pass because none are saved yet — this is a race
        condition by design.  The guarantee is that *after* a message is
        persisted, re-deliveries are rejected.

        This test verifies the re-delivery guarantee under concurrent load:
        the original is processed once and all subsequent duplicates are
        rejected regardless of which chat they arrive on.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_duplicates = 4
        shared_msg_id = "msg-shared-duplicate-001"

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Dedup response")
            )

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            # Phase 1: Submit the original message and wait for completion
            original = _make_message(
                "chat-dedup-chaos-orig",
                shared_msg_id,
                "Original message",
            )
            result_orig = await bot.handle_message(original)
            assert result_orig is not None, "Original message should succeed"

            # Phase 2: Submit duplicates concurrently (same message_id, different chats)
            dup_messages = [
                _make_message(
                    f"chat-dedup-chaos-dup-{i:03d}",
                    shared_msg_id,
                    f"Duplicate {i}",
                )
                for i in range(num_duplicates)
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in dup_messages],
                return_exceptions=True,
            )

        # Original should have succeeded
        assert result_orig is not None

        # All duplicates should be rejected by dedup (message_id is persisted)
        for i, result in enumerate(results):
            assert result is None, (
                f"Duplicate {i} should have been rejected by dedup, got: {result}"
            )

        # Verify the original chat has the correct messages
        rows = await db.get_recent_messages("chat-dedup-chaos-orig", limit=50)
        assert len(rows) == 2, (
            f"Original chat should have 2 messages (user + assistant), got {len(rows)}"
        )

        # Verify duplicate chats have NO messages (rejected before processing)
        for i in range(num_duplicates):
            cid = f"chat-dedup-chaos-dup-{i:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            assert len(rows) == 0, f"Duplicate chat {cid} should have 0 messages, got {len(rows)}"

        await db.close()

    @pytest.mark.asyncio
    async def test_unique_then_duplicate_concurrent_rejection(self, tmp_path: Path) -> None:
        """
        5 chats send unique messages concurrently (all succeed), then
        5 duplicate messages reusing the same message_ids are submitted
        concurrently.

        After the first batch is persisted, dedup should reject all
        re-deliveries — even when they arrive on different chats.

        Verifies:
          - All 5 unique messages succeed
          - All 5 duplicates are rejected by dedup
          - Each original chat has correct DB state (2 messages)
          - Duplicate chats have 0 messages
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_unique = 5

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                await asyncio.sleep(0.01)
                return _make_text_response("Response")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            # Phase 1: Send unique messages concurrently
            unique_messages = [
                _make_message(
                    f"chat-inter-{i:03d}",
                    f"msg-unique-{i:03d}",
                    f"Unique message {i}",
                )
                for i in range(num_unique)
            ]

            results1 = await asyncio.gather(
                *[bot.handle_message(msg) for msg in unique_messages],
                return_exceptions=True,
            )

        # All unique messages should succeed
        successful = [r for r in results1 if isinstance(r, str) and r is not None]
        assert len(successful) == num_unique, (
            f"Expected {num_unique} successful unique messages, "
            f"got {len(successful)}. Results: {results1}"
        )

        # Phase 2: Submit duplicates (same message_ids, different chats)
        dup_messages = [
            _make_message(
                f"chat-inter-dup-{i:03d}",
                f"msg-unique-{i:03d}",  # Same message_id as unique
                f"Dup of message {i}",
            )
            for i in range(num_unique)
        ]

        # Shuffle to increase interleaving
        random.shuffle(dup_messages)

        results2 = await asyncio.gather(
            *[bot.handle_message(msg) for msg in dup_messages],
            return_exceptions=True,
        )

        # All duplicates should be rejected by dedup
        rejected = [r for r in results2 if r is None]
        assert len(rejected) == num_unique, (
            f"Expected {num_unique} rejected duplicates, got {len(rejected)}. Results: {results2}"
        )

        # Verify each unique chat has correct DB state
        for i in range(num_unique):
            cid = f"chat-inter-{i:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            assert len(rows) == 2, (
                f"Chat {cid} should have 2 messages (user + assistant), got {len(rows)}"
            )

        # Verify duplicate chats have no messages
        for i in range(num_unique):
            cid = f"chat-inter-dup-{i:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            assert len(rows) == 0, f"Duplicate chat {cid} should have 0 messages, got {len(rows)}"

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Interleaved messages across 10+ chats with forced failures
# ─────────────────────────────────────────────────────────────────────────────


class TestChaosInterleavedMessages:
    """
    Send multiple messages per chat concurrently, where some messages
    in each chat fail randomly.  Verify that per-chat locks serialize
    messages correctly and that the final DB state is consistent.
    """

    @pytest.mark.asyncio
    async def test_interleaved_messages_with_random_failures(self, tmp_path: Path) -> None:
        """
        10 chats, each sending 3 messages concurrently.
        Random ~30% of LLM calls fail with RuntimeError.

        After all messages settle:
          - Each chat's DB contains only valid (user + assistant) pairs
          - No cross-chat leakage
          - Total assistant messages ≤ total user messages (some failed)
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 10
        msgs_per_chat = 3
        # Deterministic seed for reproducibility
        rng = random.Random(42)
        fail_set: set[tuple[int, int]] = set()

        # Pre-determine which calls will fail (~30%)
        for ci in range(num_chats):
            for mi in range(msgs_per_chat):
                if rng.random() < 0.3:
                    fail_set.add((ci, mi))

        # Track which LLM calls were made to detect per-chat serialization
        concurrent_per_chat: dict[str, int] = {}
        max_concurrent_per_chat: dict[str, int] = {}
        chat_lock = asyncio.Lock()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                # Identify chat from message content
                chat_id = "unknown"
                for ci in range(num_chats):
                    cid = f"chat-interleaved-{ci:03d}"
                    if cid in last_user_msg:
                        chat_id = cid
                        # Check if this call should fail
                        for mi in range(msgs_per_chat):
                            if f"msg-{mi}" in last_user_msg and (ci, mi) in fail_set:
                                raise RuntimeError(f"Chaos failure for {cid} msg {mi}")
                        break

                # Track concurrency
                async with chat_lock:
                    count = concurrent_per_chat.get(chat_id, 0) + 1
                    concurrent_per_chat[chat_id] = count
                    if count > max_concurrent_per_chat.get(chat_id, 0):
                        max_concurrent_per_chat[chat_id] = count

                await asyncio.sleep(0.02)  # Simulate latency

                async with chat_lock:
                    concurrent_per_chat[chat_id] = concurrent_per_chat.get(chat_id, 1) - 1

                return _make_text_response(f"OK-{chat_id}")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            # Build all messages
            all_tasks = []
            for ci in range(num_chats):
                cid = f"chat-interleaved-{ci:03d}"
                for mi in range(msgs_per_chat):
                    msg = _make_message(
                        cid,
                        f"msg-inter-{ci:03d}-{mi:03d}",
                        f"Interleaved msg-{mi} from {cid}",
                    )
                    all_tasks.append(bot.handle_message(msg))

            results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Count successes/failures
        successes = [r for r in results if isinstance(r, str) and r is not None]
        failures = [r for r in results if isinstance(r, Exception) or r is None]
        total_expected = num_chats * msgs_per_chat
        assert len(successes) + len(failures) == total_expected

        # Per-chat lock should serialize: max concurrent per chat = 1
        for cid, max_c in max_concurrent_per_chat.items():
            assert max_c <= 1, f"Per-chat lock violated for {cid}: max concurrent = {max_c}"

        # Verify DB consistency for each chat.
        # User messages are saved BEFORE the LLM call, so even failed messages
        # persist a user row.  Successful messages have user + assistant.
        for ci in range(num_chats):
            cid = f"chat-interleaved-{ci:03d}"
            rows = await db.get_recent_messages(cid, limit=50)

            # Count succeeded messages for this chat
            succeeded_for_chat = 0
            for mi in range(msgs_per_chat):
                if (ci, mi) not in fail_set:
                    succeeded_for_chat += 1

            # user rows = msgs_per_chat (always saved)
            # assistant rows = succeeded_for_chat (only if LLM succeeded)
            expected_rows = msgs_per_chat + succeeded_for_chat
            assert len(rows) == expected_rows, (
                f"Chat {cid}: expected {expected_rows} rows "
                f"({msgs_per_chat} user + {succeeded_for_chat} assistant), "
                f"got {len(rows)}"
            )

            # Verify no cross-chat leakage
            for row in rows:
                if row["role"] == "user":
                    assert cid in row["content"], f"Cross-chat leak in {cid}: {row['content']}"

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Queue recovery under concurrent stale recovery
# ─────────────────────────────────────────────────────────────────────────────


class TestChaosQueueRecovery:
    """
    Verify that queue recovery (recover_stale) works correctly when
    messages fail and are later recovered, even under concurrent load.
    """

    @pytest.mark.asyncio
    async def test_stale_recovery_after_forced_failures(self, tmp_path: Path) -> None:
        """
        1. Send 10 messages with queue enabled; 3 fail.
        2. Those 3 stay in queue as pending.
        3. Call recover_stale() with timeout=0 to force recovery.
        4. Verify queue empties and messages are reprocessed.

        This simulates a crash-recovery scenario under concurrent load.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 10
        fail_indices = {1, 4, 7}
        chat_ids = [f"chat-recover-{i:03d}" for i in range(num_chats)]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            # Phase 1: Some calls fail
            async def _failing_create(*args: Any, **kwargs: Any) -> MagicMock:
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                for idx, cid in enumerate(chat_ids):
                    if cid in last_user_msg and idx in fail_indices:
                        raise ConnectionError(f"Transient failure for {cid}")
                return _make_text_response("Phase1 OK")

            mock_client.chat.completions.create = _failing_create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            messages = [
                _make_message(cid, f"msg-recover-{cid}-001", f"Recover test {cid}")
                for cid in chat_ids
            ]

            # Phase 1: Send all messages, some fail
            await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages],
                return_exceptions=True,
            )

        # Verify 3 pending messages in queue
        pending = await queue.get_pending_count()
        assert pending == len(fail_indices), f"Expected {len(fail_indices)} pending, got {pending}"

        # Phase 2: Make stale by setting updated_at in the past
        # We use timeout=0 with recover_stale to force recovery
        stale_messages = await queue.recover_stale(timeout_seconds=0)
        assert len(stale_messages) == len(fail_indices), (
            f"Expected {len(fail_indices)} stale messages, got {len(stale_messages)}"
        )

        # Queue should be empty now (stale messages were removed)
        pending_after_recover = await queue.get_pending_count()
        assert pending_after_recover == 0, (
            f"Queue should be empty after recovery, got {pending_after_recover}"
        )

        # Phase 3: Reprocess the stale messages (now LLM succeeds)
        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Recovery OK")
            )

            # Re-create bot with working LLM
            bot2, db2, memory2, queue2, _dedup2 = _make_bot(workspace, config, with_queue=True)
            await db2.connect()
            assert queue2 is not None
            await queue2.connect()

            for stale_msg in stale_messages:
                incoming = IncomingMessage(
                    message_id=stale_msg.message_id,
                    chat_id=stale_msg.chat_id,
                    sender_id=stale_msg.sender_id or "",
                    sender_name=stale_msg.sender_name or "",
                    text=stale_msg.text,
                    timestamp=stale_msg.created_at,
                    acl_passed=True,
                )
                await bot2.handle_message(incoming)

        # Verify all 10 chats now have messages
        for idx, cid in enumerate(chat_ids):
            rows = await db2.get_recent_messages(cid, limit=50)
            # Phase 1 saved the user message for failed chats,
            # Phase 3 reprocessed them saving user + assistant
            assert len(rows) >= 2, (
                f"Chat {cid} should have ≥2 messages after recovery, got {len(rows)}"
            )

        await queue.close()
        await db.close()
        await queue2.close()
        await db2.close()

    @pytest.mark.asyncio
    async def test_concurrent_enqueue_and_complete_integrity(self, tmp_path: Path) -> None:
        """
        Directly exercise the queue's enqueue/complete under high concurrency
        without going through the Bot, to stress-test the queue's internal
        lock.

        - Enqueue 20 messages from 10 different chats
        - Immediately complete them all concurrently
        - Verify queue ends empty
        """
        data_dir = tmp_path / ".data"
        queue = MessageQueue(str(data_dir))
        await queue.connect()

        num_messages = 20

        # Enqueue all
        incoming_msgs = [
            _make_message(
                f"chat-qdirect-{i % 10:03d}",
                f"msg-qdirect-{i:04d}",
                f"Direct queue test {i}",
            )
            for i in range(num_messages)
        ]

        # Enqueue concurrently
        await asyncio.gather(*[queue.enqueue(msg) for msg in incoming_msgs])

        pending = await queue.get_pending_count()
        assert pending == num_messages, (
            f"Expected {num_messages} pending after enqueue, got {pending}"
        )

        # Complete all concurrently
        message_ids = [f"msg-qdirect-{i:04d}" for i in range(num_messages)]
        results = await asyncio.gather(*[queue.complete(mid) for mid in message_ids])

        assert all(r is True for r in results), f"Some completions failed: {results}"

        pending_after = await queue.get_pending_count()
        assert pending_after == 0, (
            f"Queue should be empty after completing all, got {pending_after}"
        )

        await queue.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Mixed success/failure with full invariant check
# ─────────────────────────────────────────────────────────────────────────────


class TestChaosMixedSuccessFailureInvariants:
    """
    Full-system chaos test: fire many messages with random failures
    and verify ALL system invariants hold.
    """

    @pytest.mark.asyncio
    async def test_full_pipeline_chaos_invariants(self, tmp_path: Path) -> None:
        """
        15 chats, each sends 2 messages concurrently.
        ~20% of LLM calls randomly fail.
        Queue is enabled.

        After all tasks settle, verify:
          1. Per-chat lock: no concurrent LLM calls for same chat
          2. Queue: pending count matches number of failed messages
          3. DB: each chat has correct message count (2× succeeded msgs)
          4. Dedup: no duplicate processing
          5. No cross-chat data leakage
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        num_chats = 15
        msgs_per_chat = 2
        rng = random.Random(123)
        fail_set: set[tuple[int, int]] = set()

        for ci in range(num_chats):
            for mi in range(msgs_per_chat):
                if rng.random() < 0.2:
                    fail_set.add((ci, mi))

        # Concurrency tracking
        concurrent_counts: dict[str, int] = {}
        max_concurrent: dict[str, int] = {}
        track_lock = asyncio.Lock()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _chaos_create(*args: Any, **kwargs: Any) -> MagicMock:
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                chat_id = "unknown"
                msg_idx = -1
                for ci in range(num_chats):
                    cid = f"chat-full-{ci:03d}"
                    if cid in last_user_msg:
                        chat_id = cid
                        for mi in range(msgs_per_chat):
                            if f"fullmsg-{mi}" in last_user_msg.lower():
                                msg_idx = mi
                                break
                        break

                # Track concurrency
                async with track_lock:
                    c = concurrent_counts.get(chat_id, 0) + 1
                    concurrent_counts[chat_id] = c
                    if c > max_concurrent.get(chat_id, 0):
                        max_concurrent[chat_id] = c

                await asyncio.sleep(0.03)

                async with track_lock:
                    concurrent_counts[chat_id] = concurrent_counts.get(chat_id, 1) - 1

                # Check if this should fail
                for ci in range(num_chats):
                    cid = f"chat-full-{ci:03d}"
                    if cid == chat_id:
                        if (ci, msg_idx) in fail_set:
                            raise RuntimeError(f"Chaos: {cid} msg {msg_idx}")
                        break

                return _make_text_response(f"Full-{chat_id}")

            mock_client.chat.completions.create = _chaos_create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            all_tasks = []
            for ci in range(num_chats):
                cid = f"chat-full-{ci:03d}"
                for mi in range(msgs_per_chat):
                    msg = _make_message(
                        cid,
                        f"msg-full-{ci:03d}-{mi:03d}",
                        f"Fullmsg-{mi} chaos from {cid}",
                    )
                    all_tasks.append(bot.handle_message(msg))

            results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Invariant 1: Per-chat lock serialization
        for cid, mc in max_concurrent.items():
            assert mc <= 1, f"INV1 FAIL: {cid} had max concurrent LLM calls = {mc}"

        # Count expected failures
        total_failures = 0
        per_chat_failures: dict[int, int] = {}
        for ci in range(num_chats):
            chat_fails = 0
            for mi in range(msgs_per_chat):
                if (ci, mi) in fail_set:
                    chat_fails += 1
            per_chat_failures[ci] = chat_fails
            total_failures += chat_fails

        # Invariant 2: Queue pending = failed messages
        pending = await queue.get_pending_count()
        assert pending == total_failures, (
            f"INV2 FAIL: Expected {total_failures} pending in queue, got {pending}"
        )

        # Invariant 3: DB consistency per chat.
        # User messages are always saved (before LLM call), assistant messages
        # only for succeeded LLM calls.
        for ci in range(num_chats):
            cid = f"chat-full-{ci:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            succeeded = msgs_per_chat - per_chat_failures[ci]
            expected = msgs_per_chat + succeeded  # user per msg + assistant per success
            assert len(rows) == expected, (
                f"INV3 FAIL: Chat {cid} expected {expected} rows, got {len(rows)}"
            )

            # Invariant 4: No duplicate messages
            message_ids = [r.get("message_id") for r in rows if "message_id" in r]
            assert len(message_ids) == len(set(message_ids)), (
                f"INV4 FAIL: Duplicate messages in chat {cid}: {message_ids}"
            )

        # Invariant 5: No cross-chat leakage
        for ci in range(num_chats):
            cid = f"chat-full-{ci:03d}"
            rows = await db.get_recent_messages(cid, limit=50)
            for row in rows:
                content = row.get("content", "")
                if row["role"] == "user" and content:
                    assert cid in content, f"INV5 FAIL: Cross-chat leak in {cid}: {content}"

        await queue.close()
        await db.close()
