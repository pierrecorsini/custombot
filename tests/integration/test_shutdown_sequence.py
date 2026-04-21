"""
test_shutdown_sequence.py — Integration test for perform_shutdown() ordered cleanup.

Exercises the 6-step graceful shutdown with real (in-memory) components
where possible:

  1. Stop accepting new messages (GracefulShutdown + channel)
  2. Wait for in-flight operations (real GracefulShutdown)
  3. Stop scheduler and health server
  4. Close channel connections
  5. Close project store, vector memory, message queue, and LLM client
  6. Close database (must be last)

Components that require external services (channel, scheduler, health_server)
are mocked. All storage backends (Database, MessageQueue, ProjectStore) and
the GracefulShutdown manager are real instances operating on tmp_path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db import Database
from src.lifecycle import perform_shutdown
from src.message_queue import MessageQueue
from src.project.store import ProjectStore
from src.shutdown import GracefulShutdown


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _setup_real_components(
    tmp_path: Path, *, shutdown_timeout: float = 5.0
) -> tuple[
    GracefulShutdown,
    Database,
    MessageQueue,
    ProjectStore,
]:
    """Create and connect real storage components on tmp_path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    shutdown = GracefulShutdown(timeout=shutdown_timeout)
    db = Database(str(data_dir))
    await db.connect()
    message_queue = MessageQueue(str(data_dir))
    await message_queue.connect()
    project_store = ProjectStore(db_path=str(data_dir / "projects.db"))
    project_store.connect()

    return shutdown, db, message_queue, project_store


def _make_mock_channel() -> MagicMock:
    """Create a mock channel with request_shutdown() and close()."""
    channel = MagicMock()
    channel.request_shutdown = MagicMock()
    channel.close = AsyncMock()
    return channel


def _make_mock_scheduler() -> MagicMock:
    """Create a mock scheduler with stop()."""
    scheduler = MagicMock()
    scheduler.stop = AsyncMock()
    return scheduler


def _make_mock_health_server() -> MagicMock:
    """Create a mock health server with stop()."""
    health_server = MagicMock()
    health_server.stop = AsyncMock()
    return health_server


def _make_mock_llm() -> MagicMock:
    """Create a mock LLM client with close()."""
    llm = MagicMock()
    llm.close = AsyncMock()
    return llm


# ─────────────────────────────────────────────────────────────────────────────
# Test: Ordering — all 6 steps execute in the correct sequence
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownOrdering:
    """Verify the 6 cleanup steps execute in the correct order."""

    @pytest.mark.asyncio
    async def test_all_six_steps_in_correct_order(self, tmp_path: Path) -> None:
        """
        All 6 steps execute and db.close() is the very last call.

        Uses real Database, MessageQueue, ProjectStore, and GracefulShutdown
        with wrapped close methods to track invocation order.
        """
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        health_server = _make_mock_health_server()
        llm = _make_mock_llm()

        call_order: list[str] = []

        # Step 1: wrap shutdown.request_shutdown
        original_rs = shutdown.request_shutdown
        def _tracked_rs():
            call_order.append("1a:shutdown.request_shutdown")
            original_rs()
        shutdown.request_shutdown = _tracked_rs

        # Step 1b: channel.request_shutdown
        channel.request_shutdown.side_effect = lambda: call_order.append("1b:channel.request_shutdown")

        # Step 3: scheduler.stop + health_server.stop
        scheduler.stop.side_effect = lambda: call_order.append("3a:scheduler.stop")
        health_server.stop.side_effect = lambda: call_order.append("3b:health_server.stop")

        # Step 4: channel.close
        channel.close.side_effect = lambda: call_order.append("4:channel.close")

        # Step 5: project_store.close (sync, called via to_thread)
        original_ps_close = project_store.close
        def _tracked_ps_close():
            call_order.append("5a:project_store.close")
            original_ps_close()
        project_store.close = _tracked_ps_close

        # Step 5: message_queue.close
        original_mq_close = message_queue.close
        async def _tracked_mq_close():
            call_order.append("5b:message_queue.close")
            await original_mq_close()
        message_queue.close = _tracked_mq_close

        # Step 5: llm.close
        llm.close.side_effect = lambda: call_order.append("5c:llm.close")

        # Step 6: db.close
        original_db_close = db.close
        async def _tracked_db_close():
            call_order.append("6:db.close")
            await original_db_close()
        db.close = _tracked_db_close

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=health_server,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        # All steps fired
        assert "1a:shutdown.request_shutdown" in call_order
        assert "1b:channel.request_shutdown" in call_order
        assert "3a:scheduler.stop" in call_order
        assert "3b:health_server.stop" in call_order
        assert "4:channel.close" in call_order
        assert "5a:project_store.close" in call_order
        assert "5b:message_queue.close" in call_order
        assert "5c:llm.close" in call_order
        assert "6:db.close" in call_order

        # DB must be the absolute last
        assert call_order[-1] == "6:db.close"

        # Phase ordering: 1 < 3 < 4 < 5 < 6
        idx_1 = call_order.index("1a:shutdown.request_shutdown")
        idx_3 = call_order.index("3a:scheduler.stop")
        idx_4 = call_order.index("4:channel.close")
        idx_5 = call_order.index("5c:llm.close")
        idx_6 = call_order.index("6:db.close")
        assert idx_1 < idx_3 < idx_4 < idx_5 < idx_6


# ─────────────────────────────────────────────────────────────────────────────
# Test: Failure tolerance — a failing step doesn't skip subsequent steps
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownFailureTolerance:
    """Verify a failing cleanup step doesn't prevent subsequent steps."""

    @pytest.mark.asyncio
    async def test_scheduler_failure_does_not_skip_channel_or_db(
        self, tmp_path: Path
    ) -> None:
        """
        If scheduler.stop() and health_server.stop() raise, steps 4-6 still run.
        """
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        health_server = _make_mock_health_server()
        llm = _make_mock_llm()

        closed: list[str] = []

        # Scheduler and health server FAIL
        scheduler.stop.side_effect = RuntimeError("scheduler boom")
        health_server.stop.side_effect = RuntimeError("health boom")

        # Track remaining steps
        channel.close.side_effect = lambda: closed.append("channel")
        llm.close.side_effect = lambda: closed.append("llm")

        original_mq_close = message_queue.close
        async def _tracked_mq_close():
            closed.append("message_queue")
            await original_mq_close()
        message_queue.close = _tracked_mq_close

        original_db_close = db.close
        async def _tracked_db_close():
            closed.append("db")
            await original_db_close()
        db.close = _tracked_db_close

        # Should NOT raise despite failures
        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=health_server,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        # All post-failure steps still executed
        assert "channel" in closed
        assert "message_queue" in closed
        assert "llm" in closed
        assert "db" in closed
        assert closed[-1] == "db"

    @pytest.mark.asyncio
    async def test_channel_close_failure_does_not_skip_storage_close(
        self, tmp_path: Path
    ) -> None:
        """If channel.close() raises, storage backends and DB still close."""
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        closed: list[str] = []

        channel.close.side_effect = RuntimeError("channel boom")
        llm.close.side_effect = lambda: closed.append("llm")

        original_mq_close = message_queue.close
        async def _tracked_mq_close():
            closed.append("message_queue")
            await original_mq_close()
        message_queue.close = _tracked_mq_close

        original_db_close = db.close
        async def _tracked_db_close():
            closed.append("db")
            await original_db_close()
        db.close = _tracked_db_close

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        assert "llm" in closed
        assert "message_queue" in closed
        assert "db" in closed

    @pytest.mark.asyncio
    async def test_db_close_failure_does_not_raise(self, tmp_path: Path) -> None:
        """If the final step (db.close) fails, perform_shutdown still returns."""
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        # Make db.close fail
        db.close = AsyncMock(side_effect=RuntimeError("db boom"))

        # Should NOT raise
        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_close_failure_does_not_block_db_close(
        self, tmp_path: Path
    ) -> None:
        """If llm.close() fails, db.close() still runs."""
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        llm.close.side_effect = RuntimeError("llm boom")

        db_closed = False
        original_db_close = db.close
        async def _tracked_db_close():
            nonlocal db_closed
            db_closed = True
            await original_db_close()
        db.close = _tracked_db_close

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        assert db_closed, "db.close() should run even if llm.close() fails"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Timeout — shutdown forces exit when in-flight ops don't complete
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownTimeout:
    """Verify shutdown timeout correctly forces exit."""

    @pytest.mark.asyncio
    async def test_timeout_forces_exit(self, tmp_path: Path) -> None:
        """
        With a stuck in-flight operation and short timeout, shutdown
        proceeds after the timeout instead of hanging forever.
        """
        shutdown, db, message_queue, project_store = await _setup_real_components(
            tmp_path, shutdown_timeout=0.3
        )
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        # Register an in-flight operation that never completes
        op_id = await shutdown.enter_operation("stuck-task")
        assert op_id is not None

        start = asyncio.get_event_loop().time()

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        elapsed = asyncio.get_event_loop().time() - start

        # Should not hang — must complete within reasonable time
        assert elapsed < 5.0, "Shutdown should not hang for more than a few seconds"

        # Subsequent steps still ran despite timeout
        channel.close.assert_awaited_once()
        llm.close.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Test: LLM client close is included in shutdown
# ─────────────────────────────────────────────────────────────────────────────


class TestLLMClientClose:
    """Verify LLM client close() is called during the shutdown sequence."""

    @pytest.mark.asyncio
    async def test_llm_close_called(self, tmp_path: Path) -> None:
        """LLM client close() should be awaited during step 5."""
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        llm.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_close_happens_before_db_close(self, tmp_path: Path) -> None:
        """LLM client close happens in step 5, before db.close in step 6."""
        shutdown, db, message_queue, project_store = await _setup_real_components(tmp_path)
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        call_order: list[str] = []

        llm.close.side_effect = lambda: call_order.append("llm.close")

        original_db_close = db.close
        async def _tracked_db_close():
            call_order.append("db.close")
            await original_db_close()
        db.close = _tracked_db_close

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        assert "llm.close" in call_order
        assert "db.close" in call_order
        assert call_order.index("llm.close") < call_order.index("db.close")


# ─────────────────────────────────────────────────────────────────────────────
# Test: End-to-end graceful shutdown with in-flight LLM call
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownWithInFlightLLMCall:
    """
    End-to-end tests for graceful shutdown during an active LLM call.

    Simulates the real production scenario where shutdown is requested
    while a message is being processed through the LLM.
    """

    @pytest.mark.asyncio
    async def test_in_flight_llm_call_completes_before_shutdown(
        self, tmp_path: Path
    ) -> None:
        """
        An in-flight LLM call that finishes before the shutdown timeout
        completes gracefully — the response is persisted and all cleanup
        steps run normally.
        """
        shutdown_timeout = 5.0
        llm_delay = 0.2  # LLM call completes well within timeout
        shutdown, db, message_queue, project_store = await _setup_real_components(
            tmp_path, shutdown_timeout=shutdown_timeout
        )
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        chat_id = "test@s.whatsapp.net"
        message_id = "msg-inflight-complete"

        # Simulate the real flow: enter operation, do slow work, exit operation
        async def _simulate_llm_message():
            op_id = await shutdown.enter_operation(f"llm-call-{chat_id}")
            assert op_id is not None
            try:
                # Simulate slow LLM call
                await asyncio.sleep(llm_delay)
                # Persist the response
                await db.save_message(
                    chat_id=chat_id,
                    role="user",
                    content="hello",
                    message_id=message_id,
                )
                await db.save_message(
                    chat_id=chat_id,
                    role="assistant",
                    content="Hello! How can I help?",
                    message_id=f"{message_id}-resp",
                )
            finally:
                await shutdown.exit_operation(op_id)

        # Start the simulated LLM call concurrently
        llm_task = asyncio.create_task(_simulate_llm_message())

        # Small delay so the LLM call is in-flight when shutdown starts
        await asyncio.sleep(0.05)

        # Shutdown should wait for the in-flight call to finish
        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        # The LLM task completed successfully
        assert llm_task.done()
        llm_task.result()  # raises if the task failed

        # Response was persisted before shutdown closed the DB
        messages = await db.get_recent_messages(chat_id, limit=10)
        assert len(messages) == 2
        assert messages[0]["content"] == "hello"
        assert messages[1]["content"] == "Hello! How can I help?"

    @pytest.mark.asyncio
    async def test_shutdown_timeout_forces_exit_with_slow_llm_call(
        self, tmp_path: Path
    ) -> None:
        """
        If the LLM call exceeds the shutdown timeout, shutdown forces exit.
        Subsequent cleanup steps still run.
        """
        shutdown_timeout = 0.3
        shutdown, db, message_queue, project_store = await _setup_real_components(
            tmp_path, shutdown_timeout=shutdown_timeout
        )
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        # Register an in-flight operation simulating a long LLM call
        op_id = await shutdown.enter_operation("slow-llm-call")
        assert op_id is not None

        start = asyncio.get_event_loop().time()

        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        elapsed = asyncio.get_event_loop().time() - start

        # Shutdown must not hang
        assert elapsed < 5.0

        # Cleanup still ran despite forced exit
        channel.close.assert_awaited_once()
        llm.close.assert_awaited_once()

        # Clean up the stuck operation so the test doesn't leak
        await shutdown.exit_operation(op_id)

    @pytest.mark.asyncio
    async def test_response_persisted_before_shutdown_closes_db(
        self, tmp_path: Path
    ) -> None:
        """
        A message response persisted during an in-flight operation is
        durable — it survives the shutdown sequence (DB close + reopen).
        """
        shutdown, db, message_queue, project_store = await _setup_real_components(
            tmp_path, shutdown_timeout=5.0
        )
        channel = _make_mock_channel()
        scheduler = _make_mock_scheduler()
        llm = _make_mock_llm()

        chat_id = "test@s.whatsapp.net"
        user_msg_id = "msg-persist-test"
        bot_msg_id = "msg-persist-test-resp"

        # Simulate a full message processing cycle:
        # 1. Enter operation (OperationTrackerMiddleware)
        # 2. Save user message
        # 3. "LLM call" (instant)
        # 4. Save bot response
        # 5. Exit operation
        op_id = await shutdown.enter_operation(f"message-{chat_id}")
        assert op_id is not None

        await db.save_message(
            chat_id=chat_id,
            role="user",
            content="What is 2+2?",
            message_id=user_msg_id,
        )
        await db.save_message(
            chat_id=chat_id,
            role="assistant",
            content="2+2 equals 4.",
            message_id=bot_msg_id,
        )
        await shutdown.exit_operation(op_id)

        # Now run full shutdown — DB gets closed
        await perform_shutdown(
            shutdown=shutdown,
            channel=channel,
            scheduler=scheduler,
            health_server=None,
            db=db,
            vector_memory=None,
            project_store=project_store,
            message_queue=message_queue,
            llm=llm,
            session_metrics={"uptime": 5.0},
            log=logging.getLogger("test"),
        )

        # Reopen the database and verify data survived the shutdown
        data_dir = tmp_path / "data"
        db2 = Database(str(data_dir))
        await db2.connect()

        messages = await db2.get_recent_messages(chat_id, limit=10)
        assert len(messages) == 2
        assert messages[0]["content"] == "What is 2+2?"
        assert messages[1]["content"] == "2+2 equals 4."

        await db2.close()
