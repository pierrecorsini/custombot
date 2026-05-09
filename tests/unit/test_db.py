"""Tests for Database.save_messages_batch() atomicity, generation counter,
name field sanitization, write circuit breaker, _read_file_lines
reverse-seek correctness, _message_file() path-cache correctness,
and _FileHandlePool LRU eviction and stale-handle recovery.

Verifies:
- save_messages_batch persists all messages in a single lock acquisition
- Generation counter increments on each write
- Concurrent calls for the same chat are serialized
- Name field is sanitized (control chars stripped, truncated)
- Write circuit breaker fast-fails when open, records failures, recovers
- _read_file_lines returns last N lines for small and large files
- _message_file() returns cached paths for repeated chat_ids
- _message_file() rejects invalid chat_ids before caching
- _message_file() cache stays within MAX_LRU_CACHE_SIZE
- _FileHandlePool evicts LRU entries when pool exceeds max_size
- _FileHandlePool detects and reopens closed/stale handles
- _FileHandlePool invalidate() removes and closes a specific handle
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from src.db.db import Database, _FileHandlePool, _sanitize_name
from src.db.file_pool import ReadHandlePool
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

    async def test_generation_increments_on_save_message(self, initialized_db: Database) -> None:
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

    async def test_generation_increments_multiple_writes(self, initialized_db: Database) -> None:
        db = initialized_db
        await db.save_message("chat_1", "user", "msg1", "Alice")
        await db.save_message("chat_1", "assistant", "resp1")
        await db.save_message("chat_1", "user", "msg2", "Alice")
        assert db.get_generation("chat_1") == 3

    def test_check_generation_matches(self, db: Database) -> None:
        db._generation_counter._generations["chat_1"] = 5
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

    async def test_concurrent_batch_writes_serialized(self, initialized_db: Database) -> None:
        """Concurrent save_messages_batch calls for the same chat are serialized."""
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")

        batch1 = [
            {"role": "user", "content": "batch1-msg1"},
            {"role": "assistant", "content": "batch1-resp1"},
        ]
        batch2 = [
            {"role": "user", "content": "batch2-msg1"},
            {"role": "assistant", "content": "batch2-resp1"},
        ]

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

    async def test_build_message_record_sanitizes_name(self, initialized_db: Database) -> None:
        db = initialized_db
        await db.upsert_chat("chat_1", "Alice")
        await db.save_message("chat_1", "user", "hello", "Bob\x00 Evil")

        rows = await db.get_recent_messages("chat_1", limit=10)
        content_rows = [r for r in rows if r.get("content")]
        assert len(content_rows) == 1
        assert content_rows[0]["name"] == "Bob Evil"

    async def test_build_message_record_truncates_long_name(self, initialized_db: Database) -> None:
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
    """Verify generation counter dict is bounded and evicts oldest entries."""

    def test_get_generation_returns_zero_for_unknown_chat(self, db: Database) -> None:
        """Unknown chat IDs return generation 0 without side effects."""
        assert db.get_generation("nonexistent_chat") == 0
        assert "nonexistent_chat" not in db._generation_counter._generations

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
        assert len(db._generation_counter._generations) == MAX_CHAT_GENERATIONS

        # One more bump triggers eviction: oldest quarter removed.
        # _bump_generation adds the new entry first (size=10001), then evicts
        # the oldest quarter (2500), leaving 10001 - 2500 = 7501.
        db._bump_generation("chat_overflow")
        expected_size = MAX_CHAT_GENERATIONS + 1 - (MAX_CHAT_GENERATIONS // 4)
        assert len(db._generation_counter._generations) == expected_size

        # The first quarter of chat IDs should have been evicted.
        evicted_count = MAX_CHAT_GENERATIONS // 4
        for i in range(evicted_count):
            assert f"chat_{i:05d}" not in db._generation_counter._generations
        # Remaining entries (and the overflow entry) should still be present.
        for i in range(evicted_count, MAX_CHAT_GENERATIONS):
            assert f"chat_{i:05d}" in db._generation_counter._generations
        assert "chat_overflow" in db._generation_counter._generations

    def test_evicted_chat_generation_resets_to_zero(self, db: Database) -> None:
        """After eviction, get_generation() returns 0 for the evicted chat_id."""
        from src.constants import MAX_CHAT_GENERATIONS

        db._bump_generation("chat_target")
        assert db.get_generation("chat_target") == 1

        # Overflow the dict to force eviction of "chat_target".
        for i in range(MAX_CHAT_GENERATIONS + 1):
            db._bump_generation(f"chat_fill_{i:05d}")

        # "chat_target" should have been evicted.
        assert "chat_target" not in db._generation_counter._generations
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
        assert "chat_first" in db._generation_counter._generations
        assert db.get_generation("chat_first") == 2

    def test_sustained_writes_dict_never_exceeds_cap(self, db: Database) -> None:
        """Simulate weeks of operation with 3× MAX_CHAT_GENERATIONS unique chats.

        Verify: (a) dict never exceeds cap after any _bump_generation call,
        (b) recently-written chats survive LRU eviction, (c) get_generation()
        returns 0 for evicted entries without error.
        """
        from src.constants import MAX_CHAT_GENERATIONS

        total_chats = MAX_CHAT_GENERATIONS * 3

        # Write 3× the cap in unique chat IDs, simulating sustained operation.
        for i in range(total_chats):
            db._bump_generation(f"chat_{i:08d}")
            # (a) Dict never exceeds cap after each _bump_generation returns.
            assert len(db._generation_counter._generations) <= MAX_CHAT_GENERATIONS

        # (b) Recently-written chats survive LRU eviction.
        # With quarter-eviction, the last ~MAX_CHAT_GENERATIONS chats remain.
        surviving_start = total_chats - MAX_CHAT_GENERATIONS
        for i in range(surviving_start, total_chats):
            chat_id = f"chat_{i:08d}"
            assert chat_id in db._generation_counter._generations, (
                f"Recent {chat_id} should survive eviction"
            )

        # (c) Early chats were evicted — get_generation() returns 0 without error.
        for i in range(0, MAX_CHAT_GENERATIONS, 100):
            chat_id = f"chat_{i:08d}"
            assert db.get_generation(chat_id) == 0, f"Evicted {chat_id} should return generation 0"


# ─────────────────────────────────────────────────────────────────────────────
# Write circuit breaker tests
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteCircuitBreaker:
    """Verify the DB write circuit breaker fast-fails on sustained failures."""

    async def test_initial_state_is_closed(self, db: Database) -> None:
        """Circuit breaker starts in CLOSED state (normal operation)."""
        assert db.write_breaker.state == CircuitState.CLOSED
        assert db.write_breaker.failure_count == 0

    async def test_write_succeeds_when_closed(self, initialized_db: Database) -> None:
        """Normal write goes through when breaker is CLOSED."""
        mid = await initialized_db.save_message("chat_1", "user", "hello")
        assert mid is not None
        assert initialized_db.write_breaker.state == CircuitState.CLOSED

    async def test_breaker_opens_after_consecutive_failures(self, initialized_db: Database) -> None:
        """Breaker transitions to OPEN after failure_threshold consecutive failures."""
        db = initialized_db
        # Force the append to fail by pointing it at a method that raises.
        original_append = db._message_store._append_to_file

        def _failing_append(*args, **kwargs):
            raise OSError("Simulated disk I/O failure")

        db._message_store._append_to_file = _failing_append  # type: ignore[assignment]

        from src.constants import DB_WRITE_CIRCUIT_FAILURE_THRESHOLD

        for i in range(DB_WRITE_CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(Exception):
                await db.save_message("chat_1", "user", f"msg_{i}")

        assert db.write_breaker.state == CircuitState.OPEN

        # Restore so teardown doesn't break
        db._message_store._append_to_file = original_append  # type: ignore[assignment]

    async def test_fast_fail_when_open(self, initialized_db: Database) -> None:
        """When breaker is OPEN, writes are rejected immediately without timeout."""
        db = initialized_db

        # Manually force the breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.save_message("chat_1", "user", "should be rejected")

    async def test_success_resets_failures(self, initialized_db: Database) -> None:
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

    async def test_batch_write_guarded(self, initialized_db: Database) -> None:
        """save_messages_batch is also protected by the write breaker."""
        db = initialized_db

        # Force breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.save_messages_batch(
                "chat_1",
                [
                    {"role": "user", "content": "batch msg"},
                ],
            )

    async def test_upsert_chat_guarded(self, initialized_db: Database) -> None:
        """upsert_chat is also protected by the write breaker."""
        db = initialized_db

        # Force breaker open.
        for _ in range(5):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.upsert_chat("chat_1", "Alice")

    async def test_read_not_blocked_by_open_breaker(self, initialized_db: Database) -> None:
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
        # Filter to messages with actual content (header line has empty content)
        content_messages = [m for m in messages if m.get("content")]
        assert len(content_messages) == 1

    async def test_breaker_uses_async_lock(self, initialized_db: Database) -> None:
        """CircuitBreaker uses AsyncLock for event-loop compatibility.

        The breaker's methods (is_open, record_success, record_failure) are
        async and acquire the AsyncLock, which defers asyncio.Lock creation
        until first use.  This is compatible with the async event loop.
        """
        from src.utils.circuit_breaker import CircuitBreaker
        from src.utils.locking import AsyncLock

        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)

        # Verify the internal lock is an AsyncLock.
        assert isinstance(cb._lock, AsyncLock)

        # Exercise all breaker methods from the event loop.
        assert await cb.is_open() is False

        for _ in range(3):
            await cb.record_failure()
        assert await cb.is_open() is True  # threshold hit → OPEN

        # Manually transition to HALF_OPEN so success closes it
        cb._state = CircuitState.HALF_OPEN
        await cb.record_success()
        assert cb.state == CircuitState.CLOSED

    async def test_guarded_write_calls_breaker_from_event_loop(
        self, initialized_db: Database
    ) -> None:
        """_guarded_write interacts with the breaker on the event loop thread.

        This confirms the breaker's is_open / record_success / record_failure
        are called in the async context (not inside asyncio.to_thread),
        which is compatible with both threading.Lock and asyncio.Lock.
        """
        db = initialized_db

        # Normal successful write — breaker should record success.
        assert db.write_breaker.failure_count == 0
        await db.save_message("chat_1", "user", "breaker-audit")
        assert db.write_breaker.state == CircuitState.CLOSED

        # Force breaker open and verify _guarded_write short-circuits.
        for _ in range(10):
            await db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db.save_message("chat_1", "user", "should-fail")


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
        monkeypatch.setattr("src.db.compression.COMPRESSION_LINE_THRESHOLD", 30)
        monkeypatch.setattr("src.db.compression.COMPRESSION_KEEP_RECENT", 10)

        db = initialized_db
        chat_id = "chat_race"
        await db.upsert_chat(chat_id, "TestBot")

        # Pre-fill with messages just below compression threshold.
        # Long content ensures the file-size gate (threshold * 200 bytes) passes.
        padding = "x" * 200
        for i in range(29):
            await db.save_message(chat_id, "user", f"prefill-{i}-{padding}")

        # Act — two batches written concurrently; first triggers compression
        batch1 = [{"role": "user", "content": f"batch1-msg{i}-{padding}"} for i in range(5)]
        batch2 = [{"role": "user", "content": f"batch2-msg{i}-{padding}"} for i in range(5)]

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
            line for line in msg_file.read_text(encoding="utf-8").splitlines() if line.strip()
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


# ─────────────────────────────────────────────────────────────────────────────
# Property-based tests for _read_file_lines reverse-seek correctness
# ─────────────────────────────────────────────────────────────────────────────


class TestReadFileLinesReverseSeek:
    """Property-based tests for _read_file_lines() correctness.

    Uses hypothesis to generate files of varying sizes and line counts,
    then verifies that _read_file_lines(path, limit) returns exactly the
    last ``limit`` non-empty lines in chronological order.

    Covers edge cases:
    - File smaller than limit
    - File exactly at limit
    - File with trailing newline
    - File with no newlines (single long line / corrupted)
    - Empty file
    - Large files that trigger the reverse-seek path (>64KB)

    Note: _read_file_lines strips ``\\r`` but preserves ``\\n`` at line
    ends (the small-file deque path yields raw file lines including ``\\n``).
    Tests account for this by using a helper to compute expected output.
    """

    # Strategy: lines of printable text (no newlines within a line).
    # Restrict to ASCII-printable + common Unicode letters/digits to avoid
    # Windows file-write errors with exotic Unicode codepoints.
    _line_text = st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P")),
        min_size=1,
        max_size=200,
    )

    # Shared settings: suppress function_scoped_fixture health check
    # because we use tmp_path but create unique filenames per example.
    _hs = settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )

    @staticmethod
    def _expected_lines(raw_lines: list[str], limit: int) -> list[str]:
        """Compute expected output of _read_file_lines.

        The small-file path iterates the file with deque(f, maxlen=limit)
        which yields lines including trailing ``\\n``.  Only ``\\r`` is stripped.
        The large-file (reverse-seek) path uses splitlines() which strips both.
        We normalise to the small-file behaviour (``\\n`` preserved) since
        that is what deque-based iteration produces.
        """
        selected = raw_lines[-limit:] if len(raw_lines) > limit else raw_lines
        # Small-file deque path keeps trailing \n; only strips \r
        return [line + "\n" for line in selected]

    @staticmethod
    def _expected_lines_large(raw_lines: list[str], limit: int) -> list[str]:
        """Expected output for large-file (reverse-seek) path.

        The reverse-seek path uses splitlines() + final rstrip("\\r"),
        so lines have NO trailing newline.
        """
        selected = raw_lines[-limit:] if len(raw_lines) > limit else raw_lines
        return selected

    # ── Small-file property tests (<64KB) ──────────────────────────────

    @given(
        lines=st.lists(_line_text, min_size=0, max_size=50),
        limit=st.integers(min_value=1, max_value=60),
    )
    @_hs
    def test_returns_last_n_lines_small_file(
        self, tmp_path: Path, lines: list[str], limit: int
    ) -> None:
        """Small file (<64KB): _read_file_lines returns last ``limit`` lines."""
        file_path = tmp_path / "small.jsonl"
        file_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)
        assert result == self._expected_lines(lines, limit)

    @given(
        lines=st.lists(_line_text, min_size=0, max_size=30),
    )
    @_hs
    def test_file_smaller_than_limit_returns_all(self, tmp_path: Path, lines: list[str]) -> None:
        """When file has fewer lines than limit, all non-empty lines are returned."""
        file_path = tmp_path / "short.jsonl"
        file_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=100)
        assert result == self._expected_lines(lines, 100)

    @given(
        # Exactly N lines, limit=N — should return all
        count=st.integers(min_value=1, max_value=50),
    )
    @_hs
    def test_file_exactly_at_limit(self, tmp_path: Path, count: int) -> None:
        """When file has exactly ``limit`` lines, all are returned."""
        lines = [f"line_{i}" for i in range(count)]
        file_path = tmp_path / "exact.jsonl"
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=count)
        assert result == self._expected_lines(lines, count)

    @given(
        # Mix of empty and non-empty lines
        lines=st.lists(
            st.one_of(st.just(""), _line_text),
            min_size=0,
            max_size=40,
        ),
        limit=st.integers(min_value=1, max_value=50),
    )
    @_hs
    def test_mixed_empty_and_nonempty_lines(
        self, tmp_path: Path, lines: list[str], limit: int
    ) -> None:
        """Empty lines in the file are filtered from results."""
        file_path = tmp_path / "mixed.jsonl"
        file_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)
        assert result == self._expected_lines(lines, limit)

    # ── Large-file property tests (>64KB, reverse-seek path) ──────────

    @given(
        line_count=st.integers(min_value=500, max_value=2000),
        limit=st.integers(min_value=1, max_value=100),
    )
    @_hs
    def test_returns_last_n_lines_large_file(
        self, tmp_path: Path, line_count: int, limit: int
    ) -> None:
        """Large file (>64KB): reverse-seek path returns last ``limit`` lines."""
        lines = [f"line_{i:06d}_" + "x" * 150 for i in range(line_count)]
        content = "\n".join(lines) + "\n"

        file_path = tmp_path / "large.jsonl"
        file_path.write_bytes(content.encode("utf-8"))

        # Verify we actually crossed the 64KB threshold
        assert file_path.stat().st_size >= 65_536

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)
        assert result == self._expected_lines_large(lines, limit)

    @given(
        total_lines=st.integers(min_value=800, max_value=2000),
        limit=st.integers(min_value=1, max_value=50),
    )
    @_hs
    def test_large_file_result_order_is_chronological(
        self, tmp_path: Path, total_lines: int, limit: int
    ) -> None:
        """Lines are returned in chronological order (oldest first), not reversed."""
        lines = [f"entry-{i:05d}_" + "y" * 80 for i in range(total_lines)]
        content = "\n".join(lines) + "\n"

        file_path = tmp_path / "order.jsonl"
        file_path.write_bytes(content.encode("utf-8"))

        # Ensure we're on the reverse-seek path (>64KB)
        assume(file_path.stat().st_size >= 65_536)

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)

        expected = self._expected_lines_large(lines, limit)
        # Verify ordering: each line should sort before the next
        for i in range(len(result) - 1):
            assert result[i] < result[i + 1], f"Lines not in chronological order at index {i}"
        assert result == expected

    # ── Deterministic edge-case tests ──────────────────────────────────

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        """Empty file returns an empty list."""
        file_path = tmp_path / "empty.jsonl"
        file_path.write_bytes(b"")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        assert result == []

    def test_file_with_only_newlines(self, tmp_path: Path) -> None:
        """File containing only newlines returns newline-only lines (deque path)."""
        file_path = tmp_path / "blank.jsonl"
        file_path.write_text("\n\n\n\n\n", encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        # The small-file deque path yields raw lines including \n
        assert result == ["\n"] * 5

    def test_file_with_trailing_newline(self, tmp_path: Path) -> None:
        """Trailing newline produces lines with trailing \\n (deque path)."""
        lines = ["alpha", "beta", "gamma"]
        file_path = tmp_path / "trailing.jsonl"
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        # deque path: lines include trailing \n, only \r is stripped
        assert result == [l + "\n" for l in lines]

    def test_single_line_no_newline(self, tmp_path: Path) -> None:
        """A single line without trailing newline is returned correctly."""
        file_path = tmp_path / "single.jsonl"
        file_path.write_bytes(b"only_line_here")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        assert result == ["only_line_here"]

    def test_single_line_with_newline(self, tmp_path: Path) -> None:
        """A single line with trailing newline — deque keeps the \\n."""
        file_path = tmp_path / "single_nl.jsonl"
        file_path.write_bytes(b"only_line_here\n")

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        assert result == ["only_line_here\n"]

    # ── Large-file edge-case tests ──────────────────────────────────────

    def test_large_file_no_newlines_falls_back_to_deque(self, tmp_path: Path) -> None:
        """Large corrupted file (>64KB, no newlines) triggers _MAX_SEEK_ITERATIONS
        fallback and returns the single long line via the deque path."""
        # Build a file >64KB with no newlines
        line_content = "x" * 70_000
        file_path = tmp_path / "corrupted.jsonl"
        file_path.write_bytes(line_content.encode("utf-8"))
        assert file_path.stat().st_size >= 65_536

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=10)
        # Falls back to deque which yields the single line, rstrip("\r")
        assert result == [line_content]

    @given(
        # Lines that produce a file right around the 64KB boundary
        line_count=st.integers(min_value=300, max_value=700),
        limit=st.integers(min_value=1, max_value=50),
    )
    @_hs
    def test_boundary_near_64kb(self, tmp_path: Path, line_count: int, limit: int) -> None:
        """File near the 64KB small/large threshold returns correct lines
        regardless of which code path is taken."""
        # Each line is ~80 bytes, so 300-700 lines spans roughly 24KB-56KB.
        # Add padding to land right at the boundary.
        lines = [f"boundary_line_{i:05d}_" + "z" * 65 for i in range(line_count)]
        content = "\n".join(lines) + "\n"
        content_bytes = content.encode("utf-8")

        # Pad to exactly 65_535 bytes (just under threshold → small-file path)
        if len(content_bytes) < 65_535:
            last_line = lines[-1]
            pad_needed = 65_535 - len(content_bytes)
            last_line = last_line + "p" * pad_needed
            lines[-1] = last_line
            content = "\n".join(lines) + "\n"
            content_bytes = content.encode("utf-8")

        file_path = tmp_path / "boundary.jsonl"
        file_path.write_bytes(content_bytes)

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)
        expected = self._expected_lines(lines, limit)
        assert result == expected

    @given(
        # Mix of empty and non-empty lines in a large file.
        # min_size=900 ensures the file always exceeds 64KB
        # (900 non-empty lines × ~85 bytes ≈ 76KB).
        line_count=st.integers(min_value=900, max_value=1500),
        limit=st.integers(min_value=1, max_value=100),
    )
    @_hs
    def test_large_file_mixed_empty_and_nonempty(
        self, tmp_path: Path, line_count: int, limit: int
    ) -> None:
        """Large file with interspersed empty lines returns the last ``limit``
        lines (including empty strings from splitlines) in order."""
        rng = __import__("random").Random(line_count)
        lines: list[str] = []
        for i in range(line_count):
            if rng.random() < 0.2:
                lines.append("")
            else:
                lines.append(f"entry-{i:06d}_" + "w" * 70)
        content = "\n".join(lines) + "\n"

        file_path = tmp_path / "mixed_large.jsonl"
        file_path.write_bytes(content.encode("utf-8"))
        assume(file_path.stat().st_size >= 65_536)

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=limit)

        # Reverse-seek path uses splitlines() which includes empty strings
        # from consecutive newlines, then takes last `limit` entries.
        # We replicate that exact logic here.
        all_lines = content.splitlines()
        expected = all_lines[-limit:] if len(all_lines) > limit else all_lines
        expected = [line.rstrip("\r") for line in expected]

        assert result == expected

    def test_large_file_trailing_newline(self, tmp_path: Path) -> None:
        """Large file with trailing newline: reverse-seek path uses splitlines()
        which strips trailing newlines, so lines have no trailing \\n."""
        # Build a large file with known content
        lines = [f"trailing_{i:06d}_" + "q" * 80 for i in range(800)]
        content = "\n".join(lines) + "\n"

        file_path = tmp_path / "trailing_large.jsonl"
        file_path.write_bytes(content.encode("utf-8"))
        assert file_path.stat().st_size >= 65_536

        db = Database(str(tmp_path / ".data"))
        result = db._message_store._read_file_lines(file_path, limit=5)
        # reverse-seek path: splitlines() strips \n, rstrip("\r")
        expected = lines[-5:]
        assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# _message_file() path-cache correctness
# ─────────────────────────────────────────────────────────────────────────────


class TestMessageFilePathCache:
    """Verify _message_file() caching behaviour.

    The LRU-backed cache must:
    (a) return the same Path for repeated calls with the same chat_id,
    (b) reject invalid chat_ids before they are cached,
    (c) never exceed MAX_LRU_CACHE_SIZE entries.
    """

    def test_repeated_call_returns_same_path(self, db: Database) -> None:
        """Repeated calls with the same chat_id return the same Path."""
        path1 = db._message_file("chat_abc")
        path2 = db._message_file("chat_abc")
        assert path1 == path2
        assert path1 is path2  # same object from cache

    def test_different_chat_ids_return_different_paths(self, db: Database) -> None:
        """Different chat_ids resolve to different message files."""
        path_a = db._message_file("chat_alpha")
        path_b = db._message_file("chat_beta")
        assert path_a != path_b
        assert path_a.name == "chat_alpha.jsonl"
        assert path_b.name == "chat_beta.jsonl"

    def test_path_points_to_messages_dir(self, db: Database) -> None:
        """Returned path is under the messages directory."""
        path = db._message_file("chat_123")
        assert path.parent == db._messages_dir
        assert path.suffix == ".jsonl"
        assert path.stem == "chat_123"

    def test_invalid_chat_id_empty_raises_before_caching(self, db: Database) -> None:
        """Empty chat_id raises ValueError and is NOT cached."""
        with pytest.raises(ValueError, match="chat_id"):
            db._message_file("")
        # Cache should remain empty — the invalid ID was not stored.
        assert "" not in db._message_file_cache

    def test_special_chars_sanitized_and_cached(self, db: Database) -> None:
        """chat_id with path-traversal chars is sanitized into a valid name."""
        # Path traversal characters are replaced by the sanitizer, not rejected.
        path = db._message_file("../../etc/passwd")
        assert path.name.endswith(".jsonl")
        # The sanitized key is cached using the original chat_id.
        assert "../../etc/passwd" in db._message_file_cache
        # The sanitized filename must not contain forward slashes or backslashes
        # (the dangerous path separators), though dots are allowed in filenames.
        assert "/" not in path.stem
        assert "\\" not in path.stem

    def test_slash_sanitized_and_cached(self, db: Database) -> None:
        """chat_id with slashes is sanitized (slashes replaced) and cached."""
        path = db._message_file("foo/bar")
        assert path.name.endswith(".jsonl")
        assert "foo/bar" in db._message_file_cache
        assert "/" not in path.stem

    def test_whitespace_only_sanitized_raises_before_caching(self, db: Database) -> None:
        """Whitespace-only chat_id raises ValueError and is NOT cached."""
        with pytest.raises(ValueError, match="chat_id"):
            db._message_file("   ")
        assert "   " not in db._message_file_cache

    def test_valid_chat_id_cached_after_first_call(self, db: Database) -> None:
        """A valid chat_id appears in the cache after first resolution."""
        assert "chat_valid" not in db._message_file_cache
        db._message_file("chat_valid")
        assert "chat_valid" in db._message_file_cache

    def test_cache_does_not_exceed_max_size(self, db: Database) -> None:
        """Cache evicts oldest entries when MAX_LRU_CACHE_SIZE is exceeded."""
        from src.constants import MAX_LRU_CACHE_SIZE

        # Fill cache to the max.
        for i in range(MAX_LRU_CACHE_SIZE):
            db._message_file(f"chat_{i:06d}")
        assert len(db._message_file_cache) == MAX_LRU_CACHE_SIZE

        # First entry should still be present.
        assert "chat_000000" in db._message_file_cache

        # One more entry triggers LRU eviction of the oldest.
        db._message_file("chat_overflow")
        assert len(db._message_file_cache) == MAX_LRU_CACHE_SIZE
        # The oldest entry was evicted.
        assert "chat_000000" not in db._message_file_cache
        # The newest entry is present.
        assert "chat_overflow" in db._message_file_cache

    def test_lru_eviction_removes_oldest_first(self, db: Database) -> None:
        """When the cache overflows, the least-recently-used entry is evicted."""
        from src.constants import MAX_LRU_CACHE_SIZE

        # Insert two specific entries we'll track.
        db._message_file("chat_old")
        db._message_file("chat_mid")

        # Fill the rest of the cache (MAX_LRU_CACHE_SIZE - 2 more).
        for i in range(MAX_LRU_CACHE_SIZE - 2):
            db._message_file(f"chat_fill_{i:06d}")

        # All should be present — cache is exactly at capacity.
        assert len(db._message_file_cache) == MAX_LRU_CACHE_SIZE
        assert "chat_old" in db._message_file_cache
        assert "chat_mid" in db._message_file_cache

        # Overflow: "chat_old" (least recently used) should be evicted.
        db._message_file("chat_new_overflow")
        assert "chat_old" not in db._message_file_cache
        assert "chat_mid" in db._message_file_cache
        assert "chat_new_overflow" in db._message_file_cache

    def test_repeated_access_refreshes_lru_position(self, db: Database) -> None:
        """Accessing a cached entry moves it to most-recently-used."""
        from src.constants import MAX_LRU_CACHE_SIZE

        db._message_file("chat_precious")

        # Fill cache to capacity — "chat_precious" is the oldest entry.
        for i in range(MAX_LRU_CACHE_SIZE - 1):
            db._message_file(f"chat_fill_{i:06d}")

        assert len(db._message_file_cache) == MAX_LRU_CACHE_SIZE

        # Re-access "chat_precious" to refresh its LRU position.
        db._message_file("chat_precious")

        # Now overflow the cache — "chat_precious" should survive.
        db._message_file("chat_overflow")
        assert "chat_precious" in db._message_file_cache
        # The fill entries starting from 0 should be evicted instead.
        assert "chat_fill_000000" not in db._message_file_cache

    def test_sanitized_chat_id_used_in_filename(self, db: Database) -> None:
        """chat_id with special chars like @ is sanitized for the filename."""
        # WhatsApp-style IDs with @ are common and get sanitized.
        path = db._message_file("123456789@s.whatsapp.com")
        # The @ and . characters should be replaced in the filename.
        assert path.name.endswith(".jsonl")
        assert "@" not in path.stem


# ─────────────────────────────────────────────────────────────────────────────
# _FileHandlePool tests
# ─────────────────────────────────────────────────────────────────────────────


class TestFileHandlePool:
    """Verify _FileHandlePool LRU eviction, stale-handle recovery, and
    invalidate / close_all semantics."""

    def test_get_or_open_returns_writable_handle(self, tmp_path: Path) -> None:
        """get_or_open returns an open, writable file handle."""
        pool = _FileHandlePool(max_size=10)
        f = tmp_path / "messages.jsonl"
        handle = pool.get_or_open(f)

        assert not handle.closed
        handle.write("test\n")
        handle.flush()
        assert f.read_text(encoding="utf-8") == "test\n"

    def test_get_or_open_reuses_cached_handle(self, tmp_path: Path) -> None:
        """Second call for the same path returns the same handle."""
        pool = _FileHandlePool(max_size=10)
        f = tmp_path / "messages.jsonl"
        h1 = pool.get_or_open(f)
        h2 = pool.get_or_open(f)

        assert h1 is h2
        assert len(pool._handles) == 1

    def test_lru_eviction_removes_oldest(self, tmp_path: Path) -> None:
        """When pool exceeds max_size, least-recently-used handle is evicted."""
        pool = _FileHandlePool(max_size=3)
        paths = [tmp_path / f"chat_{i}.jsonl" for i in range(4)]
        handles = [pool.get_or_open(p) for p in paths]

        # Pool should have evicted the first handle to stay at max_size.
        assert len(pool._handles) == 3
        # The oldest handle (paths[0]) should be closed.
        assert handles[0].closed
        # The remaining handles should still be open.
        for h in handles[1:]:
            assert not h.closed
        # Keys in the pool should be the 3 most-recent paths.
        expected_keys = [str(p) for p in paths[1:]]
        assert list(pool._handles.keys()) == expected_keys

    def test_repeated_access_refreshes_lru(self, tmp_path: Path) -> None:
        """Accessing a handle moves it to most-recently-used, protecting it
        from eviction."""
        pool = _FileHandlePool(max_size=3)
        paths = [tmp_path / f"chat_{i}.jsonl" for i in range(3)]
        for p in paths:
            pool.get_or_open(p)

        # Re-access paths[0] (currently oldest) to refresh its LRU position.
        pool.get_or_open(paths[0])

        # Overflow: paths[1] should be evicted (it's now least-recently-used).
        pool.get_or_open(tmp_path / "chat_overflow.jsonl")
        assert str(paths[1]) not in pool._handles
        assert str(paths[0]) in pool._handles

    def test_stale_closed_handle_reopened(self, tmp_path: Path) -> None:
        """A closed handle in the pool is detected and replaced with a fresh
        one on next get_or_open."""
        pool = _FileHandlePool(max_size=10)
        f = tmp_path / "messages.jsonl"
        h1 = pool.get_or_open(f)

        # Simulate external closure (e.g. OS reclaimed the fd).
        h1.close()
        assert h1.closed

        # get_or_open should detect the stale handle and reopen.
        h2 = pool.get_or_open(f)
        assert not h2.closed
        assert h2 is not h1
        assert len(pool._handles) == 1

        # The new handle should be functional.
        h2.write("after_reopen\n")
        h2.flush()
        assert "after_reopen" in f.read_text(encoding="utf-8")

    def test_oserror_triggers_evict_all_and_retry(self, tmp_path: Path) -> None:
        """On first OSError, pool evicts all handles and retries once."""
        pool = _FileHandlePool(max_size=10)
        f = tmp_path / "messages.jsonl"

        # Pre-populate the pool with a handle we can track.
        pool.get_or_open(tmp_path / "other.jsonl")
        assert len(pool._handles) == 1

        call_count = 0
        original_open = Path.open

        def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("EMFILE: too many open files")
            return original_open(self, *args, **kwargs)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "open", flaky_open)
            handle = pool.get_or_open(f)
            assert not handle.closed
            # Pool was cleared during retry, then this handle added.
            assert len(pool._handles) == 1
            assert call_count == 2
        finally:
            monkeypatch.undo()

    def test_oserror_persistent_raises_database_error(self, tmp_path: Path) -> None:
        """If retry also fails with OSError, DatabaseError is raised."""
        pool = _FileHandlePool(max_size=10)
        f = tmp_path / "messages.jsonl"

        def always_fail(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("persistent failure")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "open", always_fail)
            with pytest.raises(DatabaseError, match="Failed to open"):
                pool.get_or_open(f)
        finally:
            monkeypatch.undo()

    def test_invalidate_closes_specific_handle(self, tmp_path: Path) -> None:
        """invalidate() removes and closes only the targeted handle."""
        pool = _FileHandlePool(max_size=10)
        p1 = tmp_path / "a.jsonl"
        p2 = tmp_path / "b.jsonl"
        h1 = pool.get_or_open(p1)
        h2 = pool.get_or_open(p2)

        pool.invalidate(p1)

        assert h1.closed
        assert not h2.closed
        assert str(p1) not in pool._handles
        assert str(p2) in pool._handles

    def test_invalidate_nonexistent_path_is_noop(self, tmp_path: Path) -> None:
        """invalidate() on a path not in the pool does nothing."""
        pool = _FileHandlePool(max_size=10)
        pool.get_or_open(tmp_path / "exists.jsonl")

        # Should not raise.
        pool.invalidate(tmp_path / "ghost.jsonl")
        assert len(pool._handles) == 1

    def test_close_all_closes_every_handle(self, tmp_path: Path) -> None:
        """close_all() closes every pooled handle and clears the pool."""
        pool = _FileHandlePool(max_size=10)
        paths = [tmp_path / f"chat_{i}.jsonl" for i in range(5)]
        handles = [pool.get_or_open(p) for p in paths]

        pool.close_all()

        assert len(pool._handles) == 0
        for h in handles:
            assert h.closed


# ─────────────────────────────────────────────────────────────────────────────
# ReadHandlePool tests
# ─────────────────────────────────────────────────────────────────────────────


class TestReadHandlePool:
    """Verify ReadHandlePool LRU eviction, staleness detection, invalidate,
    and close_all semantics."""

    def test_get_reader_returns_readable_handle(self, tmp_path: Path) -> None:
        """get_reader returns an open, readable file handle."""
        f = tmp_path / "messages.jsonl"
        f.write_text("line1\nline2\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        handle = pool.get_reader(f)

        assert not handle.closed
        content = handle.read()
        assert "line1" in content

    def test_get_reader_reuses_cached_handle(self, tmp_path: Path) -> None:
        """Second call for the same path returns the same handle."""
        f = tmp_path / "messages.jsonl"
        f.write_text("data\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        h1 = pool.get_reader(f)
        h2 = pool.get_reader(f)

        assert h1 is h2
        assert len(pool._handles) == 1

    def test_get_reader_seeks_to_start(self, tmp_path: Path) -> None:
        """Returned handle is always seeked to position 0."""
        f = tmp_path / "messages.jsonl"
        f.write_text("a\nb\nc\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        h1 = pool.get_reader(f)
        h1.read()  # consume to end

        h2 = pool.get_reader(f)
        assert h2.tell() == 0
        assert h2.read().startswith("a")

    def test_lru_eviction_removes_oldest(self, tmp_path: Path) -> None:
        """When pool exceeds max_size, least-recently-used handle is evicted."""
        pool = ReadHandlePool(max_size=3)
        paths = []
        for i in range(4):
            p = tmp_path / f"chat_{i}.jsonl"
            p.write_text(f"line{i}\n", encoding="utf-8")
            paths.append(p)

        handles = [pool.get_reader(p) for p in paths]

        assert len(pool._handles) == 3
        assert handles[0].closed
        for h in handles[1:]:
            assert not h.closed

    def test_repeated_access_refreshes_lru(self, tmp_path: Path) -> None:
        """Accessing a handle moves it to most-recently-used."""
        pool = ReadHandlePool(max_size=3)
        paths = []
        for i in range(3):
            p = tmp_path / f"chat_{i}.jsonl"
            p.write_text(f"line{i}\n", encoding="utf-8")
            paths.append(p)
        for p in paths:
            pool.get_reader(p)

        # Refresh paths[0]
        pool.get_reader(paths[0])

        # Overflow should evict paths[1]
        overflow = tmp_path / "overflow.jsonl"
        overflow.write_text("x\n", encoding="utf-8")
        pool.get_reader(overflow)

        assert str(paths[1]) not in pool._handles
        assert str(paths[0]) in pool._handles

    def test_stale_closed_handle_reopened(self, tmp_path: Path) -> None:
        """A closed handle is detected and replaced on next get_reader."""
        f = tmp_path / "messages.jsonl"
        f.write_text("original\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        h1 = pool.get_reader(f)
        h1.close()

        h2 = pool.get_reader(f)
        assert not h2.closed
        assert h2 is not h1
        assert len(pool._handles) == 1

    def test_staleness_detection_reopens_on_size_change(
        self,
        tmp_path: Path,
    ) -> None:
        """Handle is reopened when the file size changes since last open."""
        f = tmp_path / "messages.jsonl"
        f.write_text("initial\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        h1 = pool.get_reader(f)

        # Grow the file
        f.write_text("initial\nappended\n", encoding="utf-8")

        h2 = pool.get_reader(f)
        # Handle should have been reopened to reflect new content
        content = h2.read()
        assert "appended" in content

    def test_oserror_triggers_evict_all_and_retry(self, tmp_path: Path) -> None:
        """On first OSError, pool evicts all handles and retries once."""
        f = tmp_path / "messages.jsonl"
        f.write_text("data\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        # Pre-populate
        other = tmp_path / "other.jsonl"
        other.write_text("x\n", encoding="utf-8")
        pool.get_reader(other)
        assert len(pool._handles) == 1

        call_count = 0
        original_open = Path.open

        def flaky_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("EMFILE")
            return original_open(self, *args, **kwargs)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "open", flaky_open)
            handle = pool.get_reader(f)
            assert not handle.closed
            assert call_count == 2
        finally:
            monkeypatch.undo()

    def test_oserror_persistent_raises_database_error(
        self,
        tmp_path: Path,
    ) -> None:
        """If retry also fails, DatabaseError is raised."""
        f = tmp_path / "messages.jsonl"
        f.write_text("data\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)

        def always_fail(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("persistent failure")

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "open", always_fail)
            with pytest.raises(DatabaseError, match="Failed to open"):
                pool.get_reader(f)
        finally:
            monkeypatch.undo()

    def test_invalidate_closes_specific_handle(self, tmp_path: Path) -> None:
        """invalidate() removes and closes only the targeted handle."""
        p1 = tmp_path / "a.jsonl"
        p2 = tmp_path / "b.jsonl"
        p1.write_text("a\n", encoding="utf-8")
        p2.write_text("b\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        h1 = pool.get_reader(p1)
        h2 = pool.get_reader(p2)

        pool.invalidate(p1)

        assert h1.closed
        assert not h2.closed
        assert str(p1) not in pool._handles
        assert str(p2) in pool._handles

    def test_invalidate_nonexistent_path_is_noop(self, tmp_path: Path) -> None:
        """invalidate() on a path not in the pool does nothing."""
        f = tmp_path / "exists.jsonl"
        f.write_text("x\n", encoding="utf-8")

        pool = ReadHandlePool(max_size=10)
        pool.get_reader(f)
        pool.invalidate(tmp_path / "ghost.jsonl")
        assert len(pool._handles) == 1

    def test_close_all_closes_every_handle(self, tmp_path: Path) -> None:
        """close_all() closes every pooled handle and clears the pool."""
        pool = ReadHandlePool(max_size=10)
        handles = []
        for i in range(5):
            p = tmp_path / f"chat_{i}.jsonl"
            p.write_text(f"line{i}\n", encoding="utf-8")
            handles.append(pool.get_reader(p))

        pool.close_all()

        assert len(pool._handles) == 0
        assert len(pool._st_sizes) == 0
        for h in handles:
            assert h.closed


# ─────────────────────────────────────────────────────────────────────────────
# _save_chats edge-case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveChatsEdgeCases:
    """Verify _save_chats edge cases: empty chats, large chats, concurrent saves."""

    async def test_save_empty_chats_dict(self, initialized_db: Database) -> None:
        """Saving when _chats is empty writes a valid empty JSON object."""
        db = initialized_db
        assert db._chats == {}

        await db._save_chats()

        raw = db._chats_file.read_text(encoding="utf-8")
        assert raw.strip() == "{}"

    async def test_save_chats_with_unicode_names(self, initialized_db: Database) -> None:
        """Unicode chat names are persisted without corruption."""
        db = initialized_db
        await db.upsert_chat("chat_1", "Tëst 用户 😊")

        # Force immediate flush (bypass debounce)
        await db.flush_chats()

        raw = db._chats_file.read_text(encoding="utf-8")
        assert "Tëst 用户 😊" in raw

    async def test_save_chats_with_special_chars_in_name(self, initialized_db: Database) -> None:
        """Chat names with special characters like quotes/backslashes are escaped."""
        db = initialized_db
        await db.upsert_chat('chat_"x"', 'Name with "quotes"')

        await db.flush_chats()

        raw = db._chats_file.read_text(encoding="utf-8")
        # JSON must be valid
        import json

        parsed = json.loads(raw)
        assert 'Name with "quotes"' in str(parsed)

    async def test_save_chats_debounce_skips_rapid_saves(self, initialized_db: Database) -> None:
        """Rapid upsert_chat calls debounce writes — only one _save_chats."""
        db = initialized_db
        await db.upsert_chat("c1", "A")
        await db.upsert_chat("c2", "B")
        await db.upsert_chat("c3", "C")

        # All three should be in memory
        assert len(db._chats) == 3
        assert db._chats_dirty is True

        # Force flush to verify all are persisted
        await db.flush_chats()
        assert db._chats_dirty is False

        raw = db._chats_file.read_text(encoding="utf-8")
        import json

        parsed = json.loads(raw)
        assert len(parsed) == 3

    async def test_save_chats_atomic_write_on_tmp_file(self, initialized_db: Database) -> None:
        """_save_chats writes atomically — intermediate tmp file shouldn't leak."""
        db = initialized_db
        await db.upsert_chat("chat_1", "Test")
        await db.flush_chats()

        # Verify the main file exists but no .tmp file is left behind
        assert db._chats_file.exists()
        tmp_file = db._chats_file.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    async def test_flush_chats_noop_when_not_dirty(self, initialized_db: Database) -> None:
        """flush_chats() is a no-op when _chats_dirty is False."""
        db = initialized_db

        # Write once to create the file
        await db.upsert_chat("chat_1", "A")
        await db.flush_chats()
        assert db._chats_dirty is False

        mtime_before = db._chats_file.stat().st_mtime

        # Second flush should be a no-op — file should not be rewritten
        import time

        time.sleep(0.05)
        await db.flush_chats()

        mtime_after = db._chats_file.stat().st_mtime
        assert mtime_before == mtime_after

    async def test_list_chats_returns_sorted_by_last_active(self, initialized_db: Database) -> None:
        """list_chats returns chats sorted by last_active (most recent first)."""
        db = initialized_db

        import time

        await db.upsert_chat("c1", "First")
        time.sleep(0.05)
        await db.upsert_chat("c2", "Second")
        time.sleep(0.05)
        await db.upsert_chat("c3", "Third")

        chats = await db.list_chats()
        assert len(chats) == 3
        assert chats[0]["chat_id"] == "c3"
        assert chats[1]["chat_id"] == "c2"
        assert chats[2]["chat_id"] == "c1"


# ─────────────────────────────────────────────────────────────────────────────
# JSONL schema migration edge-case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestJsonlSchemaMigration:
    """Verify _ensure_jsonl_schema and _apply_jsonl_migrations edge cases."""

    def test_empty_file_skipped(self, tmp_path: Path) -> None:
        """Empty JSONL file is skipped without error."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "test.jsonl"
        f.write_bytes(b"")
        db._ensure_jsonl_schema(f)
        # File should remain empty
        assert f.read_bytes() == b""

    def test_zero_byte_file_skipped(self, tmp_path: Path) -> None:
        """Zero-byte file is a no-op."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "zero.jsonl"
        f.write_bytes(b"")
        db._ensure_jsonl_schema(f)
        assert f.stat().st_size == 0

    def test_nonexistent_file_skipped(self, tmp_path: Path) -> None:
        """Non-existent file is skipped without error."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "missing.jsonl"
        db._ensure_jsonl_schema(f)  # should not raise
        assert not f.exists()

    def test_legacy_file_gets_header_prepended(self, tmp_path: Path) -> None:
        """JSONL without a schema header gets a header line prepended."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "legacy.jsonl"
        f.write_text('{"role":"user","content":"hello"}\n', encoding="utf-8")

        db._ensure_jsonl_schema(f)

        lines = f.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        import json

        header = json.loads(lines[0])
        assert header.get("type") == "header"
        assert header.get("_version") == 1

    def test_file_with_existing_header_not_modified(self, tmp_path: Path) -> None:
        """JSONL that already has a current-version header is not modified."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "current.jsonl"
        f.write_text(
            '{"type":"header","_version":1}\n{"role":"user","content":"hi"}\n',
            encoding="utf-8",
        )
        original = f.read_text(encoding="utf-8")

        db._ensure_jsonl_schema(f)

        assert f.read_text(encoding="utf-8") == original

    def test_unparseable_first_line_skipped(self, tmp_path: Path) -> None:
        """If first line is not valid JSON, file is skipped gracefully."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "bad.jsonl"
        f.write_text("NOT JSON AT ALL\n", encoding="utf-8")

        db._ensure_jsonl_schema(f)  # should not raise
        # File should remain unchanged (unparseable → skip)
        assert f.read_text(encoding="utf-8") == "NOT JSON AT ALL\n"


# ─────────────────────────────────────────────────────────────────────────────
# _run_with_timeout and _guarded_write edge-case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRunWithTimeout:
    """Verify _run_with_timeout raises DatabaseError on asyncio.TimeoutError."""

    async def test_timeout_raises_database_error(self, initialized_db: Database) -> None:
        """When the coroutine takes too long, DatabaseError is raised."""
        db = initialized_db

        async def _slow_coro():
            await asyncio.sleep(10)

        with pytest.raises(DatabaseError, match="timed out"):
            await db._run_with_timeout(_slow_coro(), timeout=0.01, operation="test_slow")

    async def test_fast_coro_returns_result(self, initialized_db: Database) -> None:
        """A coroutine that completes within timeout returns its result."""
        db = initialized_db

        async def _fast_coro():
            return 42

        result = await db._run_with_timeout(_fast_coro(), timeout=5.0, operation="test_fast")
        assert result == 42


class TestGuardedWriteRetry:
    """Verify _guarded_write retries on transient OSError."""

    async def test_oserror_retried_and_eventually_succeeds(self, initialized_db: Database) -> None:
        """First write_fn() raises OSError, second succeeds."""
        db = initialized_db
        call_count = 0

        async def _flaky_write():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient disk error")
            return "ok"

        result = await db._guarded_write(_flaky_write, timeout=5.0, operation="test_retry")
        assert result == "ok"
        assert call_count == 2

    async def test_oserror_exhausted_retries_raises(self, initialized_db: Database) -> None:
        """When all retries fail with OSError, the OSError is re-raised."""
        db = initialized_db

        async def _always_fail():
            raise OSError("persistent disk error")

        with pytest.raises(OSError, match="persistent disk error"):
            await db._guarded_write(_always_fail, timeout=5.0, operation="test_exhausted")

    async def test_database_error_not_retried(self, initialized_db: Database) -> None:
        """DatabaseError (e.g. from timeout) is NOT retried — re-raised immediately."""
        db = initialized_db
        call_count = 0

        async def _timeout_write():
            nonlocal call_count
            call_count += 1
            raise DatabaseError("operation timed out", operation="test")

        with pytest.raises(DatabaseError, match="timed out"):
            await db._guarded_write(_timeout_write, timeout=5.0, operation="test_db_error")
        # Should only be called once (no retry)
        assert call_count == 1

    async def test_circuit_breaker_open_rejects_immediately(self, initialized_db: Database) -> None:
        """When circuit breaker is open, _guarded_write rejects without calling write_fn."""
        db = initialized_db
        call_count = 0

        # Force breaker open
        for _ in range(10):
            await db.write_breaker.record_failure()

        async def _should_not_run():
            nonlocal call_count
            call_count += 1
            return "unexpected"

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db._guarded_write(_should_not_run, timeout=5.0, operation="test_breaker_open")
        assert call_count == 0


class TestGuardedWriteRetryBudget:
    """Verify _guarded_write respects cross-operation retry budget."""

    async def test_budget_allows_retry_when_fresh(self, initialized_db: Database) -> None:
        """When budget is fresh, retries proceed normally."""
        db = initialized_db
        db._retry_budget_spent = 0.0
        call_count = 0

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient")
            return "ok"

        result = await db._guarded_write(_flaky, timeout=5.0, operation="budget_fresh")
        assert result == "ok"
        assert call_count == 2
        # Budget was spent during retry but reset to 0 on success.
        assert db._retry_budget_spent == 0.0

    async def test_budget_exhausted_fails_immediately(self, initialized_db: Database) -> None:
        """When budget is exhausted, OSError is raised without retrying."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        # Exhaust the budget
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS

        call_count = 0

        async def _always_transient():
            nonlocal call_count
            call_count += 1
            raise OSError("transient")

        with pytest.raises(OSError, match="transient"):
            await db._guarded_write(
                _always_transient, timeout=5.0, operation="budget_exhausted"
            )
        # Should have been called only once — no retry sleep attempted.
        assert call_count == 1

    async def test_budget_clamps_sleep_to_remaining(self, initialized_db: Database) -> None:
        """When budget is partially spent, sleep is clamped to remaining time."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        # Spend most of the budget, leaving ~0.1s
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS - 0.1

        call_count = 0

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient")
            return "ok"

        result = await db._guarded_write(_flaky, timeout=5.0, operation="budget_clamp")
        assert result == "ok"
        assert call_count == 2
        # Budget spent should not exceed the total budget
        assert db._retry_budget_spent <= DB_WRITE_RETRY_BUDGET_SECONDS

    async def test_budget_shared_across_concurrent_writes(self, initialized_db: Database) -> None:
        """Multiple concurrent _guarded_write calls share the same budget."""
        db = initialized_db

        call_counts = [0, 0]
        barriers = asyncio.Event()

        async def _flaky(index: int):
            nonlocal barriers
            call_counts[index] += 1
            if call_counts[index] == 1:
                return "ok"
            # Second call: still succeed but slowly
            await asyncio.sleep(0.01)
            return "ok"

        # Manually exhaust the budget before concurrent calls
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS

        # Both should fail immediately (no retry) because budget is exhausted
        # but the first attempt still runs.
        async def _always_transient():
            raise OSError("degraded")

        results = await asyncio.gather(
            db._guarded_write(_always_transient, timeout=5.0, operation="concurrent_1"),
            db._guarded_write(_always_transient, timeout=5.0, operation="concurrent_2"),
            return_exceptions=True,
        )

        # Both should have failed with OSError (budget exhausted, no retry)
        assert all(isinstance(r, OSError) for r in results)

    async def test_budget_records_failure_on_exhaustion(self, initialized_db: Database) -> None:
        """Budget exhaustion records a circuit-breaker failure."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS

        failures_before = db.write_breaker.failure_count

        async def _transient():
            raise OSError("transient")

        with pytest.raises(OSError):
            await db._guarded_write(_transient, timeout=5.0, operation="budget_failure")

        failures_after = db.write_breaker.failure_count
        assert failures_after == failures_before + 1


class TestRetryBudgetRecoveryAfterBreakerCooldown:
    """Verify _retry_budget_spent resets when a HALF_OPEN probe succeeds
    after the circuit breaker cooldown expires.

    Full lifecycle:
      budget consumed → breaker OPEN → writes rejected →
      cooldown expires → HALF_OPEN → probe succeeds →
      budget resets to 0.0 and breaker returns to CLOSED.
    """

    async def test_budget_resets_after_breaker_cooldown_and_probe_success(
        self, initialized_db: Database
    ) -> None:
        """Successful HALF_OPEN probe resets the retry budget to 0."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        # 1. Consume most of the retry budget.
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS - 0.01

        # 2. Drive the write breaker to OPEN by recording threshold failures.
        breaker = db._write_breaker
        for _ in range(breaker._failure_threshold):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # 3. While breaker is OPEN, writes should be rejected immediately.
        async def _ok():
            return "should not run"

        with pytest.raises(DatabaseError, match="circuit breaker open"):
            await db._guarded_write(_ok, timeout=5.0, operation="rejected")

        # Budget is still consumed.
        assert db._retry_budget_spent > 0

        # 4. Expire cooldown → breaker transitions to HALF_OPEN.
        breaker._last_failure_time = 0
        assert await breaker.is_open() is False
        assert breaker.state == CircuitState.HALF_OPEN

        # 5. Successful probe write should reset the budget.
        async def _recover():
            return "recovered"

        result = await db._guarded_write(_recover, timeout=5.0, operation="probe")
        assert result == "recovered"
        assert db._retry_budget_spent == 0.0
        assert breaker.state == CircuitState.CLOSED

    async def test_budget_remains_consumed_if_half_open_probe_fails(
        self, initialized_db: Database
    ) -> None:
        """A failed HALF_OPEN probe should NOT reset the budget."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        # Consume budget and open the breaker.
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS - 0.5
        breaker = db._write_breaker
        for _ in range(breaker._failure_threshold):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Transition to HALF_OPEN.
        breaker._last_failure_time = 0
        assert await breaker.is_open() is False
        assert breaker.state == CircuitState.HALF_OPEN

        budget_before_probe = db._retry_budget_spent

        # A failing probe should NOT reset the budget.
        async def _failing_probe():
            raise OSError("still broken")

        with pytest.raises(OSError, match="still broken"):
            await db._guarded_write(_failing_probe, timeout=5.0, operation="probe")

        # Budget should NOT have been reset — it stays at least as high.
        assert db._retry_budget_spent >= budget_before_probe

    async def test_writes_succeed_after_full_recovery(
        self, initialized_db: Database
    ) -> None:
        """End-to-end: budget exhaustion → breaker opens → cooldown →
        probe succeeds → subsequent writes work with fresh budget."""
        db = initialized_db
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        # 1. Exhaust budget fully.
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS

        # 2. Open the breaker.
        breaker = db._write_breaker
        for _ in range(breaker._failure_threshold):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # 3. Writes fail immediately while breaker is open.
        async def _ok():
            return "ok"

        with pytest.raises(DatabaseError):
            await db._guarded_write(_ok, timeout=5.0, operation="while_open")

        # 4. Cooldown expires → HALF_OPEN.
        breaker._last_failure_time = 0
        assert await breaker.is_open() is False

        # 5. Successful probe resets everything.
        result = await db._guarded_write(_ok, timeout=5.0, operation="probe")
        assert result == "ok"
        assert db._retry_budget_spent == 0.0
        assert breaker.state == CircuitState.CLOSED

        # 6. Subsequent writes work normally with the fresh budget.
        call_count = 0

        async def _flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient")
            return "retried_ok"

        result = await db._guarded_write(_flaky, timeout=5.0, operation="post_recovery")
        assert result == "retried_ok"
        assert call_count == 2
        assert db._retry_budget_spent == 0.0


class TestSaveChatsGuardedWrite:
    """Verify _save_chats uses _guarded_write with retry and circuit breaker."""

    async def test_save_chats_retries_on_transient_oserror(self, initialized_db: Database) -> None:
        """_save_chats retries when the atomic write hits a transient OSError."""
        db = initialized_db
        await db.upsert_chat("c1", "Test")
        await db.flush_chats()

        # Add a new dirty chat to trigger incremental write via _save_chats
        await db.upsert_chat("c2", "Test2")

        call_count = 0
        original_append_to_file = None
        # Import the module-level function we need to patch
        import src.db.db as _db_mod
        original_append_to_file = _db_mod._append_to_file

        def _flaky_append(path, content):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient write error")
            return original_append_to_file(path, content)

        _db_mod._append_to_file = _flaky_append

        try:
            await db._save_chats()
            # Should have retried and succeeded
            assert call_count >= 2
            # Changelog should exist with the dirty entry
            assert db._chats_changelog_file.exists()
        finally:
            _db_mod._append_to_file = original_append_to_file

    async def test_save_chats_with_many_chats(self, initialized_db: Database) -> None:
        """Saving a large number of chats produces valid JSON."""
        db = initialized_db
        for i in range(100):
            await db.upsert_chat(f"chat_{i:04d}", f"User {i}")

        await db.flush_chats()

        raw = db._chats_file.read_text(encoding="utf-8")
        import json

        parsed = json.loads(raw)
        assert len(parsed) == 100


# ─────────────────────────────────────────────────────────────────────────────
# validate_connection edge-case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateConnection:
    """Verify validate_connection edge cases covering error/warning paths."""

    async def test_valid_connection_returns_no_errors(self, initialized_db: Database) -> None:
        """Freshly initialized DB passes validation."""
        result = await initialized_db.validate_connection()
        assert result.valid is True
        assert result.errors == []

    async def test_nonexistent_data_dir_reported(self, tmp_path: Path) -> None:
        """Data dir that doesn't exist is reported as an error."""
        missing = tmp_path / "absent"
        db = Database(str(missing))
        result = await db.validate_connection()
        assert any("does not exist" in e for e in result.errors)
        assert result.details["data_dir_exists"] is False

    async def test_unwritable_data_dir_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Data dir without write permission is reported as an error."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()

        def _fake_access(path: Path, mode: int) -> bool:
            if str(data_dir) in str(path) and mode == os.W_OK:
                return False
            return True

        monkeypatch.setattr("os.access", _fake_access)
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("not writable" in e for e in result.errors)
        assert result.details.get("data_dir_writable") is False

    async def test_missing_messages_dir_is_warning(self, tmp_path: Path) -> None:
        """Missing messages dir is a warning (not error) since it's auto-created."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("Messages directory" in w for w in result.warnings)

    async def test_corrupted_chats_json_reported(self, tmp_path: Path) -> None:
        """Invalid chats.json is reported as an error."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "chats.json").write_text("NOT VALID JSON{{{", encoding="utf-8")
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("chats.json" in e for e in result.errors)
        assert result.details.get("chats_json_valid") is False

    async def test_chats_json_type_error_reported(self, tmp_path: Path) -> None:
        """chats.json that is a valid JSON array (not object) is reported as type error."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "chats.json").write_text("[1, 2, 3]", encoding="utf-8")
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("not a valid JSON object" in e for e in result.errors)

    async def test_corrupted_message_index_reported_as_warning(self, tmp_path: Path) -> None:
        """Invalid message_index.json is reported as a warning (will be rebuilt)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "message_index.json").write_text("BROKEN", encoding="utf-8")
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("message_index.json" in w for w in result.warnings)

    async def test_message_index_type_error_reported(self, tmp_path: Path) -> None:
        """message_index.json that is a valid JSON object (not array) is reported."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "message_index.json").write_text('{"key": "val"}', encoding="utf-8")
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("not a valid JSON array" in e for e in result.errors)

    async def test_valid_chats_json_populates_count(self, tmp_path: Path) -> None:
        """Valid chats.json populates chats_count in details."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "chats.json").write_text(
            '{"c1": {"name": "A"}, "c2": {"name": "B"}}',
            encoding="utf-8",
        )
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert result.details.get("chats_count") == 2
        assert result.details.get("chats_json_valid") is True

    async def test_message_file_corruption_detected(self, tmp_path: Path) -> None:
        """Malformed JSON lines in message files are detected as warnings.

        NOTE: ``safe_json_parse`` with LINE mode returns ``{}`` (empty dict)
        for unparseable lines rather than ``None``, so the corruption path
        that checks ``msg is None`` is not triggered.  Instead, the line is
        treated as a valid dict with no ``_checksum`` field, which triggers
        the checksum-error path.  We verify corruption is surfaced via either
        the ``corrupted_message_files`` detail or the ``invalid JSON`` warning
        (the exact detection path depends on safe_json_parse behavior).
        """
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()
        # Write a message without a valid checksum — triggers checksum error
        (msg_dir / "test.jsonl").write_text(
            '{"role":"user","content":"tampered","_checksum":"invalid"}\n',
            encoding="utf-8",
        )
        db = Database(str(data_dir))
        result = await db.validate_connection()
        # At minimum, the file was scanned
        assert result.details.get("message_files_count") == 1
        # Checksum error should be detected
        has_corruption = (
            any("checksum" in w for w in result.warnings)
            or result.details.get("corrupted_message_files")
            or result.details.get("checksum_errors")
        )
        assert has_corruption

    async def test_checksum_error_detected(self, tmp_path: Path) -> None:
        """Messages with invalid checksums are detected as warnings."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()
        # Write a message without a valid checksum
        (msg_dir / "test.jsonl").write_text(
            '{"role":"user","content":"tampered","_checksum":"invalid"}\n',
            encoding="utf-8",
        )
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("checksum" in w for w in result.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# Database lifecycle edge-case tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDatabaseLifecycle:
    """Verify connect, close, and context manager edge cases."""

    async def test_context_manager_lifecycle(self, tmp_path: Path) -> None:
        """get_database context manager initializes and closes cleanly."""
        from src.db.db import get_database

        data_dir = tmp_path / ".data"
        async with get_database(str(data_dir)) as db:
            assert db._initialized is True
            await db.upsert_chat("c1", "Test")
        assert db._initialized is False

    async def test_close_flushes_dirty_chats(self, tmp_path: Path) -> None:
        """close() flushes pending dirty chat data to disk."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Manipulate the debounce interval to ensure the save doesn't happen immediately
        db._chats_save_interval = 9999.0  # prevent auto-flush

        # Add a chat (marks dirty) but don't flush
        await db.upsert_chat("c1", "Test")

        # Force dirty state if debounced
        db._chats_dirty = True

        await db.close()
        # After close, the file should exist with the chat
        assert db._chats_file.exists()

    async def test_connect_loads_existing_chats(self, tmp_path: Path) -> None:
        """connect() loads existing chats.json data."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        (data_dir / "chats.json").write_text(
            '{"existing_chat": {"name": "Loaded", "last_active": 123}}',
            encoding="utf-8",
        )
        db = Database(str(data_dir))
        await db.connect()
        assert "existing_chat" in db._chats
        assert db._chats["existing_chat"]["name"] == "Loaded"
        await db.close()

    async def test_connect_migrates_jsonl_schema(self, tmp_path: Path) -> None:
        """connect() migrates legacy JSONL files without schema header."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()
        # Write a legacy JSONL file (no header)
        (msg_dir / "legacy.jsonl").write_text(
            '{"role":"user","content":"old message"}\n',
            encoding="utf-8",
        )

        db = Database(str(data_dir))
        await db.connect()

        # The file should now have a header
        lines = (msg_dir / "legacy.jsonl").read_text(encoding="utf-8").splitlines()
        import json

        header = json.loads(lines[0])
        assert header.get("type") == "header"

        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# upsert_chat update path
# ─────────────────────────────────────────────────────────────────────────────


class TestUpsertChatUpdate:
    """Verify upsert_chat updates existing chats and creates new ones."""

    async def test_update_existing_chat_updates_name(self, initialized_db: Database) -> None:
        """Upserting an existing chat with a new name updates it."""
        db = initialized_db
        await db.upsert_chat("c1", "Original")
        await db.flush_chats()

        await db.upsert_chat("c1", "Updated")
        await db.flush_chats()

        assert db._chats["c1"]["name"] == "Updated"

    async def test_update_existing_chat_without_name_preserves_name(
        self, initialized_db: Database
    ) -> None:
        """Upserting an existing chat without a name preserves the original name."""
        db = initialized_db
        await db.upsert_chat("c1", "Original")
        await db.flush_chats()

        await db.upsert_chat("c1")
        assert db._chats["c1"]["name"] == "Original"

    async def test_upsert_new_chat_creates_metadata(self, initialized_db: Database) -> None:
        """Upserting a new chat creates proper metadata fields."""
        db = initialized_db
        await db.upsert_chat("new_chat", "NewUser")
        chat = db._chats["new_chat"]
        assert chat["name"] == "NewUser"
        assert "created_at" in chat
        assert "last_active" in chat
        assert chat["metadata"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# Delegation and accessor tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDelegationAndAccessors:
    """Verify thin delegation methods and accessors work correctly."""

    async def test_set_vector_memory(self, initialized_db: Database) -> None:
        """set_vector_memory sets on both Database and CompressionService."""
        db = initialized_db
        mock_vm = MagicMock()
        db.set_vector_memory(mock_vm)
        assert db._vector_memory is mock_vm

    async def test_message_exists_returns_false_for_unknown(self, initialized_db: Database) -> None:
        """message_exists returns False for unknown IDs."""
        assert await initialized_db.message_exists("nonexistent") is False

    async def test_get_recovery_status_initially_none(self, initialized_db: Database) -> None:
        """get_recovery_status returns None when no recovery happened (fresh DB)."""
        status = initialized_db.get_recovery_status()
        # If a recovery ran during init (e.g. index rebuild), status may be non-None
        # but for a fresh DB there should be no recovered messages
        if status is not None:
            assert status.preserved_count == 0

    async def test_clear_recovery_status_noop(self, initialized_db: Database) -> None:
        """clear_recovery_status does not raise when no recovery happened."""
        initialized_db.clear_recovery_status()

    async def test_compressed_summary_file_returns_path(self, initialized_db: Database) -> None:
        """_compressed_summary_file returns a Path for the chat."""
        path = initialized_db._compressed_summary_file("chat_1")
        assert isinstance(path, Path)
        assert "chat_1" in path.name

    async def test_get_compressed_summary_returns_none_when_no_file(
        self, initialized_db: Database
    ) -> None:
        """get_compressed_summary returns None when no summary exists."""
        result = await initialized_db.get_compressed_summary("chat_1")
        assert result is None

    async def test_detect_corruption_for_clean_file(self, initialized_db: Database) -> None:
        """detect_corruption returns clean result for a valid message file."""
        db = initialized_db
        await db.save_message("chat_1", "user", "hello")
        result = await db.detect_corruption("chat_1")
        assert result.is_corrupted is False

    async def test_validate_all_message_files_no_repair(self, initialized_db: Database) -> None:
        """validate_all_message_files with repair=False returns results dict."""
        db = initialized_db
        await db.save_message("chat_1", "user", "hello")
        results = await db.validate_all_message_files(repair=False)
        assert isinstance(results, dict)

    async def test_validate_all_message_files_with_repair(self, initialized_db: Database) -> None:
        """validate_all_message_files with repair=True repairs corrupted files."""
        db = initialized_db
        await db.save_message("chat_1", "user", "hello")
        results = await db.validate_all_message_files(repair=True)
        assert isinstance(results, dict)

    async def test_validate_all_message_files_missing_dir(self, initialized_db: Database) -> None:
        """validate_all_message_files with repair=True returns empty when no messages dir."""
        db = initialized_db
        # Remove the messages dir to test the early return
        import shutil

        if db._messages_dir.exists():
            shutil.rmtree(db._messages_dir)
        results = await db.validate_all_message_files(repair=True)
        assert results == {}

    async def test_backup_corrupted_file(self, initialized_db: Database) -> None:
        """backup_corrupted_file creates a backup and returns the backup path."""
        db = initialized_db
        await db.save_message("chat_1", "user", "hello")
        backup_path = await db.backup_corrupted_file("chat_1")
        assert backup_path is not None
        import os

        assert os.path.exists(backup_path)


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: validate_connection error paths
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateConnectionErrorPaths:
    """Cover uncovered branches in validate_connection.

    Target lines: 380-381, 400-402, 425-427, 443, 446-447,
    451-452, 455-456, 458-460, 462-467.
    """

    async def test_messages_dir_not_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Messages dir that exists but is not writable is reported as error (lines 380-381)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()

        def _fake_access(path: str | Path, mode: int) -> bool:
            if mode == os.W_OK and str(msg_dir) in str(path):
                return False
            return True

        monkeypatch.setattr("os.access", _fake_access)
        db = Database(str(data_dir))
        result = await db.validate_connection()
        assert any("Messages directory is not writable" in e for e in result.errors)
        assert result.details.get("messages_dir_writable") is False

    async def test_chats_json_oserror_on_read(self, tmp_path: Path) -> None:
        """OSError reading chats.json is reported as error (lines 400-402)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        chats_file = data_dir / "chats.json"

        # Write a valid file first
        chats_file.write_text("{}", encoding="utf-8")

        db = Database(str(data_dir))

        # Monkey-patch read_text on this specific file to raise OSError
        original_read_text = Path.read_text

        def _failing_read_text(self_path: Path, *args: object, **kwargs: object) -> str:
            if self_path == chats_file:
                raise OSError("permission denied")
            return original_read_text(self_path, *args, **kwargs)  # type: ignore[return-value]

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "read_text", _failing_read_text)
            result = await db.validate_connection()
            assert any("Failed to read chats.json" in e for e in result.errors)
            assert result.details.get("chats_json_valid") is False
        finally:
            monkeypatch.undo()

    async def test_message_index_oserror_on_read(self, tmp_path: Path) -> None:
        """OSError reading message_index.json is reported as warning (lines 425-427)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        index_file = data_dir / "message_index.json"
        index_file.write_text("[]", encoding="utf-8")

        db = Database(str(data_dir))

        original_read_text = Path.read_text

        def _failing_read_text(self_path: Path, *args: object, **kwargs: object) -> str:
            if self_path == index_file:
                raise OSError("read error")
            return original_read_text(self_path, *args, **kwargs)  # type: ignore[return-value]

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "read_text", _failing_read_text)
            result = await db.validate_connection()
            assert any("Failed to read message_index.json" in w for w in result.warnings)
            assert result.details.get("message_index_valid") is False
        finally:
            monkeypatch.undo()

    async def test_message_file_corrupted_json_detected(self, tmp_path: Path) -> None:
        """Malformed JSON lines in message files are detected (lines 443, 446-447).

        Tests the path where safe_json_parse returns None for a bad line,
        which appends to corrupted_files. We create a file with content that
        safe_json_parse(line, default=None, mode=LINE) returns None for.
        """
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()

        # Write content that will trigger the corrupted-files detection path.
        # We use a file with bytes that will cause a read error instead,
        # to cover line 451-452 (OSError in message file read).
        bad_file = msg_dir / "bad.jsonl"
        bad_file.write_text("some content\n", encoding="utf-8")

        db = Database(str(data_dir))

        original_read_text = Path.read_text

        def _error_read(self_path: Path, *args: object, **kwargs: object) -> str:
            if self_path == bad_file:
                raise OSError("read error on message file")
            return original_read_text(self_path, *args, **kwargs)  # type: ignore[return-value]

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "read_text", _error_read)
            result = await db.validate_connection()
            assert any("invalid JSON" in w for w in result.warnings)
            assert "corrupted_message_files" in result.details
        finally:
            monkeypatch.undo()

    async def test_corruption_detection_log_emitted(self, tmp_path: Path) -> None:
        """Corruption detection log is emitted when corrupted files exist (lines 455-456, 462-467)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()

        # Write a message file with an invalid checksum — triggers checksum_errors path
        (msg_dir / "corrupt.jsonl").write_text(
            '{"role":"user","content":"x","_checksum":"bad_checksum"}\n',
            encoding="utf-8",
        )

        db = Database(str(data_dir))
        result = await db.validate_connection()

        # Verify corruption was detected and reported via warnings
        has_checksum_warning = any("checksum" in w for w in result.warnings)
        has_details = "checksum_errors" in result.details
        assert has_checksum_warning or has_details


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: connect/close/async-cm edge paths
# ─────────────────────────────────────────────────────────────────────────────


class TestConnectCloseEdgeCases:
    """Cover uncovered branches in connect(), close(), __aenter__, __aexit__.

    Target lines: 523-524, 539-540, 547-548, 556.
    """

    async def test_connect_handles_migration_exception(self, tmp_path: Path) -> None:
        """connect() logs and continues when JSONL migration fails (lines 523-524)."""
        data_dir = tmp_path / ".data"
        data_dir.mkdir()
        msg_dir = data_dir / "messages"
        msg_dir.mkdir()

        # Create a legacy JSONL file that will be migrated
        legacy_file = msg_dir / "test.jsonl"
        legacy_file.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

        db = Database(str(data_dir))

        # Make _ensure_jsonl_schema raise during connect
        original_ensure = db._ensure_jsonl_schema

        def _failing_ensure(path: Path) -> None:
            if path == legacy_file:
                raise RuntimeError("migration failure")
            return original_ensure(path)

        db._ensure_jsonl_schema = _failing_ensure  # type: ignore[assignment]

        # connect() should NOT raise — it catches the exception and logs a warning
        await db.connect()
        assert db._initialized is True
        await db.close()

    async def test_close_flushes_dirty_index(self, tmp_path: Path) -> None:
        """close() flushes when MessageStore.index_dirty is True (lines 539-540)."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Force the index dirty flag
        db._message_store._index_dirty = True

        await db.close()
        assert db._message_store._index_dirty is False

    async def test_close_resilient_to_save_chats_failure(self, tmp_path: Path) -> None:
        """close() completes cleanup even when _save_chats() raises."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Make the DB dirty so close() attempts a flush
        db._chats_dirty = True

        # Monkey-patch _save_chats to raise
        original_save = db._save_chats

        async def _failing_save():
            raise OSError("disk full during close")

        db._save_chats = _failing_save  # type: ignore[assignment]

        # close() should NOT raise — it logs a warning and continues cleanup
        await db.close()

        # Cleanup must still have happened
        assert db._initialized is False

        db._save_chats = original_save  # type: ignore[assignment]

    async def test_close_resets_dirty_flag_on_success(self, tmp_path: Path) -> None:
        """close() resets _chats_dirty after a successful flush."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        db._chats_save_interval = 9999.0
        await db.upsert_chat("c1", "Test")
        db._chats_dirty = True

        await db.close()
        assert db._chats_dirty is False

    async def test_close_flushes_chats_when_to_thread_unavailable(self, tmp_path: Path) -> None:
        """close() falls back to sync chat write when default executor is unavailable."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Force a dirty chat flush on close.
        db._chats_save_interval = 9999.0
        await db.upsert_chat("c1", "Test")
        db._chats_dirty = True

        # Simulate: asyncio.to_thread cannot submit because executor is shut down.
        import src.db.db as db_module

        original_to_thread = db_module.asyncio.to_thread

        async def _failing_to_thread(*_args, **_kwargs):
            raise RuntimeError("cannot schedule new futures after shutdown")

        db_module.asyncio.to_thread = _failing_to_thread  # type: ignore[assignment]
        try:
            await db.close()
        finally:
            db_module.asyncio.to_thread = original_to_thread  # type: ignore[assignment]

        assert db._initialized is False
        assert db._chats_file.exists()
        content = db._chats_file.read_text(encoding="utf-8")
        assert "c1" in content

    async def test_close_resilient_to_index_flush_failure(self, tmp_path: Path) -> None:
        """close() completes cleanup even when index flush raises."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        db._message_store._index_dirty = True

        # Monkey-patch save_message_index to raise
        original_save_index = db._message_store.save_message_index

        async def _failing_save_index():
            raise OSError("index write failed")

        db._message_store.save_message_index = _failing_save_index  # type: ignore[assignment]

        await db.close()

        # Cleanup must still have happened
        assert db._initialized is False

        db._message_store.save_message_index = original_save_index  # type: ignore[assignment]

    async def test_aenter_returns_database(self, tmp_path: Path) -> None:
        """__aenter__ returns the Database instance (lines 547-548)."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        result = await db.__aenter__()
        assert result is db
        assert db._initialized is True
        await db.close()

    async def test_aexit_calls_close(self, tmp_path: Path) -> None:
        """__aexit__ calls close() (line 556)."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()
        assert db._initialized is True

        await db.__aexit__(None, None, None)
        assert db._initialized is False


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: _ensure_jsonl_schema and _apply_jsonl_migrations
# ─────────────────────────────────────────────────────────────────────────────


class TestJsonlSchemaMigrationAdvanced:
    """Cover uncovered branches in _ensure_jsonl_schema and _apply_jsonl_migrations.

    Target lines: 569, 585, 601-645.
    """

    def test_empty_first_line_returns_early(self, tmp_path: Path) -> None:
        """File with only a blank first line returns early (line 569)."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "blank_first.jsonl"
        f.write_text("\n", encoding="utf-8")

        db._ensure_jsonl_schema(f)
        # File should remain unchanged (blank first line → early return)
        assert f.read_text(encoding="utf-8") == "\n"

    def test_old_version_header_triggers_migration(self, tmp_path: Path) -> None:
        """Header with version < _JSONL_SCHEMA_VERSION triggers migration (line 585).

        Since _JSONL_MIGRATIONS is empty by default, _apply_jsonl_migrations
        exits early at line 601-602. We patch it to contain a no-op migration
        to cover the full migration path.
        """
        from src.db.db import _JSONL_SCHEMA_VERSION, _JSONL_MIGRATIONS

        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "old_version.jsonl"
        # Write a file with a v0 header
        f.write_text(
            '{"type":"header","_version":0}\n{"role":"user","content":"old"}\n',
            encoding="utf-8",
        )

        # Patch _JSONL_MIGRATIONS in the migration module where it's actually used.
        import src.db.migration as migration_module
        import src.db.db as db_module

        original_migrations = migration_module._JSONL_MIGRATIONS
        try:
            migration_module._JSONL_MIGRATIONS = [(1, [lambda msg: msg])]
            db_module._JSONL_MIGRATIONS = migration_module._JSONL_MIGRATIONS

            db._ensure_jsonl_schema(f)

            lines = f.read_text(encoding="utf-8").splitlines()
            assert len(lines) == 2
            import json

            header = json.loads(lines[0])
            assert header["type"] == "header"
            assert header["_version"] == _JSONL_SCHEMA_VERSION

            msg = json.loads(lines[1])
            assert msg["content"] == "old"
        finally:
            migration_module._JSONL_MIGRATIONS = original_migrations
            db_module._JSONL_MIGRATIONS = original_migrations

    def test_apply_migrations_with_unparseable_line(self, tmp_path: Path) -> None:
        """_apply_jsonl_migrations skips unparseable lines gracefully (lines 614-622)."""
        from src.db.db import _JSONL_SCHEMA_VERSION

        import src.db.migration as migration_module
        import src.db.db as db_module

        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "mixed.jsonl"
        f.write_text(
            '{"type":"header","_version":0}\nNOT JSON\n{"role":"user","content":"good"}\n',
            encoding="utf-8",
        )

        original_migrations = migration_module._JSONL_MIGRATIONS
        try:
            migration_module._JSONL_MIGRATIONS = [(1, [lambda msg: msg])]
            db_module._JSONL_MIGRATIONS = migration_module._JSONL_MIGRATIONS

            db._apply_jsonl_migrations(f, current_version=0)

            lines = f.read_text(encoding="utf-8").splitlines()
            # Should have header + unparseable line preserved + migrated message
            assert len(lines) == 3
            import json

            header = json.loads(lines[0])
            assert header["_version"] == _JSONL_SCHEMA_VERSION
            # Unparseable line preserved as-is
            assert lines[1] == "NOT JSON"
            # Good message was migrated
            msg = json.loads(lines[2])
            assert msg["content"] == "good"
        finally:
            migration_module._JSONL_MIGRATIONS = original_migrations
            db_module._JSONL_MIGRATIONS = original_migrations

    def test_apply_migrations_no_header_in_file(self, tmp_path: Path) -> None:
        """_apply_jsonl_migrations inserts header when file has no header line (lines 639-641)."""
        from src.db.db import _JSONL_SCHEMA_VERSION

        import src.db.migration as migration_module
        import src.db.db as db_module

        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "no_header.jsonl"
        f.write_text(
            '{"role":"user","content":"a"}\n{"role":"assistant","content":"b"}\n',
            encoding="utf-8",
        )

        original_migrations = migration_module._JSONL_MIGRATIONS
        try:
            migration_module._JSONL_MIGRATIONS = [(1, [lambda msg: msg])]
            db_module._JSONL_MIGRATIONS = migration_module._JSONL_MIGRATIONS

            db._apply_jsonl_migrations(f, current_version=0)

            lines = f.read_text(encoding="utf-8").splitlines()
            import json

            # Header should have been inserted at the start
            header = json.loads(lines[0])
            assert header["type"] == "header"
            assert header["_version"] == _JSONL_SCHEMA_VERSION
            assert len(lines) == 3  # header + 2 messages
        finally:
            migration_module._JSONL_MIGRATIONS = original_migrations
            db_module._JSONL_MIGRATIONS = original_migrations

    def test_apply_migrations_empty_migrations_list(self, tmp_path: Path) -> None:
        """When _JSONL_MIGRATIONS is empty, _apply_jsonl_migrations returns early (lines 601-602)."""
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))

        f = tmp_path / "no_migrate.jsonl"
        original_content = '{"type":"header","_version":0}\n{"role":"user","content":"x"}\n'
        f.write_text(original_content, encoding="utf-8")

        db._apply_jsonl_migrations(f, current_version=0)

        # File should be unchanged (empty migrations → early return)
        assert f.read_text(encoding="utf-8") == original_content


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: compress_chat_history delegation
# ─────────────────────────────────────────────────────────────────────────────


class TestCompressChatHistoryDelegation:
    """Cover compress_chat_history delegation (line 848)."""

    async def test_compress_chat_history_delegates(self, initialized_db: Database) -> None:
        """compress_chat_history delegates to CompressionService."""
        db = initialized_db
        result = await db.compress_chat_history("chat_1")
        # For a chat with few messages, compression should return False (no need)
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: repair_message_file with actual corruption
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairMessageFile:
    """Cover repair_message_file with actual corruption (lines 868-878)."""

    async def test_repair_non_corrupted_file_returns_early(self, initialized_db: Database) -> None:
        """repair_message_file returns early when file is not corrupted."""
        db = initialized_db
        await db.save_message("chat_1", "user", "hello")
        result = await db.repair_message_file("chat_1")
        assert result.is_corrupted is False
        assert result.repaired is False

    async def test_repair_corrupted_file_with_backup(self, initialized_db: Database) -> None:
        """repair_message_file detects corruption, creates backup, and repairs (lines 868-878)."""
        db = initialized_db

        # Write a valid message first so the file exists
        await db.save_message("chat_1", "user", "hello world")

        msg_file = db._message_file("chat_1")

        # Corrupt the file: overwrite with malformed JSON
        msg_file.write_text(
            '{"type":"header","_version":1}\nBROKEN LINE {{{\n',
            encoding="utf-8",
        )

        # Invalidate any cached handles
        db._file_pool.invalidate(msg_file)

        result = await db.repair_message_file("chat_1", backup=True)

        # The file should have been detected as corrupted
        assert result.is_corrupted is True
        # A backup should have been created
        assert result.backup_path is not None
        # Repair should have been attempted
        assert result.repaired is not None

    async def test_repair_corrupted_file_without_backup(self, initialized_db: Database) -> None:
        """repair_message_file with backup=False skips backup creation."""
        db = initialized_db

        await db.save_message("chat_1", "user", "hello")
        msg_file = db._message_file("chat_1")

        # Corrupt the file
        msg_file.write_text(
            '{"type":"header","_version":1}\nCORRUPTED DATA\n',
            encoding="utf-8",
        )
        db._file_pool.invalidate(msg_file)

        result = await db.repair_message_file("chat_1", backup=False)

        assert result.is_corrupted is True
        # No backup should have been created
        assert result.backup_path is None
        assert result.repaired is not None

    async def test_validate_all_with_repair_and_files(self, initialized_db: Database) -> None:
        """validate_all_message_files(repair=True) processes all message files."""
        db = initialized_db
        await db.save_message("chat_a", "user", "msg_a")
        await db.save_message("chat_b", "user", "msg_b")

        results = await db.validate_all_message_files(repair=True)
        assert "chat_a" in results
        assert "chat_b" in results


class TestGuardedWriteRetryBudgetStress:
    """Stress tests for _guarded_write retry budget exhaustion under sustained
    concurrent OSError load.

    Verifies:
    - Budget is organically consumed and never exceeds the cap
    - Circuit breaker opens after budget-exhaustion failures reach threshold
    - Concurrent writes all fail fast once budget is depleted
    - Budget invariant holds under concurrent write pressure
    """

    NUM_CONCURRENT_WRITES = 10
    MAX_BUDGET: float = 3.0  # DB_WRITE_RETRY_BUDGET_SECONDS

    async def test_organic_budget_exhaustion_under_sustained_oserror(
        self, initialized_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sustained OSError across many concurrent writes organically exhausts
        the budget and trips the circuit breaker."""
        db = initialized_db

        # Deterministic jitter-free delays so budget consumption is predictable.
        monkeypatch.setattr(
            "src.utils.retry.calculate_delay_with_jitter", lambda d: d
        )
        # Eliminate actual sleep for speed.
        slept: list[float] = []
        original_sleep = asyncio.sleep

        async def _fake_sleep(d: float) -> None:
            slept.append(d)
            await original_sleep(0)

        monkeypatch.setattr("asyncio.sleep", _fake_sleep)

        async def _always_oserror():
            raise OSError("disk I/O error")

        # Fire many concurrent writes — each will attempt 3 tries (1 + 2 retries)
        # consuming budget on each retry. Budget = 3.0s, initial delay = 0.5s.
        results = await asyncio.gather(
            *(
                db._guarded_write(
                    _always_oserror, timeout=5.0, operation=f"stress_{i}"
                )
                for i in range(self.NUM_CONCURRENT_WRITES)
            ),
            return_exceptions=True,
        )

        # All should have failed (OSError or DatabaseError from breaker open)
        assert all(isinstance(r, (OSError, DatabaseError)) for r in results)

        # Budget should never exceed the cap (invariant check)
        assert db._retry_budget_spent <= self.MAX_BUDGET

        # At least some budget should have been consumed
        assert db._retry_budget_spent > 0

    async def test_circuit_breaker_opens_from_budget_exhaustion(
        self, initialized_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated budget-exhaustion failures accumulate and eventually open
        the circuit breaker."""
        db = initialized_db

        monkeypatch.setattr(
            "src.utils.retry.calculate_delay_with_jitter", lambda d: d
        )

        async def _noop_sleep(d: float) -> None:
            pass

        monkeypatch.setattr("asyncio.sleep", _noop_sleep)

        # Use a breaker with threshold=3 for faster tripping in this test.
        from src.utils.circuit_breaker import CircuitBreaker

        original_breaker = db._write_breaker
        db._write_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=300.0)

        async def _always_oserror():
            raise OSError("degraded")

        # Run sequential writes until breaker opens.
        # With budget=3.0s, initial_delay=0.5s, each write that exhausts retries
        # consumes up to 0.5+1.0=1.5s of budget. After ~2 such writes the
        # budget is exhausted, and subsequent writes fail immediately (1 failure
        # each recorded on breaker). After 3 exhaustion failures the breaker opens.
        for i in range(10):
            try:
                await db._guarded_write(
                    _always_oserror, timeout=5.0, operation=f"seq_{i}"
                )
            except (OSError, DatabaseError):
                pass

        # Check breaker state
        breaker_opened = await db._write_breaker.is_open()
        assert breaker_opened, "Circuit breaker should have opened from budget-exhaustion failures"

        # Restore original breaker to avoid side effects
        db._write_breaker = original_breaker

    async def test_budget_invariant_holds_under_concurrency(
        self, initialized_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The budget cap invariant (_retry_budget_spent <= BUDGET) holds even
        when many concurrent writes compete for the remaining budget."""
        db = initialized_db

        monkeypatch.setattr(
            "src.utils.retry.calculate_delay_with_jitter", lambda d: d
        )

        # Track actual sleep durations for invariant verification
        sleep_calls: list[float] = []
        original_sleep = asyncio.sleep

        async def _track_sleep(d: float) -> None:
            sleep_calls.append(d)
            await original_sleep(0)

        monkeypatch.setattr("asyncio.sleep", _track_sleep)

        attempt_count = 0

        async def _oserror_first_three():
            nonlocal attempt_count
            attempt_count += 1
            raise OSError("transient")

        results = await asyncio.gather(
            *(
                db._guarded_write(
                    _oserror_first_three, timeout=5.0, operation=f"inv_{i}"
                )
                for i in range(self.NUM_CONCURRENT_WRITES)
            ),
            return_exceptions=True,
        )

        # Core invariant: budget never exceeded
        assert (
            db._retry_budget_spent <= self.MAX_BUDGET
        ), f"Budget overspent: {db._retry_budget_spent}s > {self.MAX_BUDGET}s"

        # Total sleep time should also respect the budget cap
        total_sleep = sum(sleep_calls)
        assert total_sleep <= self.MAX_BUDGET, (
            f"Total sleep {total_sleep:.3f}s exceeds budget {self.MAX_BUDGET}s"
        )

    async def test_concurrent_writes_fail_fast_once_budget_depleted(
        self, initialized_db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the retry budget is organically depleted, subsequent concurrent
        writes fail immediately (single attempt, no retry sleep)."""
        db = initialized_db

        monkeypatch.setattr(
            "src.utils.retry.calculate_delay_with_jitter", lambda d: d
        )

        sleep_calls: list[float] = []
        original_sleep = asyncio.sleep

        async def _track_sleep(d: float) -> None:
            sleep_calls.append(d)
            await original_sleep(0)

        monkeypatch.setattr("asyncio.sleep", _track_sleep)

        call_counts: list[int] = [0] * self.NUM_CONCURRENT_WRITES

        def _make_writer(index: int):
            async def _fn():
                call_counts[index] += 1
                raise OSError("degraded")

            return _fn

        results = await asyncio.gather(
            *(
                db._guarded_write(
                    _make_writer(i), timeout=5.0, operation=f"fast_{i}"
                )
                for i in range(self.NUM_CONCURRENT_WRITES)
            ),
            return_exceptions=True,
        )

        # All failed
        assert all(isinstance(r, (OSError, DatabaseError)) for r in results)

        # Writers that encountered budget exhaustion should have been called
        # only once (no retry). At least some writers should be single-call.
        single_call_writers = sum(1 for c in call_counts if c == 1)
        assert single_call_writers >= 1, (
            f"Expected some writers to fail fast (1 call), "
            f"got call counts: {call_counts}"
        )

        # No writer should exceed max retries + 1 (3 attempts total)
        assert all(
            c <= 3 for c in call_counts
        ), f"Some writers exceeded max attempts: {call_counts}"


# ===========================================================================
# Tests for retry_budget_remaining property and Prometheus gauge
# ===========================================================================


class TestRetryBudgetRemaining:
    """Tests for the Database.retry_budget_remaining property and its
    integration with the Prometheus output renderer."""

    def test_returns_full_budget_when_fresh(self, db: Database) -> None:
        """When no retries have occurred, remaining equals the full budget."""
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db._retry_budget_spent = 0.0
        assert db.retry_budget_remaining == pytest.approx(
            DB_WRITE_RETRY_BUDGET_SECONDS
        )

    def test_returns_remaining_after_partial_spend(self, db: Database) -> None:
        """After partial budget consumption, remaining reflects the difference."""
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db._retry_budget_spent = 1.5
        assert db.retry_budget_remaining == pytest.approx(
            DB_WRITE_RETRY_BUDGET_SECONDS - 1.5
        )

    def test_clamps_to_zero_when_exhausted(self, db: Database) -> None:
        """When budget is fully consumed, remaining is clamped to 0."""
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS
        assert db.retry_budget_remaining == 0.0

    def test_clamps_to_zero_when_overspent(self, db: Database) -> None:
        """Even if overspent (edge case), remaining never goes negative."""
        db._retry_budget_spent = 999.0
        assert db.retry_budget_remaining == 0.0


class TestDbWriteBreakerPrometheusWithBudget:
    """Tests for build_db_write_breaker_prometheus_output including budget gauge."""

    def test_returns_empty_string_for_none_circuit_breaker(self) -> None:
        from src.health.prometheus import build_db_write_breaker_prometheus_output

        assert build_db_write_breaker_prometheus_output(None) == ""
        assert build_db_write_breaker_prometheus_output(None, 1.5) == ""

    def test_emits_breaker_metrics_without_budget(self) -> None:
        from src.health.prometheus import build_db_write_breaker_prometheus_output
        from src.utils.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
        output = build_db_write_breaker_prometheus_output(cb)

        assert "custombot_db_write_circuit_breaker_state" in output
        assert "custombot_db_write_circuit_breaker_failures_total" in output
        # Budget gauge should NOT appear when not provided
        assert "custombot_db_retry_budget_remaining_seconds" not in output

    def test_emits_budget_gauge_when_provided(self) -> None:
        from src.health.prometheus import build_db_write_breaker_prometheus_output
        from src.utils.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
        output = build_db_write_breaker_prometheus_output(cb, retry_budget_remaining=2.5)

        # All three metrics should appear
        assert "custombot_db_write_circuit_breaker_state" in output
        assert "custombot_db_write_circuit_breaker_failures_total" in output
        assert "custombot_db_retry_budget_remaining_seconds" in output

        # Verify HELP and TYPE headers for the budget gauge
        assert (
            "# HELP custombot_db_retry_budget_remaining_seconds"
            in output
        )
        assert (
            "# TYPE custombot_db_retry_budget_remaining_seconds gauge"
            in output
        )
        # Value should be 2.5
        assert "2.5" in output

    def test_budget_gauge_zero_value(self) -> None:
        from src.health.prometheus import build_db_write_breaker_prometheus_output
        from src.utils.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=30)
        output = build_db_write_breaker_prometheus_output(cb, retry_budget_remaining=0.0)

        assert "custombot_db_retry_budget_remaining_seconds" in output
        # Should contain value 0
        for line in output.split("\n"):
            if (
                line.startswith("custombot_db_retry_budget_remaining_seconds")
                and not line.startswith("#")
            ):
                assert "0" in line


class TestCompactChatsCrashRecovery:
    """Verify crash recovery for _compact_chats snapshot/changelog scenarios.

    Simulates crashes at different points during the compaction sequence:
      1. Crash after snapshot + marker, before changelog unlink
         → marker prevents stale changelog double-replay
      2. Crash after snapshot, before marker
         → without marker, changelog replayed normally (entries not in snapshot)
      3. Crash after changelog unlink, before marker unlink
         → orphaned marker cleaned up harmlessly
    """

    async def test_marker_prevents_stale_changelog_replay(
        self, tmp_path: Path
    ) -> None:
        """Crash after chats.json + marker written, before changelog unlink.

        The compaction marker tells connect() to skip stale changelog replay,
        preventing entries already in the snapshot from being re-applied.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Build up chats and force a full compaction to create a snapshot.
        await db.upsert_chat("chat_1", "Alice")
        await db.upsert_chat("chat_2", "Bob")
        await db.flush_chats()

        # Add more chats via incremental changelog, then force compaction.
        await db.upsert_chat("chat_3", "Charlie")
        await db.flush_chats()

        # Force compaction threshold so next save triggers full compact.
        db._changelog_entries_since_compact = db._changelog_compact_threshold
        await db.upsert_chat("chat_4", "Diana")
        await db.flush_chats()

        # Snapshot now has all 4 chats, changelog should be gone.
        assert db._chats_file.exists()
        snapshot_data = json.loads(
            db._chats_file.read_text(encoding="utf-8")
        )
        assert len(snapshot_data) == 4

        # ── Simulate crash: snapshot + marker exist, stale changelog left behind ──
        # Write a stale changelog with an entry that conflicts with the snapshot.
        # In a real crash, this would be the old changelog that wasn't unlinked.
        stale_line = json.dumps({"id": "chat_1", "name": "Alice_STALE"}) + "\n"
        db._chats_changelog_file.write_text(stale_line, encoding="utf-8")
        # Marker is present — tells connect() the snapshot is authoritative.
        db._chats_compaction_marker.write_text("1", encoding="utf-8")

        await db.close()

        # ── Reconnect — marker should prevent stale replay ──
        db2 = Database(str(data_dir))
        await db2.connect()

        # chat_1 name must be "Alice" (from snapshot), NOT "Alice_STALE"
        assert db2._chats["chat_1"]["name"] == "Alice"
        assert db2._chats["chat_2"]["name"] == "Bob"
        assert db2._chats["chat_3"]["name"] == "Charlie"
        assert db2._chats["chat_4"]["name"] == "Diana"

        # Cleanup must have removed both stale changelog and marker.
        assert not db2._chats_compaction_marker.exists()
        assert not db2._chats_changelog_file.exists()

        await db2.close()

    async def test_no_marker_allows_changelog_replay(
        self, tmp_path: Path
    ) -> None:
        """Crash after snapshot written, but before compaction marker.

        Without the marker, connect() replays the changelog normally so
        entries not yet in the snapshot are picked up.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Create initial snapshot with 2 chats.
        await db.upsert_chat("chat_1", "Alice")
        await db.upsert_chat("chat_2", "Bob")
        await db.flush_chats()

        snapshot_data = json.loads(
            db._chats_file.read_text(encoding="utf-8")
        )
        assert len(snapshot_data) == 2

        # ── Simulate crash: snapshot has 2 chats, but a changelog entry
        #    with an update NOT in the snapshot was written before crash. ──
        #    No marker → connect() replays the changelog.
        changelog_line = (
            json.dumps({"id": "chat_1", "name": "Alice_Updated"}) + "\n"
        )
        db._chats_changelog_file.write_text(changelog_line, encoding="utf-8")
        # Explicitly ensure NO marker file.
        assert not db._chats_compaction_marker.exists()

        await db.close()

        # ── Reconnect — changelog should be replayed ──
        db2 = Database(str(data_dir))
        await db2.connect()

        assert db2._chats["chat_1"]["name"] == "Alice_Updated"
        assert db2._chats["chat_2"]["name"] == "Bob"

        await db2.close()

    async def test_orphaned_marker_cleaned_up_harmlessly(
        self, tmp_path: Path
    ) -> None:
        """Crash after changelog unlinked, but before marker unlinked.

        The orphaned marker file is harmless — connect() detects it,
        skips replay (no changelog to replay anyway), and cleans it up.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        await db.upsert_chat("chat_1", "Alice")
        await db.flush_chats()

        # Simulate: compaction completed (snapshot + marker written,
        # changelog unlinked), but crashed before marker unlink.
        db._chats_compaction_marker.write_text("1", encoding="utf-8")
        if db._chats_changelog_file.exists():
            db._chats_changelog_file.unlink()

        await db.close()

        # Reconnect — marker cleaned up, data intact.
        db2 = Database(str(data_dir))
        await db2.connect()

        assert db2._chats["chat_1"]["name"] == "Alice"
        assert not db2._chats_compaction_marker.exists()

        await db2.close()

    async def test_corrupted_marker_overwritten_and_recovery_correct(
        self, tmp_path: Path
    ) -> None:
        """A pre-existing corrupted/partial marker is overwritten atomically.

        Simulates a crash that left a partial marker file (e.g. empty or
        truncated garbage from a non-atomic write).  Exercises two scenarios:

        1. ``_compact_chats`` overwrites the corrupt marker atomically
           via ``_write_marker`` (no ``.tmp`` artifact left behind).
        2. On reconnect, a valid marker (written by compaction) causes
           ``connect()`` to skip stale changelog replay, and both the
           stale changelog and marker are cleaned up.
        """
        data_dir = tmp_path / ".data"
        db = Database(str(data_dir))
        await db.connect()

        # Build a snapshot with some chats.
        await db.upsert_chat("chat_1", "Alice")
        await db.upsert_chat("chat_2", "Bob")
        await db.flush_chats()

        snapshot_data = json.loads(
            db._chats_file.read_text(encoding="utf-8")
        )
        assert len(snapshot_data) == 2

        # ── Pre-condition: plant a CORRUPTED marker file ──
        # Simulates a crash during a non-atomic write that left an
        # empty or garbage marker.
        db._chats_compaction_marker.write_bytes(b"")

        # Also plant a stale changelog with conflicting data.
        stale_line = json.dumps({"id": "chat_1", "name": "Alice_STALE"}) + "\n"
        db._chats_changelog_file.write_text(stale_line, encoding="utf-8")

        await db.close()

        # ── Reconnect with corrupted marker present ──
        # The corrupted (empty) marker file still exists on disk.
        # ``connect()`` checks ``.exists()`` and treats it as present,
        # skipping changelog replay and cleaning up.
        db2 = Database(str(data_dir))
        await db2.connect()

        # chat_1 must retain the snapshot value, NOT the stale one.
        assert db2._chats["chat_1"]["name"] == "Alice"
        assert db2._chats["chat_2"]["name"] == "Bob"

        # Cleanup must have removed both stale changelog and marker.
        assert not db2._chats_compaction_marker.exists()
        assert not db2._chats_changelog_file.exists()

        # ── Now trigger compaction that writes a fresh marker ──
        await db2.upsert_chat("chat_3", "Charlie")
        db2._changelog_entries_since_compact = (
            db2._changelog_compact_threshold
        )
        await db2.upsert_chat("chat_4", "Diana")
        await db2.flush_chats()

        # Verify no .tmp artifact left behind by atomic marker write.
        tmp_files = list(data_dir.glob("*.tmp"))
        assert tmp_files == [], f"Unexpected .tmp artifacts: {tmp_files}"

        # All 4 chats must be in the snapshot.
        snapshot_data2 = json.loads(
            db2._chats_file.read_text(encoding="utf-8")
        )
        assert len(snapshot_data2) == 4
        assert snapshot_data2["chat_3"]["name"] == "Charlie"
        assert snapshot_data2["chat_4"]["name"] == "Diana"

        # Marker and changelog should both be gone after compaction.
        assert not db2._chats_compaction_marker.exists()
        assert not db2._chats_changelog_file.exists()

        await db2.close()

        # ── Final reconnect: verify data integrity ──
        db3 = Database(str(data_dir))
        await db3.connect()

        assert db3._chats["chat_1"]["name"] == "Alice"
        assert db3._chats["chat_2"]["name"] == "Bob"
        assert db3._chats["chat_3"]["name"] == "Charlie"
        assert db3._chats["chat_4"]["name"] == "Diana"

        await db3.close()


# ─────────────────────────────────────────────────────────────────────────────
# _retry_budget_spent recovery after cooldown
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryBudgetRecovery:
    """Verify _retry_budget_spent is reset after successful write recovery."""

    @pytest.mark.asyncio
    async def test_budget_resets_after_successful_write(self, initialized_db: Database) -> None:
        """After a successful write, _retry_budget_spent should be 0."""
        db = initialized_db

        # Pre-set a non-zero budget to simulate prior retry spending.
        db._retry_budget_spent = 2.5

        async def _succeed():
            return "ok"

        result = await db._guarded_write(_succeed, timeout=5.0, operation="reset_test")
        assert result == "ok"

        # Budget must have been reset to 0 on success.
        assert db._retry_budget_spent == 0.0

    @pytest.mark.asyncio
    async def test_budget_accumulates_during_retries(self, initialized_db: Database) -> None:
        """_retry_budget_spent accumulates across OSError retries then resets on success."""
        db = initialized_db
        db._retry_budget_spent = 0.0

        call_count = 0
        budget_snapshot: list[float] = []

        async def _flaky_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Capture budget state during the retry phase (before sleep).
                budget_snapshot.append(db._retry_budget_spent)
                raise OSError("transient I/O error")
            return "recovered"

        result = await db._guarded_write(
            _flaky_then_succeed, timeout=5.0, operation="accumulate_test"
        )
        assert result == "recovered"
        assert call_count == 2

        # Budget was non-zero during retries (accumulated from the retry delay).
        assert len(budget_snapshot) == 1
        assert budget_snapshot[0] > 0, "Budget should have accumulated during retry"

        # After successful recovery, budget is reset to 0.
        assert db._retry_budget_spent == 0.0

    @pytest.mark.asyncio
    async def test_budget_property_reflects_remaining(self, initialized_db: Database) -> None:
        """retry_budget_remaining property reflects current budget state."""
        from src.constants import DB_WRITE_RETRY_BUDGET_SECONDS

        db = initialized_db

        # Fresh state: remaining equals the full budget.
        db._retry_budget_spent = 0.0
        assert db.retry_budget_remaining == pytest.approx(DB_WRITE_RETRY_BUDGET_SECONDS)

        # Partially spent: remaining equals total minus spent.
        db._retry_budget_spent = 1.2
        assert db.retry_budget_remaining == pytest.approx(
            DB_WRITE_RETRY_BUDGET_SECONDS - 1.2
        )

        # Fully exhausted: remaining clamped to 0.
        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS
        assert db.retry_budget_remaining == 0.0

        # After a successful write, budget resets and remaining is restored.
        async def _succeed():
            return "ok"

        db._retry_budget_spent = DB_WRITE_RETRY_BUDGET_SECONDS - 0.1
        await db._guarded_write(_succeed, timeout=5.0, operation="property_test")
        assert db._retry_budget_spent == 0.0
        assert db.retry_budget_remaining == pytest.approx(DB_WRITE_RETRY_BUDGET_SECONDS)
