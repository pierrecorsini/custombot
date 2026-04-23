"""Tests for Database.save_messages_batch() atomicity, generation counter,
name field sanitization, and write circuit breaker.

Verifies:
- save_messages_batch persists all messages in a single lock acquisition
- Generation counter increments on each write
- Concurrent calls for the same chat are serialized
- Name field is sanitized (control chars stripped, truncated)
- Write circuit breaker fast-fails when open, records failures, recovers
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.db.db import Database, _sanitize_name
from src.exceptions import DatabaseError
from src.utils.circuit_breaker import CircuitState


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

    async def test_build_message_record_none_content_becomes_empty(
        self, initialized_db: Database
    ) -> None:
        """None content is normalized to empty string before checksum and persist."""
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")
        await db.save_message("chat_1", "assistant", None, "Bot")  # type: ignore[arg-type]

        rows = await db.get_recent_messages("chat_1", limit=10)
        assistant_rows = [r for r in rows if r["role"] == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["content"] == ""

    def test_build_message_record_static_none_content(self) -> None:
        """Static call: None content is coerced to empty string."""
        record, mid = Database._build_message_record("tool", None, "skill_a")
        assert record["content"] == ""
        assert mid  # message ID was generated
        assert "_checksum" in record


# ─────────────────────────────────────────────────────────────────────────────
# Generation counter bounded growth
# ─────────────────────────────────────────────────────────────────────────────


class TestChatGenerationsBoundedGrowth:
    """Verify _chat_generations dict is bounded and evicts oldest entries."""

    def test_get_generation_returns_zero_for_unknown_chat(self, db: Database) -> None:
        """Unknown chat IDs return generation 0 without side effects."""
        assert db.get_generation("nonexistent_chat") == 0
        assert "nonexistent_chat" not in db._chat_generations

    def test_bump_generation_increments_from_zero(self, db: Database) -> None:
        """_bump_generation starts at 0 and increments by 1."""
        assert db.get_generation("chat_1") == 0
        db._bump_generation("chat_1")
        assert db.get_generation("chat_1") == 1
        db._bump_generation("chat_1")
        assert db.get_generation("chat_1") == 2

    def test_bump_generation_independent_per_chat(self, db: Database) -> None:
        """Each chat has an independent generation counter."""
        db._bump_generation("chat_a")
        db._bump_generation("chat_a")
        db._bump_generation("chat_b")
        assert db.get_generation("chat_a") == 2
        assert db.get_generation("chat_b") == 1

    def test_eviction_removes_oldest_entries(self, db: Database) -> None:
        """When _chat_generations exceeds MAX_CHAT_GENERATIONS,
        the oldest quarter of entries is evicted (FIFO order)."""
        from src.constants import MAX_CHAT_GENERATIONS

        # Fill to exactly the cap — no eviction yet.
        for i in range(MAX_CHAT_GENERATIONS):
            db._bump_generation(f"chat_{i:05d}")
        assert len(db._chat_generations) == MAX_CHAT_GENERATIONS

        # One more bump triggers eviction: oldest quarter removed.
        # _bump_generation adds the new entry first (size=10001), then evicts
        # the oldest quarter (2500), leaving 10001 - 2500 = 7501.
        db._bump_generation("chat_overflow")
        expected_size = MAX_CHAT_GENERATIONS + 1 - (MAX_CHAT_GENERATIONS // 4)
        assert len(db._chat_generations) == expected_size

        # The first quarter of chat IDs should have been evicted.
        evicted_count = MAX_CHAT_GENERATIONS // 4
        for i in range(evicted_count):
            assert f"chat_{i:05d}" not in db._chat_generations
        # Remaining entries (and the overflow entry) should still be present.
        for i in range(evicted_count, MAX_CHAT_GENERATIONS):
            assert f"chat_{i:05d}" in db._chat_generations
        assert "chat_overflow" in db._chat_generations

    def test_evicted_chat_generation_resets_to_zero(self, db: Database) -> None:
        """After eviction, get_generation() returns 0 for the evicted chat_id."""
        from src.constants import MAX_CHAT_GENERATIONS

        db._bump_generation("chat_target")
        assert db.get_generation("chat_target") == 1

        # Overflow the dict to force eviction of "chat_target".
        for i in range(MAX_CHAT_GENERATIONS + 1):
            db._bump_generation(f"chat_fill_{i:05d}")

        # "chat_target" should have been evicted.
        assert "chat_target" not in db._chat_generations
        assert db.get_generation("chat_target") == 0

    def test_move_to_end_keeps_recent_chats(self, db: Database) -> None:
        """Re-bumping an existing chat moves it to the end, protecting it from eviction."""
        from src.constants import MAX_CHAT_GENERATIONS

        # Bump chat_first early — it would normally be evicted first.
        db._bump_generation("chat_first")
        # Fill with enough additional chats to approach the cap.
        for i in range(MAX_CHAT_GENERATIONS - 1):
            db._bump_generation(f"chat_mid_{i:05d}")

        # Re-bump "chat_first" to move it to the end (most-recently-used).
        db._bump_generation("chat_first")

        # Now overflow to trigger eviction.
        db._bump_generation("chat_overflow")

        # "chat_first" should survive because it was moved to the end.
        assert "chat_first" in db._chat_generations
        assert db.get_generation("chat_first") == 2


# ─────────────────────────────────────────────────────────────────────────────
# Write circuit breaker tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteCircuitBreaker:
    """Verify the DB write circuit breaker fast-fails on sustained failures."""

    async def test_initial_state_is_closed(self, db: Database) -> None:
        """Circuit breaker starts in CLOSED state (normal operation)."""
        assert db.write_breaker.state == CircuitState.CLOSED
        assert db.write_breaker.failure_count == 0

    async def test_write_succeeds_when_closed(
        self, initialized_db: Database
    ) -> None:
        """Normal write goes through when breaker is CLOSED."""
        mid = await initialized_db.save_message("chat_1", "user", "hello")
        assert mid is not None
        assert initialized_db.write_breaker.state == CircuitState.CLOSED

    async def test_breaker_opens_after_consecutive_failures(
        self, initialized_db: Database
    ) -> None:
        """Breaker transitions to OPEN after failure_threshold consecutive failures."""
        db = initialized_db
        # Force the message directory to be unwritable by pointing the
        # _append_to_file at a non-existent nested path that can't be created.
        original_append = db._append_to_file

        def _failing_append(*args, **kwargs):
            raise OSError("Simulated disk I/O failure")

        db._append_to_file = _failing_append  # type: ignore[assignment]

        from src.constants import DB_WRITE_CIRCUIT_FAILURE_THRESHOLD

        for i in range(DB_WRITE_CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(Exception):
                await db.save_message("chat_1", "user", f"msg_{i}")

        assert db.write_breaker.state == CircuitState.OPEN

        # Restore so teardown doesn't break
        db._append_to_file = original_append  # type: ignore[assignment]

    async def test_fast_fail_when_open(
        self, initialized_db: Database
    ) -> None:
        """When breaker is OPEN, writes are rejected immediately without timeout."""
        db = initialized_db

        # Manually force the breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.save_message("chat_1", "user", "should be rejected")

    async def test_success_resets_failures(
        self, initialized_db: Database
    ) -> None:
        """A successful write resets the failure counter in CLOSED state."""
        db = initialized_db

        # Record a few failures (not enough to open).
        for _ in range(3):
            await db.write_breaker.record_failure()
        assert db.write_breaker.failure_count == 3

        # A successful write resets the count.
        await db.save_message("chat_1", "user", "good write")
        assert db.write_breaker.failure_count == 0
        assert db.write_breaker.state == CircuitState.CLOSED

    async def test_batch_write_guarded(
        self, initialized_db: Database
    ) -> None:
        """save_messages_batch is also protected by the write breaker."""
        db = initialized_db

        # Force breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.save_messages_batch("chat_1", [
                {"role": "user", "content": "batch msg"},
            ])

    async def test_upsert_chat_guarded(
        self, initialized_db: Database
    ) -> None:
        """upsert_chat is also protected by the write breaker."""
        db = initialized_db

        # Force breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.upsert_chat("chat_1", "Alice")

    async def test_read_not_blocked_by_open_breaker(
        self, initialized_db: Database
    ) -> None:
        """Read operations bypass the write circuit breaker entirely."""
        db = initialized_db

        # Write a message first.
        await db.save_message("chat_1", "user", "existing")

        # Force breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        # Reads should still succeed — the write breaker doesn't affect them.
        messages = await db.get_recent_messages("chat_1", limit=10)
        assert len(messages) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent batch writes + compression race
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentBatchAndCompression:
    """Verify no data loss when concurrent batch writes trigger history compression.

    Both save_messages_batch and compress_chat_history acquire the same per-chat
    lock, so they are serialized.  This test exercises the race where a batch
    write completes and triggers compression, while a second batch arrives
    concurrently.  All messages must survive regardless of interleaving order.
    """

    async def test_concurrent_batches_with_compression_no_data_loss(
        self,
        initialized_db: Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Write two batches concurrently where one triggers compression.

        Scenario:
            1. Pre-fill chat to just below compression threshold
            2. Launch two save_messages_batch calls concurrently via gather()
            3. First to write pushes total lines over threshold,
               triggering compress_chat_history after lock release
            4. Second batch arrives while compression is pending / running
            5. Verify ALL batch messages survive with no data loss
        """
        # Arrange — lower thresholds so compression fires at test scale
        monkeypatch.setattr("src.db.db.COMPRESSION_LINE_THRESHOLD", 30)
        monkeypatch.setattr("src.db.db.COMPRESSION_KEEP_RECENT", 10)

        db = initialized_db
        chat_id = "chat_race"
        await db.upsert_chat(chat_id, "TestBot")

        # Pre-fill with messages just below compression threshold.
        # Long content ensures the file-size gate (threshold * 200 bytes) passes.
        padding = "x" * 200
        for i in range(29):
            await db.save_message(chat_id, "user", f"prefill-{i}-{padding}")

        # Act — two batches written concurrently; first triggers compression
        batch1 = [
            {"role": "user", "content": f"batch1-msg{i}-{padding}"} for i in range(5)
        ]
        batch2 = [
            {"role": "user", "content": f"batch2-msg{i}-{padding}"} for i in range(5)
        ]

        ids1, ids2 = await asyncio.gather(
            db.save_messages_batch(chat_id, batch1),
            db.save_messages_batch(chat_id, batch2),
        )

        # Assert — both batches returned valid IDs
        assert len(ids1) == 5
        assert len(ids2) == 5

        # JSONL file must be valid and fully parseable
        msg_file = db._message_file(chat_id)
        raw_lines = [
            line
            for line in msg_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for line in raw_lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict), f"Invalid JSONL line: {line[:80]}"

        # ALL batch messages must be present — no data loss
        rows = await db.get_recent_messages(chat_id, limit=500)
        contents = {r["content"] for r in rows if r.get("content")}

        for i in range(5):
            assert f"batch1-msg{i}-{padding}" in contents, (
                f"batch1-msg{i} lost during concurrent compression"
            )
            assert f"batch2-msg{i}-{padding}" in contents, (
                f"batch2-msg{i} lost during concurrent compression"
            )

        # Generation should reflect both batch writes (≥ 2 bumps)
        assert db.get_generation(chat_id) >= 2
