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
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from src.db.db import Database, _FileHandlePool, _sanitize_name
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
            assert len(db._chat_generations) <= MAX_CHAT_GENERATIONS

        # (b) Recently-written chats survive LRU eviction.
        # With quarter-eviction, the last ~MAX_CHAT_GENERATIONS chats remain.
        surviving_start = total_chats - MAX_CHAT_GENERATIONS
        for i in range(surviving_start, total_chats):
            chat_id = f"chat_{i:08d}"
            assert chat_id in db._chat_generations, (
                f"Recent {chat_id} should survive eviction"
            )

        # (c) Early chats were evicted — get_generation() returns 0 without error.
        for i in range(0, MAX_CHAT_GENERATIONS, 100):
            chat_id = f"chat_{i:08d}"
            assert db.get_generation(chat_id) == 0, (
                f"Evicted {chat_id} should return generation 0"
            )


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
            db.write_breaker.record_failure()
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
            db.write_breaker.record_failure()
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
            db.write_breaker.record_failure()
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
            db.write_breaker.record_failure()
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
            db.write_breaker.record_failure()
        assert db.write_breaker.state == CircuitState.OPEN

        # Reads should still succeed — the write breaker doesn't affect them.
        messages = await db.get_recent_messages("chat_1", limit=10)
        assert len(messages) == 1

    async def test_breaker_threading_lock_from_thread_context(
        self, initialized_db: Database
    ) -> None:
        """CircuitBreaker uses threading.Lock so it works from thread contexts.

        Regression test for the lock-model mismatch: the breaker must be
        callable from ``asyncio.to_thread()`` workers where no event loop
        is available.  If ``asyncio.Lock`` were used, calling ``is_open()``
        from a thread would raise ``RuntimeError``.
        """
        import threading

        from src.utils.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)

        # Verify the internal lock is a threading.Lock, not asyncio.Lock.
        assert isinstance(cb._lock, type(threading.Lock()))

        errors: list[str] = []

        def _call_from_thread():
            """Exercise all breaker methods without an event loop."""
            try:
                # is_open() must work in a plain thread
                assert cb.is_open() is False

                # record_failure() must work in a plain thread
                for _ in range(3):
                    cb.record_failure()
                assert cb.is_open() is True  # threshold hit → OPEN

                # record_success() must work in a plain thread
                # Manually transition to HALF_OPEN so success closes it
                cb._state = cb._state.__class__("half_open")
                cb.record_success()
                assert cb.state == CircuitState.CLOSED
            except Exception as exc:
                errors.append(str(exc))

        # Run breaker operations in a plain thread (no event loop).
        t = threading.Thread(target=_call_from_thread)
        t.start()
        t.join(timeout=5)

        assert not errors, f"Breaker failed from thread context: {errors}"

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
            db.write_breaker.record_failure()
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

    # Strategy: lines of printable text (no newlines within a line)
    _line_text = st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
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
        non_empty = [l for l in raw_lines if l.strip()]
        selected = non_empty[-limit:] if len(non_empty) > limit else non_empty
        # Small-file deque path keeps trailing \n; only strips \r
        return [line + "\n" for line in selected]

    @staticmethod
    def _expected_lines_large(raw_lines: list[str], limit: int) -> list[str]:
        """Expected output for large-file (reverse-seek) path.

        The reverse-seek path uses splitlines() + final rstrip("\\r"),
        so lines have NO trailing newline.
        """
        non_empty = [l for l in raw_lines if l.strip()]
        selected = non_empty[-limit:] if len(non_empty) > limit else non_empty
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
        file_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=limit)
        assert result == self._expected_lines(lines, limit)

    @given(
        lines=st.lists(_line_text, min_size=0, max_size=30),
    )
    @_hs
    def test_file_smaller_than_limit_returns_all(
        self, tmp_path: Path, lines: list[str]
    ) -> None:
        """When file has fewer lines than limit, all non-empty lines are returned."""
        file_path = tmp_path / "short.jsonl"
        file_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=100)
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
        result = db._read_file_lines(file_path, limit=count)
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
        file_path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=limit)
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
        lines = [f"line_{i:06d}_" + "x" * 90 for i in range(line_count)]
        content = "\n".join(lines) + "\n"

        file_path = tmp_path / "large.jsonl"
        file_path.write_bytes(content.encode("utf-8"))

        # Verify we actually crossed the 64KB threshold
        assert file_path.stat().st_size >= 65_536

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=limit)
        assert result == self._expected_lines_large(lines, limit)

    @given(
        total_lines=st.integers(min_value=100, max_value=800),
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

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=limit)

        expected = self._expected_lines_large(lines, limit)
        # Verify ordering: each line should sort before the next
        for i in range(len(result) - 1):
            assert result[i] < result[i + 1], (
                f"Lines not in chronological order at index {i}"
            )
        assert result == expected

    # ── Deterministic edge-case tests ──────────────────────────────────

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        """Empty file returns an empty list."""
        file_path = tmp_path / "empty.jsonl"
        file_path.write_bytes(b"")

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        assert result == []

    def test_file_with_only_newlines(self, tmp_path: Path) -> None:
        """File containing only newlines returns newline-only lines (deque path)."""
        file_path = tmp_path / "blank.jsonl"
        file_path.write_text("\n\n\n\n\n", encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        # The small-file deque path yields raw lines including \n
        assert result == ["\n"] * 5

    def test_file_with_trailing_newline(self, tmp_path: Path) -> None:
        """Trailing newline produces lines with trailing \\n (deque path)."""
        lines = ["alpha", "beta", "gamma"]
        file_path = tmp_path / "trailing.jsonl"
        file_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        # deque path: lines include trailing \n, only \r is stripped
        assert result == [l + "\n" for l in lines]

    def test_single_line_no_newline(self, tmp_path: Path) -> None:
        """A single line without trailing newline is returned correctly."""
        file_path = tmp_path / "single.jsonl"
        file_path.write_bytes(b"only_line_here")

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        assert result == ["only_line_here"]

    def test_single_line_with_newline(self, tmp_path: Path) -> None:
        """A single line with trailing newline — deque keeps the \\n."""
        file_path = tmp_path / "single_nl.jsonl"
        file_path.write_bytes(b"only_line_here\n")

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        assert result == ["only_line_here\n"]

    # ── Large-file edge-case tests ──────────────────────────────────────

    def test_large_file_no_newlines_falls_back_to_deque(
        self, tmp_path: Path
    ) -> None:
        """Large corrupted file (>64KB, no newlines) triggers _MAX_SEEK_ITERATIONS
        fallback and returns the single long line via the deque path."""
        # Build a file >64KB with no newlines
        line_content = "x" * 70_000
        file_path = tmp_path / "corrupted.jsonl"
        file_path.write_bytes(line_content.encode("utf-8"))
        assert file_path.stat().st_size >= 65_536

        db = Database(str(tmp_path / ".data"))
        result = db._read_file_lines(file_path, limit=10)
        # Falls back to deque which yields the single line, rstrip("\r")
        assert result == [line_content]

    @given(
        # Lines that produce a file right around the 64KB boundary
        line_count=st.integers(min_value=300, max_value=700),
        limit=st.integers(min_value=1, max_value=50),
    )
    @_hs
    def test_boundary_near_64kb(
        self, tmp_path: Path, line_count: int, limit: int
    ) -> None:
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
        result = db._read_file_lines(file_path, limit=limit)
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
        result = db._read_file_lines(file_path, limit=limit)

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
        result = db._read_file_lines(file_path, limit=5)
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

    def test_different_chat_ids_return_different_paths(
        self, db: Database
    ) -> None:
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

    def test_invalid_chat_id_empty_raises_before_caching(
        self, db: Database
    ) -> None:
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

    def test_whitespace_only_sanitized_and_cached(self, db: Database) -> None:
        """Whitespace-only chat_id is sanitized to underscores and cached."""
        path = db._message_file("   ")
        assert path.name.endswith(".jsonl")
        assert "   " in db._message_file_cache

    def test_valid_chat_id_cached_after_first_call(
        self, db: Database
    ) -> None:
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

    def test_repeated_access_refreshes_lru_position(
        self, db: Database
    ) -> None:
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

    def test_oserror_persistent_raises_database_error(
        self, tmp_path: Path
    ) -> None:
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
