"""Tests for src/core/dedup.py — Unified DeduplicationService.

Covers:
  - Inbound dedup (message-id) hit/miss stats
  - Outbound dedup (content-hash) TTL-based hit/miss stats
  - DedupStats snapshot and serialization
  - Prometheus output function for dedup metrics
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.core.dedup import DedupStats, DeduplicationService, outbound_key
from src.exceptions import DatabaseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(message_exists_return: bool = False) -> AsyncMock:
    """Create a minimal mock Storage with ``message_exists``."""
    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=message_exists_return)
    db.batch_message_exists = AsyncMock(return_value={})
    return db


# ===========================================================================
# outbound_key — standalone hash function
# ===========================================================================


class TestOutboundKey:
    """Tests for the module-level ``outbound_key`` hash function."""

    def test_deterministic(self) -> None:
        """Same inputs always produce the same key."""
        assert outbound_key("chat_1", "hello") == outbound_key("chat_1", "hello")

    def test_different_inputs_produce_different_keys(self) -> None:
        assert outbound_key("chat_1", "hello") != outbound_key("chat_1", "world")
        assert outbound_key("chat_1", "hello") != outbound_key("chat_2", "hello")

    def test_returns_hex_string(self) -> None:
        key = outbound_key("chat_1", "text")
        assert isinstance(key, str)
        assert len(key) == 16  # xxHash xxh64 hex digest length

    def test_empty_text(self) -> None:
        """Empty text still produces a valid key."""
        key = outbound_key("chat_1", "")
        assert len(key) == 16


# ===========================================================================
# DedupStats
# ===========================================================================


class TestDedupStats:
    """Tests for the DedupStats dataclass."""

    def test_defaults_are_zero(self) -> None:
        stats = DedupStats()
        assert stats.inbound_hits == 0
        assert stats.inbound_misses == 0
        assert stats.outbound_hits == 0
        assert stats.outbound_misses == 0

    def test_to_dict_round_trip(self) -> None:
        stats = DedupStats(inbound_hits=5, inbound_misses=10, outbound_hits=2, outbound_misses=8)
        d = stats.to_dict()
        assert d == {
            "inbound_hits": 5,
            "inbound_misses": 10,
            "outbound_hits": 2,
            "outbound_misses": 8,
            "request_hits": 0,
            "request_misses": 0,
        }


# ===========================================================================
# Inbound dedup
# ===========================================================================


class TestInboundDedup:
    """Tests for message-id–based inbound dedup."""

    @pytest.mark.asyncio
    async def test_miss_when_new_message(self) -> None:
        db = _make_db(message_exists_return=False)
        dedup = DeduplicationService(db=db)
        assert await dedup.is_inbound_duplicate("msg_001") is False
        db.message_exists.assert_awaited_once_with("msg_001")
        assert dedup.stats.inbound_misses == 1
        assert dedup.stats.inbound_hits == 0

    @pytest.mark.asyncio
    async def test_hit_when_duplicate_message(self) -> None:
        db = _make_db(message_exists_return=True)
        dedup = DeduplicationService(db=db)
        assert await dedup.is_inbound_duplicate("msg_001") is True
        assert dedup.stats.inbound_hits == 1
        assert dedup.stats.inbound_misses == 0

    @pytest.mark.asyncio
    async def test_stats_accumulate_across_calls(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        # First call: miss
        db.message_exists.return_value = False
        await dedup.is_inbound_duplicate("msg_001")
        # Second call: hit
        db.message_exists.return_value = True
        await dedup.is_inbound_duplicate("msg_002")
        stats = dedup.stats
        assert stats.inbound_misses == 1
        assert stats.inbound_hits == 1

    @pytest.mark.asyncio
    async def test_stats_snapshot_is_copy(self) -> None:
        """The ``stats`` property returns a snapshot — mutation doesn't affect service."""
        db = _make_db(message_exists_return=False)
        dedup = DeduplicationService(db=db)
        await dedup.is_inbound_duplicate("msg_001")
        snap = dedup.stats
        assert snap.inbound_misses == 1
        # Mutating the snapshot doesn't affect the service
        snap.inbound_misses = 999
        assert dedup.stats.inbound_misses == 1

    @pytest.mark.asyncio
    async def test_db_failure_returns_false(self) -> None:
        """Graceful degradation: DB failure allows message through."""
        db = _make_db()
        db.message_exists.side_effect = DatabaseError("disk I/O failure")
        dedup = DeduplicationService(db=db)

        result = await dedup.is_inbound_duplicate("msg_001")

        assert result is False
        # Stats should NOT be updated on failure — no hit or miss recorded
        assert dedup.stats.inbound_hits == 0
        assert dedup.stats.inbound_misses == 0

    @pytest.mark.asyncio
    async def test_db_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """DB failure logs a warning with message ID."""
        db = _make_db()
        db.message_exists.side_effect = DatabaseError("disk I/O failure")
        dedup = DeduplicationService(db=db)

        with caplog.at_level("WARNING", logger="src.core.dedup"):
            await dedup.is_inbound_duplicate("msg_001")

        assert any("Dedup DB lookup failed" in r.message for r in caplog.records)
        assert any("msg_001" in r.message for r in caplog.records)

    # ── Inbound LRU cache tests ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_db_call(self) -> None:
        """Cached duplicate result short-circuits the async DB call."""
        db = _make_db(message_exists_return=True)
        dedup = DeduplicationService(db=db)

        # First call: DB is queried, result cached.
        result1 = await dedup.is_inbound_duplicate("msg_001")
        assert result1 is True
        assert db.message_exists.await_count == 1

        # Second call: cache hit — DB should NOT be queried again.
        result2 = await dedup.is_inbound_duplicate("msg_001")
        assert result2 is True
        assert db.message_exists.await_count == 1  # unchanged
        assert dedup.stats.inbound_hits == 2

    @pytest.mark.asyncio
    async def test_cache_miss_avoids_db_call(self) -> None:
        """Cached non-duplicate result short-circuits the async DB call."""
        db = _make_db(message_exists_return=False)
        dedup = DeduplicationService(db=db)

        # First call: DB queried, False cached.
        result1 = await dedup.is_inbound_duplicate("msg_001")
        assert result1 is False
        assert db.message_exists.await_count == 1

        # Second call: cache hit — DB should NOT be queried again.
        result2 = await dedup.is_inbound_duplicate("msg_001")
        assert result2 is False
        assert db.message_exists.await_count == 1  # unchanged
        assert dedup.stats.inbound_misses == 2

    @pytest.mark.asyncio
    async def test_cache_does_not_store_db_failure(self) -> None:
        """DB failures are NOT cached — next call retries the DB."""
        db = _make_db()
        call_count = 0

        async def _fail_then_succeed(msg_id: str) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise DatabaseError("transient")
            return True

        db.message_exists.side_effect = _fail_then_succeed
        dedup = DeduplicationService(db=db)

        # First call: DB failure — returns False, NOT cached.
        result1 = await dedup.is_inbound_duplicate("msg_001")
        assert result1 is False

        # Second call: DB succeeds — result IS cached.
        result2 = await dedup.is_inbound_duplicate("msg_001")
        assert result2 is True

        # Third call: cache hit — DB not called again.
        result3 = await dedup.is_inbound_duplicate("msg_001")
        assert result3 is True
        assert db.message_exists.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_bounded_by_max_size(self) -> None:
        """Cache evicts oldest entries when max_size is exceeded."""
        db = _make_db(message_exists_return=False)
        dedup = DeduplicationService(db=db, inbound_cache_max_size=3)

        # Fill cache with 3 entries.
        await dedup.is_inbound_duplicate("msg_001")
        await dedup.is_inbound_duplicate("msg_002")
        await dedup.is_inbound_duplicate("msg_003")
        assert db.message_exists.await_count == 3

        # Adding a 4th entry evicts the oldest (msg_001).
        await dedup.is_inbound_duplicate("msg_004")
        assert db.message_exists.await_count == 4

        # msg_001 is no longer cached → DB queried again.
        await dedup.is_inbound_duplicate("msg_001")
        assert db.message_exists.await_count == 5

        # msg_003 is still cached (was the newest before msg_004) → no DB query.
        await dedup.is_inbound_duplicate("msg_003")
        assert db.message_exists.await_count == 5  # unchanged

    @pytest.mark.asyncio
    async def test_cache_respects_ttl(self) -> None:
        """Cache entries expire after the configured TTL."""
        db = _make_db(message_exists_return=False)
        dedup = DeduplicationService(db=db, inbound_cache_ttl=0.01)  # 10ms

        await dedup.is_inbound_duplicate("msg_001")
        assert db.message_exists.await_count == 1

        # Immediately re-check: cache hit.
        await dedup.is_inbound_duplicate("msg_001")
        assert db.message_exists.await_count == 1  # unchanged

        # Wait for TTL to expire.
        import asyncio

        await asyncio.sleep(0.05)

        # Cache expired → DB queried again.
        await dedup.is_inbound_duplicate("msg_001")
        assert db.message_exists.await_count == 2

    @pytest.mark.asyncio
    async def test_different_message_ids_independent(self) -> None:
        """Each message_id has its own cache entry."""
        db = _make_db()
        dedup = DeduplicationService(db=db)

        db.message_exists.return_value = False
        await dedup.is_inbound_duplicate("msg_001")
        db.message_exists.return_value = True
        await dedup.is_inbound_duplicate("msg_002")
        assert db.message_exists.await_count == 2

        # Both cached independently.
        db.message_exists.return_value = False  # would give wrong answer if DB queried
        result1 = await dedup.is_inbound_duplicate("msg_001")
        result2 = await dedup.is_inbound_duplicate("msg_002")
        assert result1 is False  # cached from first call
        assert result2 is True  # cached from second call
        assert db.message_exists.await_count == 2  # no additional DB calls


# ===========================================================================
# Batch inbound dedup
# ===========================================================================


class TestBatchCheckInbound:
    """Tests for batch_check_inbound — batch message-id dedup."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        assert await dedup.batch_check_inbound([]) == {}
        db.batch_message_exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_new_messages(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(
            return_value={"msg_001": False, "msg_002": False, "msg_003": False}
        )
        dedup = DeduplicationService(db=db)
        result = await dedup.batch_check_inbound(["msg_001", "msg_002", "msg_003"])
        assert result == {"msg_001": False, "msg_002": False, "msg_003": False}
        db.batch_message_exists.assert_awaited_once_with(["msg_001", "msg_002", "msg_003"])
        assert dedup.stats.inbound_misses == 3

    @pytest.mark.asyncio
    async def test_all_duplicates(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(
            return_value={"msg_001": True, "msg_002": True}
        )
        dedup = DeduplicationService(db=db)
        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])
        assert result == {"msg_001": True, "msg_002": True}
        assert dedup.stats.inbound_hits == 2

    @pytest.mark.asyncio
    async def test_mixed_results(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(
            return_value={"msg_001": True, "msg_002": False}
        )
        dedup = DeduplicationService(db=db)
        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])
        assert result == {"msg_001": True, "msg_002": False}
        assert dedup.stats.inbound_hits == 1
        assert dedup.stats.inbound_misses == 1

    @pytest.mark.asyncio
    async def test_cached_ids_skip_db_call(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(return_value={})
        dedup = DeduplicationService(db=db)

        # Pre-populate cache via single lookup
        db.message_exists.return_value = True
        await dedup.is_inbound_duplicate("msg_001")

        # batch_check should use cache for msg_001, only query DB for msg_002
        db.batch_message_exists = AsyncMock(return_value={"msg_002": False})
        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])

        assert result == {"msg_001": True, "msg_002": False}
        # Only msg_002 passed to batch DB call
        db.batch_message_exists.assert_awaited_once_with(["msg_002"])

    @pytest.mark.asyncio
    async def test_all_cached_skips_db_entirely(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)

        # Pre-populate cache
        db.message_exists.return_value = True
        await dedup.is_inbound_duplicate("msg_001")
        await dedup.is_inbound_duplicate("msg_002")

        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])
        assert result == {"msg_001": True, "msg_002": True}
        db.batch_message_exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_failure_returns_all_false(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(side_effect=DatabaseError("disk failure"))
        dedup = DeduplicationService(db=db)

        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])
        assert result == {"msg_001": False, "msg_002": False}
        # No stats updated on failure
        assert dedup.stats.inbound_hits == 0
        assert dedup.stats.inbound_misses == 0

    @pytest.mark.asyncio
    async def test_db_failure_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(side_effect=DatabaseError("disk failure"))
        dedup = DeduplicationService(db=db)

        with caplog.at_level("WARNING", logger="src.core.dedup"):
            await dedup.batch_check_inbound(["msg_001"])

        assert any("batch DB lookup failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_results_are_cached(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(
            return_value={"msg_001": True, "msg_002": False}
        )
        dedup = DeduplicationService(db=db)

        await dedup.batch_check_inbound(["msg_001", "msg_002"])

        # Second batch call should hit cache for both
        result = await dedup.batch_check_inbound(["msg_001", "msg_002"])
        assert result == {"msg_001": True, "msg_002": False}
        # batch_message_exists only called once (first call)
        assert db.batch_message_exists.await_count == 1

    @pytest.mark.asyncio
    async def test_single_id_works(self) -> None:
        db = _make_db()
        db.batch_message_exists = AsyncMock(return_value={"msg_001": True})
        dedup = DeduplicationService(db=db)

        result = await dedup.batch_check_inbound(["msg_001"])
        assert result == {"msg_001": True}
        assert dedup.stats.inbound_hits == 1


# ===========================================================================
# Outbound dedup
# ===========================================================================


class TestOutboundDedup:
    """Tests for content-hash–based outbound dedup (keyed two-phase API)."""

    def test_miss_on_first_send(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        is_dup, key = dedup.check_outbound_with_key("chat_1", "hello")
        assert is_dup is False
        assert isinstance(key, str) and len(key) == 16
        assert dedup.stats.outbound_misses == 1
        assert dedup.stats.outbound_hits == 0

    def test_hit_on_duplicate_within_ttl(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # First send: miss, then record
        is_dup, key = dedup.check_outbound_with_key("chat_1", "hello")
        assert is_dup is False
        dedup.record_outbound_keyed(key)
        # Second send: hit (same content within TTL)
        is_dup2, _ = dedup.check_outbound_with_key("chat_1", "hello")
        assert is_dup2 is True
        assert dedup.stats.outbound_hits == 1
        assert dedup.stats.outbound_misses == 1

    def test_different_chats_are_independent(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        is_dup, key = dedup.check_outbound_with_key("chat_1", "hello")
        assert is_dup is False
        dedup.record_outbound_keyed(key)
        # Same text, different chat → miss
        is_dup2, _ = dedup.check_outbound_with_key("chat_2", "hello")
        assert is_dup2 is False
        assert dedup.stats.outbound_misses == 2
        assert dedup.stats.outbound_hits == 0

    def test_different_texts_are_independent(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        is_dup, key = dedup.check_outbound_with_key("chat_1", "hello")
        assert is_dup is False
        dedup.record_outbound_keyed(key)
        # Same chat, different text → miss
        is_dup2, _ = dedup.check_outbound_with_key("chat_1", "world")
        assert is_dup2 is False
        assert dedup.stats.outbound_misses == 2
        assert dedup.stats.outbound_hits == 0

    def test_record_outbound_keyed_manual(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # Manually compute key and record, then check → should be a hit
        key = outbound_key("chat_1", "manual")
        dedup.record_outbound_keyed(key)
        is_dup, _ = dedup.check_outbound_with_key("chat_1", "manual")
        assert is_dup is True

    def test_check_and_record_outbound_single_pass(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # First call: not a duplicate
        assert dedup.check_and_record_outbound("chat_1", "hello") is False
        # Second call: is a duplicate (recorded in first call)
        assert dedup.check_and_record_outbound("chat_1", "hello") is True
        assert dedup.stats.outbound_hits == 1
        assert dedup.stats.outbound_misses == 1

    def test_key_reuse_avoids_double_hash(self) -> None:
        """check_outbound_with_key + record_outbound_keyed uses hash once."""
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # The key returned by check_outbound_with_key should match outbound_key
        is_dup, key = dedup.check_outbound_with_key("chat_1", "test")
        assert key == outbound_key("chat_1", "test")
        dedup.record_outbound_keyed(key)
        # The same key should now be a hit
        assert key in dedup._outbound_cache


# ===========================================================================
# Prometheus output
# ===========================================================================


class TestDedupPrometheusOutput:
    """Tests for the build_dedup_prometheus_output function."""

    def test_returns_empty_string_for_none(self) -> None:
        from src.health.prometheus import build_dedup_prometheus_output

        assert build_dedup_prometheus_output(None) == ""

    def test_emits_all_four_counters(self) -> None:
        from src.health.prometheus import build_dedup_prometheus_output

        stats = DedupStats(inbound_hits=3, inbound_misses=10, outbound_hits=1, outbound_misses=5)
        output = build_dedup_prometheus_output(stats)
        assert "custombot_dedup_inbound_hits_total" in output
        assert "custombot_dedup_inbound_misses_total" in output
        assert "custombot_dedup_outbound_hits_total" in output
        assert "custombot_dedup_outbound_misses_total" in output
        # Verify counter values
        assert "3" in output  # inbound_hits
        assert "10" in output  # inbound_misses
        assert "1" in output  # outbound_hits
        assert "5" in output  # outbound_misses

    def test_prometheus_format_structure(self) -> None:
        from src.health.prometheus import build_dedup_prometheus_output

        stats = DedupStats(inbound_hits=1, inbound_misses=0, outbound_hits=0, outbound_misses=0)
        output = build_dedup_prometheus_output(stats)
        # Each metric has HELP and TYPE headers
        assert "# HELP custombot_dedup_inbound_hits_total" in output
        assert "# TYPE custombot_dedup_inbound_hits_total counter" in output

    def test_zero_stats_produce_valid_output(self) -> None:
        from src.health.prometheus import build_dedup_prometheus_output

        stats = DedupStats()
        output = build_dedup_prometheus_output(stats)
        # Should produce valid output even with all zeros
        assert "custombot_dedup_inbound_hits_total" in output
        # Value should be 0
        for metric_name in [
            "custombot_dedup_inbound_hits_total",
            "custombot_dedup_inbound_misses_total",
            "custombot_dedup_outbound_hits_total",
            "custombot_dedup_outbound_misses_total",
        ]:
            # Find the metric line (not HELP/TYPE) and verify value
            for line in output.split("\n"):
                if line.startswith(metric_name + " ") or (
                    metric_name in line and not line.startswith("#")
                ):
                    assert "0" in line


# ===========================================================================
# Property-based tests for outbound_key() hash collision resistance
# ===========================================================================


class TestOutboundKeyPropertyBased:
    """Hypothesis-driven tests validating outbound_key hash properties."""

    @given(
        chat_id_a=st.text(min_size=1, max_size=200),
        text_a=st.text(min_size=0, max_size=500),
        chat_id_b=st.text(min_size=1, max_size=200),
        text_b=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=500)
    def test_distinct_inputs_produce_distinct_keys(
        self, chat_id_a: str, text_a: str, chat_id_b: str, text_b: str
    ) -> None:
        """Different (chat_id, text) pairs must produce different keys."""
        from hypothesis import assume

        assume((chat_id_a, text_a) != (chat_id_b, text_b))
        assert outbound_key(chat_id_a, text_a) != outbound_key(chat_id_b, text_b)

    def test_null_byte_separator_prevents_prefix_collision(self) -> None:
        """The \\x00 separator prevents ("a","bc") vs ("ab","c") collisions.

        Without the separator, concatenating "a"+"bc" and "ab"+"c" would
        produce the same string "abc" and thus the same hash.  The null
        byte ensures these tuples map to distinct byte sequences.
        """
        assert outbound_key("a", "bc") != outbound_key("ab", "c")
        assert outbound_key("", "abc") != outbound_key("abc", "")

    @given(
        chat_id=st.text(min_size=1, max_size=200),
        text=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=200)
    def test_always_returns_valid_hex(self, chat_id: str, text: str) -> None:
        """outbound_key always returns a valid 16-character hex string (xxh64)."""
        key = outbound_key(chat_id, text)
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    @given(
        chat_id=st.text(min_size=0, max_size=200),
        text=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=300)
    def test_deterministic(self, chat_id: str, text: str) -> None:
        """Calling outbound_key twice with the same inputs always yields the same key."""
        assert outbound_key(chat_id, text) == outbound_key(chat_id, text)

    @given(
        chat_id=st.text(min_size=0, max_size=10_000),
        text=st.text(min_size=0, max_size=10_000),
    )
    @settings(max_examples=100)
    def test_very_long_inputs(self, chat_id: str, text: str) -> None:
        """outbound_key handles very long inputs (up to 10 KB) without error.

        Verifies both determinism and valid hex output for large payloads
        that could appear in practice (e.g. long messages or encoded attachments).
        """
        key = outbound_key(chat_id, text)
        assert key == outbound_key(chat_id, text)
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    @given(data=st.data())
    @settings(max_examples=200)
    def test_unicode_edge_cases(self, data: st.DataObject) -> None:
        """Exercise Unicode edge cases including surrogates, zero-width chars, and CJK."""
        chat_id = data.draw(
            st.text(
                alphabet=st.characters(
                    min_codepoint=0x0001,
                    max_codepoint=0x10FFFF,
                    exclude_categories=("Cs",),
                ),
                min_size=0,
                max_size=200,
            )
        )
        text = data.draw(
            st.text(
                alphabet=st.characters(
                    min_codepoint=0x0001,
                    max_codepoint=0x10FFFF,
                    exclude_categories=("Cs",),
                ),
                min_size=0,
                max_size=500,
            )
        )
        key = outbound_key(chat_id, text)
        assert key == outbound_key(chat_id, text)
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    # ── Truncation stability (mirrors check_and_record_request) ─────────

    _TRUNCATION_LENGTH = 500  # matches REQUEST_DEDUP_HASH_TEXT_LENGTH

    @given(
        chat_id=st.text(min_size=1, max_size=200),
        text=st.text(min_size=0, max_size=2000),
    )
    @settings(max_examples=300)
    def test_truncated_text_deterministic(self, chat_id: str, text: str) -> None:
        """Truncated text hashing is deterministic: same truncation → same key."""
        truncated = text[: self._TRUNCATION_LENGTH]
        assert outbound_key(chat_id, truncated) == outbound_key(chat_id, truncated)

    @given(
        chat_id=st.text(min_size=1, max_size=200),
        text=st.text(min_size=501, max_size=5000),
        suffix=st.text(min_size=1, max_size=500),
    )
    @settings(max_examples=200)
    def test_truncation_stable_across_extensions(
        self, chat_id: str, text: str, suffix: str
    ) -> None:
        """Appending content beyond the truncation boundary does not change the hash.

        This mirrors the behaviour of ``check_and_record_request`` which truncates
        text to ``_request_hash_text_length`` characters before hashing.  Two texts
        that differ only after the truncation boundary must produce the same key.
        """
        key_original = outbound_key(chat_id, text[: self._TRUNCATION_LENGTH])
        key_extended = outbound_key(chat_id, (text + suffix)[: self._TRUNCATION_LENGTH])
        assert key_original == key_extended

    @given(
        chat_id=st.text(min_size=1, max_size=200),
        text=st.text(min_size=0, max_size=10_000),
    )
    @settings(max_examples=200)
    def test_truncated_always_valid_hex(self, chat_id: str, text: str) -> None:
        """Truncated text always produces valid 16-char hex (xxh64) output."""
        truncated = text[: self._TRUNCATION_LENGTH]
        key = outbound_key(chat_id, truncated)
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)


# ===========================================================================
# Pooled hasher — _outbound_key consistency with outbound_key
# ===========================================================================


class TestPooledHasher:
    """Verify that the pooled _outbound_key produces identical results
    to the module-level outbound_key function."""

    def test_pooled_matches_standalone(self) -> None:
        """Pooled hasher and standalone function produce identical keys."""
        db = _make_db()
        dedup = DeduplicationService(db=db)
        for chat_id, text in [
            ("chat_1", "hello"),
            ("chat_1", ""),
            ("", "text_only"),
            ("a" * 200, "b" * 500),
            ("unicode", "🎉"),
        ]:
            assert dedup._outbound_key(chat_id, text) == outbound_key(chat_id, text)

    @given(
        chat_id=st.text(min_size=0, max_size=200),
        text=st.text(min_size=0, max_size=2000),
    )
    @settings(max_examples=500)
    def test_pooled_deterministic(self, chat_id: str, text: str) -> None:
        """Pooled hasher produces consistent results across repeated calls."""
        db = _make_db()
        dedup = DeduplicationService(db=db)
        key1 = dedup._outbound_key(chat_id, text)
        key2 = dedup._outbound_key(chat_id, text)
        assert key1 == key2
        assert key1 == outbound_key(chat_id, text)

    def test_burst_flush_outbound_batch_pooled(self) -> None:
        """Burst outbound flush uses pooled hasher — keys match standalone."""
        db = _make_db()
        dedup = DeduplicationService(db=db)
        pairs = [(f"chat_{i}", f"message text {i}") for i in range(100)]
        for cid, txt in pairs:
            dedup._outbound_buffer.append((cid, txt))
        dedup.flush_outbound_batch()
        # Verify every entry is visible in the cache via standalone key
        for cid, txt in pairs:
            assert dedup._outbound_cache.get(outbound_key(cid, txt)) is not None


# ===========================================================================
# Integration tests — concurrent flush + check
# ===========================================================================


class TestConcurrentFlushAndCheck:
    """Integration tests for concurrent flush + check operations.

    Verifies that ``flush_outbound_batch()`` and ``check_and_record_outbound()``
    work correctly when called concurrently, that no data is lost during
    concurrent access, and that the buffer is properly drained.
    """

    @pytest.mark.asyncio
    async def test_concurrent_check_and_record_outbound(self) -> None:
        """Concurrent check_and_record_outbound calls don't lose data."""
        db = _make_db()
        dedup = DeduplicationService(db=db)

        # Concurrently check and record unique entries
        async def record(i: int) -> bool:
            return dedup.check_and_record_outbound(f"chat_{i}", f"response_{i}")

        results = await asyncio.gather(*[record(i) for i in range(20)])

        # All should be non-duplicate (first time seen)
        assert all(r is False for r in results)

        # Now check again - all should be duplicates
        results2 = await asyncio.gather(*[record(i) for i in range(20)])
        assert all(r is True for r in results2)

    def test_flush_outbound_batch_deduplicates(self) -> None:
        """flush_outbound_batch deduplicates buffered entries."""
        db = _make_db()
        dedup = DeduplicationService(db=db)

        # Manually add duplicates to buffer
        for _ in range(5):
            dedup._outbound_buffer.append(("chat_1", "same text"))

        dedup.flush_outbound_batch()

        # Buffer should be empty after flush
        assert len(dedup._outbound_buffer) == 0

        # Only one unique entry in cache (5 duplicates deduped to 1)
        key = outbound_key("chat_1", "same text")
        assert dedup._outbound_cache.get(key) is not None
