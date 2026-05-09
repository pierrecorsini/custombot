"""
test_smoke.py — E2E smoke test for the full message pipeline.

Verifies that a message flows through the complete pipeline
(Bot + MessagePipeline + all middlewares + mock channel) and
produces a response.  Uses a mocked LLM to avoid real API calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.base import BaseChannel, ChannelType, IncomingMessage
from src.config import Config, LLMConfig
from src.core.message_pipeline import (
    PipelineDependencies,
    build_pipeline_from_config,
)
from tests.helpers.llm_mocks import make_text_response
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


class MockChannel(BaseChannel):
    """Lightweight mock channel that records sent messages."""

    def __init__(self) -> None:
        super().__init__()
        self._sent: list[tuple[str, str]] = []

    async def start(self, handler) -> None:
        self.mark_connected()

    async def _send_message(self, chat_id: str, text: str, *, skip_delays: bool = False) -> None:
        self._sent.append((chat_id, text))

    async def send_typing(self, chat_id: str) -> None:
        pass

    async def close(self) -> None:
        pass

    def request_shutdown(self) -> None:
        pass


async def _create_bot(tmp_path: Path):
    """Create a fully-wired Bot with mocked LLM."""
    from src.bot import Bot, BotConfig, BotDeps
    from src.core.dedup import DeduplicationService
    from src.db import Database
    from src.memory import Memory
    from src.routing import RoutingEngine, RoutingRule
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / ".data"))
    await db.connect()

    dedup = DeduplicationService(db=db)

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        ),
        skills_auto_load=False,
    )

    memory = Memory(str(workspace))
    skills = SkillRegistry()

    # Routing: catch-all rule
    instructions_dir = workspace / "instructions"
    instructions_dir.mkdir(exist_ok=True)
    routing = RoutingEngine(instructions_dir)
    routing._rules = [
        RoutingRule(
            id="smoke-catch-all",
            priority=100,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.agent.md",
            enabled=True,
        )
    ]
    # DB.connect() seeds instruction templates into workspace/instructions/,
    # which changes file mtimes.  Synchronise the engine's cache so it
    # doesn't reload from disk and overwrite our catch-all rule.
    routing._file_mtimes = routing._scan_file_mtimes()

    bot_config = BotConfig(
        max_tool_iterations=config.llm.max_tool_iterations,
        memory_max_history=config.memory_max_history,
        system_prompt_prefix=config.llm.system_prompt_prefix,
        stream_response=False,
    )

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(
            return_value=make_text_response("Smoke test response!")
        )

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

    bot = Bot(BotDeps(
        config=bot_config,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        dedup=dedup,
        instructions_dir=str(instructions_dir),
        rate_limiter=MagicMock(),
        tool_executor=AsyncMock(),
        context_assembler=AsyncMock(),
    ))

    return bot, db


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_message_flows_through_pipeline(tmp_path: Path):
    """
    E2E Smoke Test: Message flows through the full pipeline to a response.

    Arrange:
        - Create a Bot with mocked LLM
        - Build the full middleware pipeline
        - Create a mock channel that records sent messages

    Act:
        - Send an IncomingMessage through the pipeline

    Assert:
        - Pipeline completes without error
        - ctx.response contains the expected text
        - Channel.send_message was called with the response
    """
    from src.monitoring import SessionMetrics
    from src.shutdown import GracefulShutdown

    # ── Arrange ──
    bot, db = await _create_bot(tmp_path)
    channel = MockChannel()
    shutdown_mgr = GracefulShutdown(timeout=5.0)
    session_metrics = SessionMetrics()

    # Build the pipeline with all default middlewares
    deps = PipelineDependencies(
        shutdown_mgr=shutdown_mgr,
        session_metrics=session_metrics,
        bot=bot,
        channel=channel,
        verbose=False,
    )
    pipeline = build_pipeline_from_config(
        middleware_order=[],
        extra_middleware_paths=[],
        deps=deps,
    )

    msg = IncomingMessage(
        message_id="smoke-msg-001",
        chat_id="smoke-chat@test",
        sender_id="tester",
        sender_name="Smoke Tester",
        text="Hello from smoke test",
        timestamp=1000.0,
        channel_type=ChannelType.CLI,
        acl_passed=True,
    )

    # ── Act ──
    from src.core.message_pipeline import MessageContext

    ctx = MessageContext(msg=msg)
    # Patch log_message_flow to avoid UnicodeEncodeError on Windows (cp1252)
    # when Rich console prints the → arrow character.
    with patch("src.core.message_pipeline.log_message_flow"):
        await pipeline.execute(ctx)

    # ── Assert ──
    assert ctx.response is not None, "Pipeline should produce a response"
    assert "Smoke test response" in ctx.response, (
        f"Expected smoke response text, got: {ctx.response!r}"
    )
    assert len(channel._sent) >= 1, "Channel should have sent at least one message"

    sent_text = channel._sent[-1][1]
    assert "Smoke test response" in sent_text, (
        f"Channel should have sent the bot response, got: {sent_text!r}"
    )

    # Metrics should reflect the processed message
    assert session_metrics.to_dict()["messages_processed"] >= 1, (
        "Session metrics should track at least one message"
    )

    # Cleanup
    await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Application-level smoke test
# ─────────────────────────────────────────────────────────────────────────────


async def _create_app(tmp_path: Path):
    """Create an Application with manually-wired components for E2E testing.

    Bypasses ``Application._startup()`` (which creates real WhatsApp channels)
    by setting internal state directly, then builds the pipeline so that
    ``_on_message()`` works end-to-end.
    """
    from src.app import Application
    from src.builder import BotComponents
    from src.llm import TokenUsage
    from src.message_queue import MessageQueue
    from src.project.store import ProjectStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = workspace / ".data"
    data_dir.mkdir()

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        ),
        skills_auto_load=False,
    )

    app = Application(config=config)

    # ── Database ──
    from src.db import Database

    db = Database(str(data_dir))
    await db.connect()

    # ── Dedup ──
    from src.core.dedup import DeduplicationService

    dedup = DeduplicationService(db=db)

    # ── Memory ──
    from src.memory import Memory

    memory = Memory(str(workspace))

    # ── Skills ──
    from src.skills import SkillRegistry

    skills = SkillRegistry()

    # ── Routing ──
    from src.routing import RoutingEngine, RoutingRule

    instructions_dir = workspace / "instructions"
    instructions_dir.mkdir(exist_ok=True)
    routing = RoutingEngine(instructions_dir)
    routing._rules = [
        RoutingRule(
            id="app-smoke-catch-all",
            priority=100,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.agent.md",
            enabled=True,
        )
    ]
    # DB.connect() seeds instruction templates into workspace/instructions/,
    # which changes file mtimes.  Synchronise the engine's cache so it
    # doesn't reload from disk and overwrite our catch-all rule.
    routing._file_mtimes = routing._scan_file_mtimes()

    # ── LLM (mocked) ──
    from src.llm import LLMClient

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(
            return_value=make_text_response("App lifecycle response!")
        )
        llm = LLMClient(config.llm)

    # ── Bot ──
    from src.bot import Bot, BotConfig, BotDeps

    bot_config = BotConfig(
        max_tool_iterations=config.llm.max_tool_iterations,
        memory_max_history=config.memory_max_history,
        system_prompt_prefix=config.llm.system_prompt_prefix,
        stream_response=False,
    )
    bot = Bot(BotDeps(
        config=bot_config,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        dedup=dedup,
        instructions_dir=str(instructions_dir),
        rate_limiter=MagicMock(),
        tool_executor=AsyncMock(),
        context_assembler=AsyncMock(),
    ))

    # ── Auxiliary components for BotComponents ──
    project_store = ProjectStore(str(data_dir / "projects.db"))
    project_store.connect()

    message_queue = MessageQueue(str(data_dir))
    await message_queue.connect()

    token_usage = TokenUsage()

    components = BotComponents(
        bot=bot,
        db=db,
        vector_memory=None,
        project_store=project_store,
        token_usage=token_usage,
        message_queue=message_queue,
        llm=llm,
        dedup=dedup,
    )

    # ── Wire Application internals ──
    from src.shutdown import GracefulShutdown

    channel = MockChannel()
    shutdown_mgr = GracefulShutdown(timeout=5.0)

    app._shutdown_mgr = shutdown_mgr
    app._channel = channel
    app._components = components
    app._pipeline = app._build_pipeline()

    return app, db


@pytest.mark.asyncio
async def test_smoke_application_lifecycle(tmp_path: Path):
    """
    E2E Smoke Test: Application._on_message flows through the full lifecycle.

    Verifies that a message sent via ``Application._on_message()`` passes
    through the middleware pipeline, reaches the bot, and produces a
    response that is sent back through the channel.

    Arrange:
        - Create an Application with mocked LLM and mock channel
        - Wire internal components to bypass real startup

    Act:
        - Call ``_on_message()`` with an IncomingMessage

    Assert:
        - Pipeline completes without error
        - Channel received the response
        - Session metrics reflect the processed message
    """
    app, db = await _create_app(tmp_path)

    msg = IncomingMessage(
        message_id="app-smoke-msg-001",
        chat_id="app-smoke-chat@test",
        sender_id="app-tester",
        sender_name="App Tester",
        text="Hello from Application smoke test",
        timestamp=1000.0,
        channel_type=ChannelType.CLI,
        acl_passed=True,
    )

    # ── Act ──
    # Patch log_message_flow to avoid UnicodeEncodeError on Windows (cp1252)
    # when Rich console prints the → arrow character.
    with patch("src.core.message_pipeline.log_message_flow"):
        await app._on_message(msg)

    # ── Assert ──
    channel = app._channel
    assert len(channel._sent) >= 1, (
        f"Channel should have sent at least one message, got {len(channel._sent)}"
    )

    sent_text = channel._sent[-1][1]
    assert "App lifecycle response" in sent_text, (
        f"Channel should contain the bot response, got: {sent_text!r}"
    )

    # Session metrics should reflect the processed message
    metrics = app._session_metrics.to_dict()
    assert metrics["messages_processed"] >= 1, "Session metrics should track at least one message"

    # Cleanup
    await db.close()
