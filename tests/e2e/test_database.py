"""
test_database.py - E2E tests for database operations.

Tests the database layer:
  - Message persistence
  - Chat management
  - Routing rules
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Database Lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_database_connect_creates_directories():
    """
    E2E Test: Database connect creates required directories and files.

    Arrange:
        - Create Database instance

    Act:
        - Connect to database

    Assert:
        - Data directory is created
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "data"
        db = Database(str(db_path))
        await db.connect()

        # Check data directory exists
        assert db_path.exists()

        await db.close()


@pytest.mark.asyncio
async def test_database_close_releases_resources():
    """
    E2E Test: Database close releases resources.

    Arrange:
        - Connect to database

    Act:
        - Close database

    Assert:
        - No exception on double close
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()
        await db.close()

        # Just verify no exception on double close
        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Message Operations
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_save_message_stores_message():
    """
    E2E Test: save_message stores message in database.

    Arrange:
        - Connect to database

    Act:
        - Save a message

    Assert:
        - Message can be retrieved
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        msg_id = await db.save_message(
            chat_id="test-chat",
            role="user",
            content="Hello, bot!",
            name="Test User",
        )

        assert msg_id is not None

        # Verify message exists
        exists = await db.message_exists(msg_id)
        assert exists is True

        await db.close()


@pytest.mark.asyncio
async def test_message_exists_returns_false_for_unknown():
    """
    E2E Test: message_exists returns False for unknown messages.

    Arrange:
        - Connect to database

    Act:
        - Check non-existent message

    Assert:
        - Returns False
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        exists = await db.message_exists("nonexistent-msg-id")

        assert exists is False

        await db.close()


@pytest.mark.asyncio
async def test_get_recent_messages_returns_messages():
    """
    E2E Test: get_recent_messages returns messages in order.

    Arrange:
        - Save multiple messages

    Act:
        - Get recent messages

    Assert:
        - Messages are returned in chronological order
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        # Save messages
        await db.save_message("chat-1", "user", "First message")
        await db.save_message("chat-1", "assistant", "Second message")
        await db.save_message("chat-1", "user", "Third message")

        messages = await db.get_recent_messages("chat-1", limit=10)

        assert len(messages) == 3
        # Should be in chronological order (oldest first)
        assert messages[0]["content"] == "First message"
        assert messages[2]["content"] == "Third message"

        await db.close()


@pytest.mark.asyncio
async def test_get_recent_messages_respects_limit():
    """
    E2E Test: get_recent_messages respects limit parameter.

    Arrange:
        - Save 5 messages

    Act:
        - Get recent with limit 2

    Assert:
        - Only 2 most recent messages returned
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        for i in range(5):
            await db.save_message("chat-1", "user", f"Message {i}")

        messages = await db.get_recent_messages("chat-1", limit=2)

        assert len(messages) == 2
        # Should be the last 2 messages
        assert "Message 3" in messages[0]["content"]
        assert "Message 4" in messages[1]["content"]

        await db.close()


@pytest.mark.asyncio
async def test_messages_isolated_by_chat():
    """
    E2E Test: Messages are isolated by chat_id.

    Arrange:
        - Save messages for different chats

    Act:
        - Get messages for each chat

    Assert:
        - Each chat only sees its own messages
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        await db.save_message("chat-alice", "user", "Alice message")
        await db.save_message("chat-bob", "user", "Bob message")

        alice_msgs = await db.get_recent_messages("chat-alice")
        bob_msgs = await db.get_recent_messages("chat-bob")

        assert len(alice_msgs) == 1
        assert "Alice" in alice_msgs[0]["content"]

        assert len(bob_msgs) == 1
        assert "Bob" in bob_msgs[0]["content"]

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Chat Operations
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_chat_creates_chat():
    """
    E2E Test: upsert_chat creates a new chat.
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        await db.upsert_chat("test-chat", "Test User")

        chats = await db.list_chats()
        assert len(chats) == 1
        assert chats[0]["chat_id"] == "test-chat"
        assert chats[0]["name"] == "Test User"

        await db.close()


@pytest.mark.asyncio
async def test_upsert_chat_updates_existing():
    """
    E2E Test: upsert_chat updates existing chat.
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        await db.upsert_chat("test-chat", "Test User")
        await db.upsert_chat("test-chat", "Updated Name")

        chats = await db.list_chats()
        assert len(chats) == 1
        assert chats[0]["name"] == "Updated Name"

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Routing (frontmatter-based, via RoutingEngine)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routing_rules_loaded_from_frontmatter():
    """
    E2E Test: Routing rules are loaded from instruction file frontmatter.
    """
    from src.routing import RoutingEngine

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up instructions directory with a frontmatter-bearing .md file
        instructions_dir = Path(tmpdir) / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)

        (instructions_dir / "test.agent.md").write_text(
            "---\n"
            "routing:\n"
            "  id: personal-self-chat\n"
            "  priority: 1\n"
            "  fromMe: true\n"
            "---\n\n"
            "# Test instruction\n",
            encoding="utf-8",
        )

        engine = RoutingEngine(instructions_dir)
        engine.load_rules()

        rules = engine.rules
        assert len(rules) >= 1
        assert any(r.id == "personal-self-chat" for r in rules)


@pytest.mark.asyncio
async def test_routing_engine_refresh():
    """
    E2E Test: RoutingEngine picks up changes after refresh.
    """
    from src.routing import RoutingEngine

    with tempfile.TemporaryDirectory() as tmpdir:
        instructions_dir = Path(tmpdir) / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)

        (instructions_dir / "chat.agent.md").write_text(
            "---\nrouting:\n  id: catch-all\n  priority: 10\n---\n\n# Chat\n",
            encoding="utf-8",
        )

        engine = RoutingEngine(instructions_dir)
        engine.load_rules()
        assert len(engine.rules) == 1

        # Add another instruction file
        (instructions_dir / "support.md").write_text(
            "---\n"
            "routing:\n"
            "  id: support-rule\n"
            "  priority: 5\n"
            "  content_regex: '^support'\n"
            "---\n\n"
            "# Support\n",
            encoding="utf-8",
        )

        engine.refresh_rules()
        assert len(engine.rules) == 2


@pytest.mark.asyncio
async def test_database_connect_without_routing():
    """
    E2E Test: Database connects successfully without routing.json.
    Routing is now handled by RoutingEngine + frontmatter.
    """
    from src.db import Database

    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(str(Path(tmpdir) / "data"))
        await db.connect()

        # Database should connect fine without any routing code
        validation = await db.validate_connection()
        assert validation.valid is True

        await db.close()
