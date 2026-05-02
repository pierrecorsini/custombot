"""
conftest.py - Shared fixtures for unit tests.

Provides a fully-wired Bot instance with mocked LLM, DB, Memory,
Skills, and DedupService so individual test files don't duplicate
mock setup.  Each test gets a fresh Bot — no shared mutable state.

Usage::

    async def test_something(bot: Bot):
        result = await bot.preflight_check(msg)
        assert result is not None
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot import Bot, BotConfig
from src.channels.base import IncomingMessage


# ─────────────────────────────────────────────────────────────────────────────
# Component fixtures (building blocks)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncMock Database with standard stubs."""
    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=False)
    db.upsert_chat = AsyncMock()
    db.save_message = AsyncMock()
    db.get_history = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_llm() -> AsyncMock:
    """AsyncMock LLM provider (satisfies LLMProvider protocol)."""
    return AsyncMock()


@pytest.fixture
def mock_memory() -> AsyncMock:
    """AsyncMock Memory with workspace/memory stubs."""
    memory = AsyncMock()
    memory.ensure_workspace = MagicMock(
        return_value=Path("/tmp/workspace/chat_123")
    )
    memory.read_memory = AsyncMock(return_value="")
    memory.read_agents_md = AsyncMock(return_value="")
    return memory


@pytest.fixture
def mock_skills() -> MagicMock:
    """MagicMock SkillRegistry with empty defaults."""
    skills = MagicMock()
    skills.tool_definitions = []
    skills.all = MagicMock(return_value=[])
    return skills


@pytest.fixture
def mock_dedup() -> AsyncMock:
    """AsyncMock DeduplicationService — no duplicates by default."""
    dedup = AsyncMock()
    dedup.is_inbound_duplicate = AsyncMock(return_value=False)
    dedup.is_outbound_duplicate = MagicMock(return_value=False)
    return dedup


# ─────────────────────────────────────────────────────────────────────────────
# Bot fixture (fully wired)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def bot(
    mock_db: AsyncMock,
    mock_llm: AsyncMock,
    mock_memory: AsyncMock,
    mock_skills: MagicMock,
    mock_dedup: AsyncMock,
) -> Bot:
    """Fully-wired Bot with all dependencies mocked.

    Pass overrides via direct parametrisation or by redefining a
    sub-fixture in your test module::

        # Override just the LLM in a single test
        @pytest.fixture
        def mock_llm():
            llm = AsyncMock()
            llm.chat = AsyncMock(return_value="custom response")
            return llm
    """
    cfg = BotConfig(
        max_tool_iterations=10,
        memory_max_history=50,
        system_prompt_prefix="",
    )
    return Bot(
        config=cfg,
        db=mock_db,
        llm=mock_llm,
        memory=mock_memory,
        skills=mock_skills,
        dedup=mock_dedup,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper: IncomingMessage builder
# ─────────────────────────────────────────────────────────────────────────────


def make_message(
    text: str = "Hello!",
    message_id: str = "msg_001",
    chat_id: str = "chat_123",
    sender_name: str = "Alice",
    sender_id: str = "1234567890",
    channel_type: str = "whatsapp",
    fromMe: bool = False,
    toMe: bool = True,
    acl_passed: bool = True,
    correlation_id: str | None = None,
) -> IncomingMessage:
    """Create a valid IncomingMessage with sensible defaults.

    ``acl_passed`` defaults to ``True`` because unit-test messages
    simulate the post-channel-verification state.
    """
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=time.time(),
        channel_type=channel_type,
        fromMe=fromMe,
        toMe=toMe,
        acl_passed=acl_passed,
        correlation_id=correlation_id,
    )
