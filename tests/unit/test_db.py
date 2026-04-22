"""Tests for Database.save_messages_batch() atomicity, generation counter,
and name field sanitization.

Verifies:
- save_messages_batch persists all messages in a single lock acquisition
- Generation counter increments on each write
- Concurrent calls for the same chat are serialized
- Name field is sanitized (control chars stripped, truncated)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.db.db import Database, _sanitize_name


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """Provide a fresh Database with a temp data directory."""
    data_dir = tmp_path / ".data"
    return Database(str(data_dir))


@pytest.fixture
async def initialized_db(db: Database) -> Database:
    """Provide an initialized Database ready for operations."""
    await db.connect()
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Generation counter tests
# ─────────────────────────────────────────────────────────────────────────────


class TestGenerationCounter:
    """Verify generation counter increments on each write."""

    def test_initial_generation_is_zero(self, db: Database) -> None:
        assert db.get_generation("chat_1") == 0

    async def test_generation_increments_on_save_message(
        self, initialized_db: Database
    ) -> None:
        db = initialized_db
        await db.save_message("chat_1", "user", "hello", "Alice")
        assert db.get_generation("chat_1") == 1

    async def test_generation_increments_on_save_messages_batch(
        self, initialized_db: Database
    ) -> None:
        db = initialized_db
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        await db.save_messages_batch("chat_1", messages)
        assert db.get_generation("chat_1") == 1

    async def test_generation_increments_multiple_writes(
        self, initialized_db: Database
    ) -> None:
        db = initialized_db
        await db.save_message("chat_1", "user", "msg1", "Alice")
        await db.save_message("chat_1", "assistant", "resp1")
        await db.save_message("chat_1", "user", "msg2", "Alice")
        assert db.get_generation("chat_1") == 3

    def test_check_generation_matches(self, db: Database) -> None:
        db._chat_generations["chat_1"] = 5
        assert db.check_generation("chat_1", 5) is True
        assert db.check_generation("chat_1", 4) is False


# ─────────────────────────────────────────────────────────────────────────────
# Batch write atomicity
# ─────────────────────────────────────────────────────────────────────────────


class TestBatchWriteAtomicity:
    """Verify save_messages_batch writes all messages atomically."""

    async def test_all_messages_persisted(self, initialized_db: Database) -> None:
        db = initialized_db
        # Register the chat first
        await db.upsert_chat("chat_1", "Alice")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you?"},
        ]
        ids = await db.save_messages_batch("chat_1", messages)
        assert len(ids) == 3

        # Verify all 3 messages are in the JSONL file
        rows = await db.get_recent_messages("chat_1", limit=10)
        # Filter to only the messages we wrote (non-empty content)
        content_rows = [r for r in rows if r.get("content")]
        assert len(content_rows) == 3

    async def test_concurrent_batch_writes_serialized(
        self, initialized_db: Database
    ) -> None:
        """Concurrent save_messages_batch calls for the same chat are serialized."""
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")

        batch1 = [{"role": "user", "content": "batch1-msg1"}, {"role": "assistant", "content": "batch1-resp1"}]
        batch2 = [{"role": "user", "content": "batch2-msg1"}, {"role": "assistant", "content": "batch2-resp1"}]

        # Run both batches concurrently
        results = await asyncio.gather(
            db.save_messages_batch("chat_1", batch1),
            db.save_messages_batch("chat_1", batch2),
        )

        # Both should succeed
        assert len(results[0]) == 2
        assert len(results[1]) == 2

        # All 4 messages should be persisted (filter out empty-content header)
        rows = await db.get_recent_messages("chat_1", limit=10)
        content_rows = [r for r in rows if r.get("content")]
        assert len(content_rows) == 4

        # Generation should be 2 (one per batch)
        assert db.get_generation("chat_1") == 2


# ─────────────────────────────────────────────────────────────────────────────
# Name sanitization
# ─────────────────────────────────────────────────────────────────────────────


class TestNameSanitization:
    """Verify _sanitize_name strips control chars and truncates."""

    def test_none_returns_none(self) -> None:
        assert _sanitize_name(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _sanitize_name("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _sanitize_name("   \t  ") is None

    def test_normal_name_unchanged(self) -> None:
        assert _sanitize_name("Alice") == "Alice"

    def test_strips_control_characters(self) -> None:
        assert _sanitize_name("Alice\x00Bob\x1f") == "AliceBob"

    def test_strips_c1_control_range(self) -> None:
        assert _sanitize_name("Name\x80\x9fEnd") == "NameEnd"

    def test_preserves_common_whitespace(self) -> None:
        assert _sanitize_name("Alice Smith") == "Alice Smith"

    def test_strips_leading_trailing_whitespace(self) -> None:
        assert _sanitize_name("  Alice  ") == "Alice"

    def test_truncates_long_name(self) -> None:
        long_name = "A" * 300
        result = _sanitize_name(long_name)
        assert result is not None
        assert len(result) == 200

    def test_name_with_emoji_preserved(self) -> None:
        assert _sanitize_name("Alice 😊") == "Alice 😊"

    async def test_build_message_record_sanitizes_name(
        self, initialized_db: Database
    ) -> None:
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")
        await db.save_message("chat_1", "user", "hello", "Bob\x00 Evil")

        rows = await db.get_recent_messages("chat_1", limit=10)
        content_rows = [r for r in rows if r.get("content")]
        assert len(content_rows) == 1
        assert content_rows[0]["name"] == "Bob Evil"

    async def test_build_message_record_truncates_long_name(
        self, initialized_db: Database
    ) -> None:
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")
        long_name = "X" * 300
        await db.save_message("chat_1", "user", "hello", long_name)

        rows = await db.get_recent_messages("chat_1", limit=10)
        content_rows = [r for r in rows if r.get("content")]
        assert len(content_rows) == 1
        assert content_rows[0]["name"] == "X" * 200
