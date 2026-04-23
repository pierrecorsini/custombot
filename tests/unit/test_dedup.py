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

from src.core.dedup import DedupStats, DeduplicationService, outbound_key
from src.exceptions import DatabaseError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(message_exists_return: bool = False) -> AsyncMock:
    """Create a minimal mock Storage with ``message_exists``."""
    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=message_exists_return)
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
        assert len(key) == 64  # SHA-256 hex digest length

    def test_empty_text(self) -> None:
        """Empty text still produces a valid key."""
        key = outbound_key("chat_1", "")
        assert len(key) == 64


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


# ===========================================================================
# Outbound dedup
# ===========================================================================


class TestOutboundDedup:
    """Tests for content-hash–based outbound dedup."""

    def test_miss_on_first_send(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        assert dedup.is_outbound_duplicate("chat_1", "hello") is False
        assert dedup.stats.outbound_misses == 1
        assert dedup.stats.outbound_hits == 0

    def test_hit_on_duplicate_within_ttl(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # First send: miss
        dedup.is_outbound_duplicate("chat_1", "hello")
        # Second send: hit (same content within TTL)
        assert dedup.is_outbound_duplicate("chat_1", "hello") is True
        assert dedup.stats.outbound_hits == 1
        assert dedup.stats.outbound_misses == 1

    def test_different_chats_are_independent(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        dedup.is_outbound_duplicate("chat_1", "hello")
        # Same text, different chat → miss
        assert dedup.is_outbound_duplicate("chat_2", "hello") is False
        assert dedup.stats.outbound_misses == 2
        assert dedup.stats.outbound_hits == 0

    def test_different_texts_are_independent(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db)
        dedup.is_outbound_duplicate("chat_1", "hello")
        # Same chat, different text → miss
        assert dedup.is_outbound_duplicate("chat_1", "world") is False
        assert dedup.stats.outbound_misses == 2
        assert dedup.stats.outbound_hits == 0

    def test_record_outbound_manual(self) -> None:
        db = _make_db()
        dedup = DeduplicationService(db=db, outbound_ttl=60.0)
        # Manually record, then check → should be a hit
        dedup.record_outbound("chat_1", "manual")
        assert dedup.is_outbound_duplicate("chat_1", "manual") is True


# ===========================================================================
# Prometheus output
# ===========================================================================


class TestDedupPrometheusOutput:
    """Tests for the _build_dedup_prometheus_output function."""

    def test_returns_empty_string_for_none(self) -> None:
        from src.health.server import _build_dedup_prometheus_output

        assert _build_dedup_prometheus_output(None) == ""

    def test_emits_all_four_counters(self) -> None:
        from src.health.server import _build_dedup_prometheus_output

        stats = DedupStats(inbound_hits=3, inbound_misses=10, outbound_hits=1, outbound_misses=5)
        output = _build_dedup_prometheus_output(stats)
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
        from src.health.server import _build_dedup_prometheus_output

        stats = DedupStats(inbound_hits=1, inbound_misses=0, outbound_hits=0, outbound_misses=0)
        output = _build_dedup_prometheus_output(stats)
        # Each metric has HELP and TYPE headers
        assert "# HELP custombot_dedup_inbound_hits_total" in output
        assert "# TYPE custombot_dedup_inbound_hits_total counter" in output

    def test_zero_stats_produce_valid_output(self) -> None:
        from src.health.server import _build_dedup_prometheus_output

        stats = DedupStats()
        output = _build_dedup_prometheus_output(stats)
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
