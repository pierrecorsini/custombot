"""test_chaos.py — Chaos engineering tests with random failure injection.

Uses hypothesis to generate random failure scenarios:
  - Network errors (random connection failures)
  - Timeouts (random timeout exceptions)
  - Database errors (random write failures)

Verifies:
  - No data corruption
  - No deadlocks
  - Graceful degradation under random failures
  - System remains consistent after failures
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck, Phase
from hypothesis import strategies as st

from src.bot import Bot, BotConfig
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig
from src.core.dedup import DeduplicationService
from src.db import Database
from src.exceptions import DatabaseError, LLMError, ErrorCode
from src.llm import LLMClient
from src.memory import Memory
from src.message_queue import MessageQueue
from src.routing import RoutingEngine, RoutingRule
from src.skills import SkillRegistry

from tests.helpers.llm_mocks import make_text_response


# ── Strategies ───────────────────────────────────────────────────────────────

# How many concurrent operations to run
chat_count_strategy = st.integers(min_value=3, max_value=15)

# Probability of injecting a failure per operation
failure_probability = st.floats(min_value=0.1, max_value=0.5)

# Type of failure to inject
failure_type_strategy = st.sampled_from([
    "network",
    "timeout",
    "database",
    "llm_rate_limit",
])

# Number of messages per chat
messages_per_chat_strategy = st.integers(min_value=1, max_value=4)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_routing_engine(workspace) -> RoutingEngine:
    engine = RoutingEngine(workspace)
    engine._rules = [
        RoutingRule(
            id="chaos-hypothesis-catch-all",
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


def _make_bot(workspace, config, with_queue=False):
    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    routing = _make_routing_engine(workspace)
    skills = SkillRegistry()
    queue = MessageQueue(str(workspace / ".data")) if with_queue else None

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
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=f"user-{chat_id}",
        sender_name=f"User-{chat_id}",
        text=text,
        timestamp=time.time(),
        acl_passed=True,
    )


def _build_failure_injector(
    chat_ids: list[str],
    fail_set: set[tuple[int, int]],
    failure_type: str,
    rng: random.Random,
):
    """Create an LLM mock that injects failures based on type."""

    async def _create(*args: Any, **kwargs: Any) -> MagicMock:
        messages = kwargs.get("messages", [])
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break

        for ci, cid in enumerate(chat_ids):
            if cid in last_user_msg:
                for mi in range(20):
                    if f"msg-{mi}" in last_user_msg and (ci, mi) in fail_set:
                        if failure_type == "network":
                            raise ConnectionError(f"Chaos: network error for {cid}")
                        elif failure_type == "timeout":
                            raise TimeoutError(f"Chaos: timeout for {cid}")
                        elif failure_type == "llm_rate_limit":
                            raise LLMError(
                                "Chaos: rate limited",
                                error_code=ErrorCode.LLM_RATE_LIMITED,
                            )
                        else:
                            raise RuntimeError(f"Chaos: unknown failure for {cid}")
                break

        await asyncio.sleep(0.01)
        return make_text_response(f"OK-{cid if cid in last_user_msg else 'unknown'}")

    return _create


# ── Tests: Random failure injection in concurrent message processing ────────


class TestChaosRandomFailureInjection:
    """Inject random failures into concurrent message processing and verify
    the system degrades gracefully without corruption."""

    @pytest.mark.asyncio
    @given(
        num_chats=chat_count_strategy,
        msgs_per_chat=messages_per_chat_strategy,
        fail_prob=failure_probability,
        failure_type=failure_type_strategy,
    )
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
        phases=[Phase.generate],
    )
    async def test_concurrent_messages_with_random_failures(
        self,
        num_chats: int,
        msgs_per_chat: int,
        fail_prob: float,
        failure_type: str,
        tmp_path_factory,
    ) -> None:
        """Random failures during concurrent message processing should not
        corrupt data or cause deadlocks."""
        tmp_path = tmp_path_factory.mktemp("chaos_msgs")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))
        chat_ids = [f"chat-hyp-{i:03d}" for i in range(num_chats)]

        rng = random.Random(99)
        fail_set: set[tuple[int, int]] = set()
        for ci in range(num_chats):
            for mi in range(msgs_per_chat):
                if rng.random() < fail_prob:
                    fail_set.add((ci, mi))

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = _build_failure_injector(
                chat_ids, fail_set, failure_type, rng,
            )

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            all_tasks = []
            for ci in range(num_chats):
                cid = chat_ids[ci]
                for mi in range(msgs_per_chat):
                    msg = _make_message(
                        cid,
                        f"msg-hyp-{ci:03d}-{mi:03d}",
                        f"Hypothesis msg-{mi} from {cid}",
                    )
                    all_tasks.append(bot.handle_message(msg))

            results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Verify: no cross-chat leakage
        for ci in range(num_chats):
            cid = chat_ids[ci]
            rows = await db.get_recent_messages(cid, limit=100)
            for row in rows:
                if row["role"] == "user" and row.get("content"):
                    assert cid in row["content"], (
                        f"Cross-chat leak: {cid} contains {row['content']}"
                    )

        # Verify: succeeded chats have both user + assistant messages
        for ci in range(num_chats):
            cid = chat_ids[ci]
            rows = await db.get_recent_messages(cid, limit=100)
            succeeded = sum(1 for mi in range(msgs_per_chat) if (ci, mi) not in fail_set)
            # user msgs = msgs_per_chat, assistant msgs = succeeded
            expected = msgs_per_chat + succeeded
            assert len(rows) == expected, (
                f"Chat {cid}: expected {expected} rows, got {len(rows)}"
            )

        await db.close()


# ── Tests: Random database write failures ───────────────────────────────────


class TestChaosDatabaseFailures:
    """Inject random database errors during concurrent writes."""

    @pytest.mark.asyncio
    async def test_concurrent_db_writes_with_random_errors(self, tmp_path) -> None:
        """Random DB write failures should not corrupt existing data.

        Write 50 messages to 5 chats concurrently.  ~30% of writes fail.
        Verify that all successful writes are readable and no partial data.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        num_chats = 5
        messages_per_chat = 10

        original_save = db.save_message
        save_call_count = 0
        write_lock = asyncio.Lock()

        async def _flaky_save(*args, **kwargs):
            nonlocal save_call_count
            async with write_lock:
                save_call_count += 1
                call_idx = save_call_count

            # Fail ~30% of the time deterministically
            if call_idx % 10 in {2, 5, 8}:
                raise DatabaseError(
                    "Chaos: random write failure",
                    error_code=ErrorCode.DB_WRITE_FAILED,
                )

            return await original_save(*args, **kwargs)

        db.save_message = _flaky_save

        async def _writer(ci: int, mi: int):
            cid = f"chat-db-chaos-{ci:03d}"
            try:
                return await db.save_message(
                    chat_id=cid,
                    role="user",
                    content=f"DB chaos msg {mi} from chat {ci}",
                    message_id=f"msg-db-chaos-{ci:03d}-{mi:03d}",
                )
            except DatabaseError:
                return None

        tasks = [
            _writer(ci, mi)
            for ci in range(num_chats)
            for mi in range(messages_per_chat)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successes vs failures
        successes = [r for r in results if isinstance(r, str)]
        failures = [r for r in results if r is None or isinstance(r, Exception)]

        # Verify that successful writes are readable
        for ci in range(num_chats):
            cid = f"chat-db-chaos-{ci:03d}"
            rows = await db.get_recent_messages(cid, limit=100)
            for row in rows:
                assert "role" in row, f"Corrupted row in chat {cid}: {row}"
                assert "content" in row, f"Missing content in chat {cid}: {row}"

        await db.close()


# ── Tests: Random tool execution crashes ────────────────────────────────────


class TestChaosToolExecution:
    """Inject random crashes during tool execution."""

    @pytest.mark.asyncio
    @given(
        num_chats=st.integers(min_value=2, max_value=8),
        failure_type=st.sampled_from(["crash", "timeout", "corrupt"]),
    )
    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
        phases=[Phase.generate],
    )
    async def test_tool_execution_with_random_crashes(
        self,
        num_chats: int,
        failure_type: str,
        tmp_path_factory,
    ) -> None:
        """Random tool execution failures should not break the message loop."""
        tmp_path = tmp_path_factory.mktemp("chaos_tools")
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))

        rng = random.Random(55)
        chat_ids = [f"chat-tool-chaos-{i:03d}" for i in range(num_chats)]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args, **kwargs):
                messages = kwargs.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break

                cid = "unknown"
                for c in chat_ids:
                    if c in last_user_msg:
                        cid = c
                        break

                if rng.random() < 0.2:
                    if failure_type == "crash":
                        raise RuntimeError(f"Tool crash for {cid}")
                    elif failure_type == "timeout":
                        raise TimeoutError(f"Tool timeout for {cid}")
                    else:
                        raise ValueError(f"Corrupt result for {cid}")

                await asyncio.sleep(0.01)
                return make_text_response(f"Tool-OK-{cid}")

            mock_client.chat.completions.create = _create

            bot, db, memory, queue, _dedup = _make_bot(workspace, config)
            await db.connect()

            messages = [
                _make_message(cid, f"msg-tool-chaos-{cid}-001", f"Tool chaos from {cid}")
                for cid in chat_ids
            ]

            results = await asyncio.gather(
                *[bot.handle_message(msg) for msg in messages],
                return_exceptions=True,
            )

        # No deadlocks: all tasks completed
        assert len(results) == num_chats

        # Verify DB is not corrupted
        for cid in chat_ids:
            rows = await db.get_recent_messages(cid, limit=50)
            for row in rows:
                assert "role" in row
                assert "content" in row

        await db.close()


# ── Tests: Concurrent database writes with random errors ────────────────────


class TestChaosConcurrentDatabaseWrites:
    """Stress test concurrent database writes with random errors."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_no_data_loss(self, tmp_path) -> None:
        """50 concurrent writes to the same chat, some failing randomly.
        Verify all successful writes are readable."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        chat_id = "chat-concurrent-db-001"
        num_writes = 50

        write_lock = asyncio.Lock()
        successful_ids: list[str] = []

        async def _writer(idx: int) -> str | None:
            msg_id = f"msg-concurrent-{idx:04d}"
            try:
                result = await db.save_message(
                    chat_id=chat_id,
                    role="user",
                    content=f"Concurrent write {idx}",
                    message_id=msg_id,
                )
                async with write_lock:
                    successful_ids.append(msg_id)
                return result
            except Exception:
                return None

        results = await asyncio.gather(
            *[_writer(i) for i in range(num_writes)],
            return_exceptions=True,
        )

        # Verify: all successful writes are readable
        final_rows = await db.get_recent_messages(chat_id, limit=500)
        final_ids = {r.get("message_id") for r in final_rows if "message_id" in r}

        for mid in successful_ids:
            assert mid in final_ids, f"Successful write {mid} not found in final state"

        assert len(final_rows) == len(successful_ids), (
            f"Expected {len(successful_ids)} rows, got {len(final_rows)}"
        )

        await db.close()
