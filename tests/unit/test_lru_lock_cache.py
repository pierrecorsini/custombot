"""
Tests for src/utils/__init__.py — LRULockCache eviction safety.

Covers:
- Held lock is never evicted when cache is full
- Lock can be evicted after release
- Cache grows beyond max_size when all entries are in-use
- Ref-counted but unlocked lock survives eviction
- Oldest unreferenced entry is evicted first
- acquire() context manager properly pairs ref counts
- EvictionPolicy.REJECT_ON_FULL raises when all entries active
- EvictionPolicy.GROW allows unbounded growth (default)
- stats() includes eviction_policy
"""

from __future__ import annotations

import asyncio

import pytest

from src.constants import EvictionPolicy
from src.utils import LRULockCache


# ─────────────────────────────────────────────────────────────────────────────
# Test eviction safety with held locks
# ─────────────────────────────────────────────────────────────────────────────


class TestEvictionSafetyWithHeldLocks:
    """Verify that LRULockCache never evicts a lock that is currently held."""

    @pytest.mark.asyncio
    async def test_held_lock_not_evicted_on_insert(self) -> None:
        """A lock held via acquire() survives eviction when new keys are inserted."""
        cache = LRULockCache(max_size=2)

        # Acquire lock "A" and hold it
        async with cache.acquire("A"):
            # Insert "B" — cache is now at max_size (A, B)
            lock_b = await cache.get_or_create("B")
            cache.release("B")
            assert len(cache) == 2

            # Insert "C" — triggers eviction of oldest unreferenced entry ("B")
            lock_c = await cache.get_or_create("C")
            cache.release("C")

            # "A" must still be in cache because it's held
            assert "A" in cache._cache
            assert "B" not in cache._cache  # evicted
            assert "C" in cache._cache

    @pytest.mark.asyncio
    async def test_lock_evictable_after_release(self) -> None:
        """A lock becomes eligible for eviction once released."""
        cache = LRULockCache(max_size=2)

        # Use "A" then release
        async with cache.acquire("A"):
            pass  # released now

        # Fill cache to max
        lock_b = await cache.get_or_create("B")
        cache.release("B")
        assert len(cache) == 2

        # Insert "C" — should evict oldest unreferenced ("A")
        lock_c = await cache.get_or_create("C")
        cache.release("C")

        assert "A" not in cache._cache
        assert "B" in cache._cache
        assert "C" in cache._cache

    @pytest.mark.asyncio
    async def test_cache_grows_when_all_entries_held(self) -> None:
        """If all cached locks are held, inserting a new key grows beyond max_size."""
        cache = LRULockCache(max_size=2)

        async with cache.acquire("A"):
            async with cache.acquire("B"):
                # Both "A" and "B" are held; inserting "C" must grow the cache
                lock_c = await cache.get_or_create("C")
                cache.release("C")

                assert len(cache) == 3
                assert "A" in cache._cache
                assert "B" in cache._cache
                assert "C" in cache._cache


# ─────────────────────────────────────────────────────────────────────────────
# Test ref-count tracking
# ─────────────────────────────────────────────────────────────────────────────


class TestRefCountTracking:
    """Verify that reference counts prevent eviction even when lock is unlocked."""

    @pytest.mark.asyncio
    async def test_ref_counted_unlocked_lock_survives_eviction(self) -> None:
        """A lock with ref_count > 0 but not locked is not evicted."""
        cache = LRULockCache(max_size=2)

        # get_or_create bumps ref count but we don't acquire the lock
        lock_a = await cache.get_or_create("A")  # ref_count["A"] = 1, not locked
        lock_b = await cache.get_or_create("B")
        cache.release("B")
        assert len(cache) == 2

        # Insert "C" — should try to evict oldest. "A" has ref_count=1, skip it.
        # "B" has ref_count=0, evict "B".
        lock_c = await cache.get_or_create("C")
        cache.release("C")

        assert "A" in cache._cache  # survived due to ref_count > 0
        assert "B" not in cache._cache  # evicted
        assert "C" in cache._cache

        # Clean up
        cache.release("A")

    @pytest.mark.asyncio
    async def test_oldest_unreferenced_evicted_first(self) -> None:
        """When multiple entries are unreferenced, the oldest (first inserted) is evicted."""
        cache = LRULockCache(max_size=3)

        # Insert A, B, C — all unreferenced after release
        lock_a = await cache.get_or_create("A")
        cache.release("A")
        lock_b = await cache.get_or_create("B")
        cache.release("B")
        lock_c = await cache.get_or_create("C")
        cache.release("C")

        assert len(cache) == 3

        # Insert D — evicts oldest unreferenced ("A")
        lock_d = await cache.get_or_create("D")
        cache.release("D")

        assert "A" not in cache._cache
        assert "B" in cache._cache
        assert "C" in cache._cache
        assert "D" in cache._cache

    @pytest.mark.asyncio
    async def test_acquire_context_manager_paired_ref_count(self) -> None:
        """After acquire() context exits, ref_count returns to zero."""
        cache = LRULockCache(max_size=5)

        async with cache.acquire("X"):
            assert cache._ref_counts.get("X", 0) > 0

        # After context exit, ref count should be cleaned up
        assert "X" not in cache._ref_counts

    @pytest.mark.asyncio
    async def test_evicted_entry_ref_count_cleaned_up(self) -> None:
        """After eviction, _ref_counts has no stale keys for evicted entries."""
        cache = LRULockCache(max_size=2)

        lock_a = await cache.get_or_create("A")
        cache.release("A")
        lock_b = await cache.get_or_create("B")
        cache.release("B")

        # Insert C to trigger eviction of A
        lock_c = await cache.get_or_create("C")
        cache.release("C")

        assert "A" not in cache._ref_counts


# ─────────────────────────────────────────────────────────────────────────────
# Test get_or_create re-access
# ─────────────────────────────────────────────────────────────────────────────


class TestGetOrCreateReaccess:
    """Verify that re-accessing an existing key moves it to most-recently-used."""

    @pytest.mark.asyncio
    async def test_reaccess_moves_to_end(self) -> None:
        """Re-accessing a key protects it from being the next eviction candidate."""
        cache = LRULockCache(max_size=3)

        lock_a = await cache.get_or_create("A")
        cache.release("A")
        lock_b = await cache.get_or_create("B")
        cache.release("B")
        lock_c = await cache.get_or_create("C")
        cache.release("C")

        # Re-access "A" to make it most-recently-used
        lock_a2 = await cache.get_or_create("A")
        cache.release("A")

        # Insert "D" — should evict "B" (oldest unreferenced after A was refreshed)
        lock_d = await cache.get_or_create("D")
        cache.release("D")

        assert "A" in cache._cache
        assert "B" not in cache._cache  # oldest, evicted
        assert "C" in cache._cache
        assert "D" in cache._cache

    @pytest.mark.asyncio
    async def test_returns_same_lock_for_same_key(self) -> None:
        """get_or_create returns the identical Lock object for the same key."""
        cache = LRULockCache(max_size=5)

        lock1 = await cache.get_or_create("chat_1")
        cache.release("chat_1")
        lock2 = await cache.get_or_create("chat_1")
        cache.release("chat_1")

        assert lock1 is lock2


# ─────────────────────────────────────────────────────────────────────────────
# Test active_count and configurable cache size
# ─────────────────────────────────────────────────────────────────────────────


class TestActiveCountAndConfigurableSize:
    """Verify active_count tracking and configurable cache size."""

    @pytest.mark.asyncio
    async def test_active_count_tracks_held_locks(self) -> None:
        """active_count reflects the number of currently held locks."""
        cache = LRULockCache(max_size=10)

        assert cache.active_count == 0

        async with cache.acquire("A"):
            assert cache.active_count == 1
            async with cache.acquire("B"):
                assert cache.active_count == 2

        assert cache.active_count == 0

    @pytest.mark.asyncio
    async def test_active_count_with_get_or_create(self) -> None:
        """active_count tracks refs from get_or_create until release."""
        cache = LRULockCache(max_size=10)

        await cache.get_or_create("X")
        assert cache.active_count == 1

        cache.release("X")
        assert cache.active_count == 0

    @pytest.mark.asyncio
    async def test_configurable_cache_size_larger_than_default(self) -> None:
        """Cache respects a custom max_size larger than the default."""
        cache = LRULockCache(max_size=5)

        # Insert 5 entries and release all
        for i in range(5):
            await cache.get_or_create(str(i))
            cache.release(str(i))

        assert len(cache) == 5

        # Insert 6th — evicts oldest (0)
        await cache.get_or_create("5")
        cache.release("5")

        assert len(cache) == 5
        assert "0" not in cache._cache
        assert "5" in cache._cache

    @pytest.mark.asyncio
    async def test_pressure_warning_logged_at_high_utilization(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pressure warning is logged when cache is full and most entries are active."""
        cache = LRULockCache(max_size=2)

        # Hold both locks — 2/2 = 100% active (> 80% threshold)
        async with cache.acquire("A"):
            async with cache.acquire("B"):
                # Insert "C" triggers eviction; all entries active → pressure warning
                with caplog.at_level("WARNING", logger="src.utils"):
                    await cache.get_or_create("C")
                    cache.release("C")

                assert any("under pressure" in r.message.lower() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Test configurable eviction policy
# ─────────────────────────────────────────────────────────────────────────────


class TestEvictionPolicy:
    """Verify configurable eviction policy behavior."""

    @pytest.mark.asyncio
    async def test_grow_policy_allows_unbounded_growth(self) -> None:
        """GROW policy (default) allows cache to grow beyond max_size when all held."""
        cache = LRULockCache(max_size=2, eviction_policy=EvictionPolicy.GROW)

        async with cache.acquire("A"):
            async with cache.acquire("B"):
                # All entries in-use; GROW allows insertion
                await cache.get_or_create("C")
                cache.release("C")

                assert len(cache) == 3  # grew beyond max_size=2

    @pytest.mark.asyncio
    async def test_reject_on_full_raises_when_saturated(self) -> None:
        """REJECT_ON_FULL raises RuntimeError when all cached locks are in-use."""
        cache = LRULockCache(max_size=2, eviction_policy=EvictionPolicy.REJECT_ON_FULL)

        async with cache.acquire("A"):
            async with cache.acquire("B"):
                # All 2 entries are in-use; inserting "C" should raise
                with pytest.raises(RuntimeError, match="Lock cache saturated"):
                    await cache.get_or_create("C")

        assert len(cache) == 2  # did not grow

    @pytest.mark.asyncio
    async def test_reject_on_full_evicts_unreferenced(self) -> None:
        """REJECT_ON_FULL still evicts unreferenced entries normally."""
        cache = LRULockCache(max_size=2, eviction_policy=EvictionPolicy.REJECT_ON_FULL)

        # Insert A and B, then release both
        await cache.get_or_create("A")
        cache.release("A")
        await cache.get_or_create("B")
        cache.release("B")

        # Insert C — should evict A (oldest unreferenced) without error
        await cache.get_or_create("C")
        cache.release("C")

        assert "A" not in cache._cache
        assert "B" in cache._cache
        assert "C" in cache._cache

    @pytest.mark.asyncio
    async def test_default_policy_is_grow(self) -> None:
        """Default eviction policy is GROW (backward compatible)."""
        cache = LRULockCache(max_size=2)

        async with cache.acquire("A"):
            async with cache.acquire("B"):
                # Should NOT raise — default is GROW
                await cache.get_or_create("C")
                cache.release("C")
                assert len(cache) == 3

    @pytest.mark.asyncio
    async def test_stats_includes_eviction_policy(self) -> None:
        """stats() includes the configured eviction policy."""
        cache = LRULockCache(max_size=10, eviction_policy=EvictionPolicy.REJECT_ON_FULL)
        stats = cache.stats()
        assert stats["eviction_policy"] == "reject_on_full"

        cache2 = LRULockCache(max_size=10)
        stats2 = cache2.stats()
        assert stats2["eviction_policy"] == "grow"
