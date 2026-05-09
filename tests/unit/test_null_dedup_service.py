"""Tests for NullDedupService — NullObject for DeduplicationService interface."""

from __future__ import annotations

import asyncio

import pytest

from src.core.dedup import NullDedupService


class TestNullDedupServiceCheckMethodsReturnFalse:
    """All check methods must return False (never a duplicate)."""

    @pytest.mark.asyncio
    async def test_is_inbound_duplicate_returns_false(self) -> None:
        svc = NullDedupService()
        assert await svc.is_inbound_duplicate("msg-123") is False

    @pytest.mark.asyncio
    async def test_batch_check_inbound_returns_all_false(self) -> None:
        svc = NullDedupService()
        ids = ["msg-1", "msg-2", "msg-3"]
        result = await svc.batch_check_inbound(ids)
        assert list(result.keys()) == ids
        assert all(v is False for v in result.values())

    def test_check_outbound_with_key_returns_false_and_key(self) -> None:
        svc = NullDedupService()
        is_dup, key = svc.check_outbound_with_key("chat-1", "hello")
        assert is_dup is False
        assert isinstance(key, str) and len(key) > 0

    def test_check_and_record_outbound_returns_false(self) -> None:
        svc = NullDedupService()
        assert svc.check_and_record_outbound("chat-1", "hello") is False

    def test_check_and_record_request_returns_false(self) -> None:
        svc = NullDedupService()
        assert svc.check_and_record_request("chat-1", "hello") is False


class TestNullDedupServiceRecordMethodsAreNoop:
    """All record/flush methods must be safe no-ops."""

    def test_record_outbound_keyed_is_noop(self) -> None:
        svc = NullDedupService()
        svc.record_outbound_keyed("some-key")  # no exception

    def test_flush_outbound_batch_is_noop(self) -> None:
        svc = NullDedupService()
        svc.flush_outbound_batch()  # no exception


class TestNullDedupServiceStats:
    """stats property must return zeroed counters."""

    def test_stats_returns_zeroed_dedup_stats(self) -> None:
        from src.core.dedup import DedupStats

        svc = NullDedupService()
        stats = svc.stats
        assert isinstance(stats, DedupStats)
        assert stats.inbound_hits == 0
        assert stats.inbound_misses == 0
        assert stats.outbound_hits == 0
        assert stats.outbound_misses == 0
        assert stats.request_hits == 0
        assert stats.request_misses == 0


class TestNullDedupServiceDeterminism:
    """NullDedupService must be deterministic and idempotent."""

    @pytest.mark.asyncio
    async def test_repeated_inbound_checks_always_false(self) -> None:
        svc = NullDedupService()
        for _ in range(20):
            assert await svc.is_inbound_duplicate("same-msg-id") is False

    def test_repeated_outbound_checks_always_false(self) -> None:
        svc = NullDedupService()
        for _ in range(20):
            is_dup, _ = svc.check_outbound_with_key("chat-1", "hello")
            assert is_dup is False

    def test_stats_always_fresh_zeroed_instance(self) -> None:
        svc = NullDedupService()
        stats1 = svc.stats
        # Even after record calls, stats should be zeroed
        svc.record_outbound_keyed("some-key")
        stats2 = svc.stats
        assert stats2.inbound_hits == 0
        assert stats2.outbound_hits == 0
        # Each access returns a new instance
        assert stats1 is not stats2
