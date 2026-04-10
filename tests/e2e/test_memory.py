"""
test_memory.py - E2E tests for memory operations.

Tests the memory system:
  - Memory read/write
  - Workspace creation
  - AGENTS.md handling
  - Chat isolation
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Memory Read/Write
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_write_creates_file():
    """
    E2E Test: Writing memory creates MEMORY.md file.

    Arrange:
        - Create Memory instance with temp directory

    Act:
        - Write memory for a chat

    Assert:
        - File is created with correct content
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        await memory.write_memory("test-chat", "# Notes\n- User likes Python")

        # Check file exists (Memory stores under whatsapp_data/<chat_id>/)
        memory_file = Path(tmpdir) / "whatsapp_data" / "test-chat" / "MEMORY.md"
        assert memory_file.exists()
        assert "# Notes" in memory_file.read_text()


@pytest.mark.asyncio
async def test_memory_read_returns_content():
    """
    E2E Test: Reading memory returns stored content.

    Arrange:
        - Write memory content

    Act:
        - Read memory back

    Assert:
        - Content matches
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        content = "# User Preferences\n- Dark mode\n- Python"
        await memory.write_memory("test-chat", content)

        result = await memory.read_memory("test-chat")

        assert result is not None
        assert "Dark mode" in result
        assert "Python" in result


@pytest.mark.asyncio
async def test_memory_read_returns_none_for_nonexistent():
    """
    E2E Test: Reading non-existent memory returns None.

    Arrange:
        - Don't create memory file

    Act:
        - Read memory

    Assert:
        - Returns None
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        result = await memory.read_memory("nonexistent-chat")

        assert result is None


@pytest.mark.asyncio
async def test_memory_write_overwrites_existing():
    """
    E2E Test: Writing memory overwrites existing content.

    Arrange:
        - Write initial memory
        - Write new memory

    Act:
        - Read memory

    Assert:
        - Contains new content only
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        await memory.write_memory("test-chat", "Old content")
        await memory.write_memory("test-chat", "New content")

        result = await memory.read_memory("test-chat")

        assert "New content" in result
        assert "Old content" not in result


@pytest.mark.asyncio
async def test_memory_empty_returns_none():
    """
    E2E Test: Empty memory returns None.

    Arrange:
        - Write empty memory

    Act:
        - Read memory

    Assert:
        - Returns None (empty is treated as not set)
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        await memory.write_memory("test-chat", "   ")  # Whitespace only

        result = await memory.read_memory("test-chat")

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Workspace Creation
# ─────────────────────────────────────────────────────────────────────────────


def test_ensure_workspace_creates_directory():
    """
    E2E Test: ensure_workspace creates chat directory.

    Arrange:
        - Create Memory instance

    Act:
        - Call ensure_workspace

    Assert:
        - Directory exists
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        workspace = memory.ensure_workspace("new-chat")

        assert workspace.exists()
        assert workspace.is_dir()


def test_ensure_workspace_seeds_agents_md():
    """
    E2E Test: ensure_workspace seeds AGENTS.md.

    Arrange:
        - Create Memory instance

    Act:
        - Call ensure_workspace on new chat

    Assert:
        - AGENTS.md is created with default content
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        workspace = memory.ensure_workspace("new-chat")

        agents_file = workspace / "AGENTS.md"
        assert agents_file.exists()
        content = agents_file.read_text()
        assert "Agent Instructions" in content


def test_ensure_workspace_preserves_existing_agents_md():
    """
    E2E Test: ensure_workspace doesn't overwrite existing AGENTS.md.

    Arrange:
        - Create workspace with custom AGENTS.md

    Act:
        - Call ensure_workspace again

    Assert:
        - AGENTS.md content is preserved
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        # First call creates default
        workspace = memory.ensure_workspace("test-chat")

        # Modify AGENTS.md
        agents_file = workspace / "AGENTS.md"
        custom_content = "# Custom Agent\nThis is custom content."
        agents_file.write_text(custom_content)

        # Second call should preserve
        memory.ensure_workspace("test-chat")

        assert agents_file.read_text() == custom_content


# ─────────────────────────────────────────────────────────────────────────────
# Tests: AGENTS.md Reading
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_agents_md_returns_content():
    """
    E2E Test: read_agents_md returns file content.

    Arrange:
        - Create workspace with AGENTS.md

    Act:
        - Read AGENTS.md

    Assert:
        - Content is returned
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        workspace = memory.ensure_workspace("test-chat")

        agents_file = workspace / "AGENTS.md"
        custom_content = "# Custom Agent"
        agents_file.write_text(custom_content)

        result = await memory.read_agents_md("test-chat")

        assert custom_content in result


@pytest.mark.asyncio
async def test_read_agents_md_raises_when_missing():
    """
    E2E Test: read_agents_md raises FileNotFoundError when file missing.

    Arrange:
        - Don't create AGENTS.md

    Act:
        - Read AGENTS.md

    Assert:
        - Raises FileNotFoundError
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        # Create directory but not AGENTS.md
        chat_dir = Path(tmpdir) / "test-chat"
        chat_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="AGENTS.md not found"):
            await memory.read_agents_md("test-chat")


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Chat Isolation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_isolates_by_chat():
    """
    E2E Test: Memory is isolated per chat.

    Arrange:
        - Write different memories for different chats

    Act:
        - Read each chat's memory

    Assert:
        - Each chat has its own memory
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        await memory.write_memory("chat-alice", "Alice's notes")
        await memory.write_memory("chat-bob", "Bob's notes")

        alice_memory = await memory.read_memory("chat-alice")
        bob_memory = await memory.read_memory("chat-bob")

        assert "Alice" in alice_memory
        assert "Bob" not in alice_memory
        assert "Bob" in bob_memory
        assert "Alice" not in bob_memory


def test_workspace_path_returns_correct_directory():
    """
    E2E Test: workspace_path returns correct path.

    Arrange:
        - Create Memory instance

    Act:
        - Get workspace path for chat

    Assert:
        - Path is correct
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)
        path = memory.workspace_path("my-chat")

        assert str(path).endswith("my-chat")
        assert tmpdir in str(path)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Safe Name Handling
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_handles_special_characters_in_chat_id():
    """
    E2E Test: Memory handles special characters in chat ID.

    Arrange:
        - Use chat ID with special characters

    Act:
        - Write and read memory

    Assert:
        - Works correctly (characters are sanitized)
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        # Chat ID with special characters
        weird_chat_id = "user@domain.com#123"
        await memory.write_memory(weird_chat_id, "Test content")

        result = await memory.read_memory(weird_chat_id)

        assert result == "Test content"


@pytest.mark.asyncio
async def test_memory_handles_long_chat_id():
    """
    E2E Test: Memory handles long chat IDs.

    Arrange:
        - Use very long chat ID

    Act:
        - Write and read memory

    Assert:
        - Works correctly
    """
    from src.memory import Memory

    with tempfile.TemporaryDirectory() as tmpdir:
        memory = Memory(tmpdir)

        long_chat_id = "a" * 200  # Very long ID
        await memory.write_memory(long_chat_id, "Long ID test")

        result = await memory.read_memory(long_chat_id)

        assert result == "Long ID test"
