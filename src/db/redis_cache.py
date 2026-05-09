"""
src/db/redis_cache.py — Optional Redis caching layer with in-memory fallback.

Provides a ``RedisCacheBackend`` that transparently uses Redis when available
and falls back to an in-memory dict cache when Redis is unreachable or the
``redis`` package is not installed.

Designed for caching hot data: active chat contexts, routing rules, dedup state.

Usage::

    from src.db.redis_cache import RedisCacheBackend

    cache = RedisCacheBackend(redis_url="redis://localhost:6379", enabled=True)
    await cache.set("key", {"data": 1}, ttl=300)
    value = await cache.get("key")
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# Graceful import — ``redis`` is an optional dependency.
try:
    import redis.asyncio as aioredis

    _REDIS_AVAILABLE = True
except ImportError:
    aioredis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False


class _InMemoryCache:
    """Minimal TTL-aware dict cache used when Redis is unavailable."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> Optional[str]:
        entry = self._store.get(key)
        if entry is None:
            return None
        _, expires_at = entry
        if expires_at > 0 and time.monotonic() > expires_at:
            del self._store[key]
            return None
        return entry[0]

    def set(self, key: str, value: str, ttl: float = 0) -> None:
        expires_at = (time.monotonic() + ttl) if ttl > 0 else 0.0
        self._store[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None


class RedisCacheBackend:
    """Async caching backend with Redis primary and in-memory fallback.

    When ``redis_url`` is empty, ``enabled`` is False, or the ``redis``
    package is not installed, all operations transparently use an
    in-memory dict cache instead.
    """

    def __init__(
        self,
        *,
        redis_url: str = "",
        enabled: bool = False,
    ) -> None:
        self._enabled = enabled and bool(redis_url)
        self._redis_url = redis_url
        self._redis: Any = None
        self._memory = _InMemoryCache()
        self._use_redis = False

        if self._enabled and not _REDIS_AVAILABLE:
            log.warning(
                "Redis caching requested but 'redis' package not installed — "
                "using in-memory fallback"
            )
            self._enabled = False

    async def connect(self) -> None:
        """Initialize the Redis connection pool.

        Safe to call even when Redis is disabled — no-op in that case.
        """
        if not self._enabled:
            log.debug("Redis cache disabled — using in-memory fallback")
            return

        try:
            self._redis = aioredis.from_url(  # type: ignore[union-attr]
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            await self._redis.ping()
            self._use_redis = True
            log.info("Redis cache connected: %s", self._redis_url)
        except Exception as exc:
            log.warning(
                "Redis connection failed — using in-memory fallback: %s", exc
            )
            self._redis = None
            self._use_redis = False

    async def close(self) -> None:
        """Close the Redis connection pool."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                log.debug("Error closing Redis connection", exc_info=True)
            self._redis = None
            self._use_redis = False

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a cached value by key.

        Returns the deserialized Python object, or ``None`` if not found.
        """
        if self._use_redis:
            return await self._redis_get(key)
        return self._deserialize(self._memory.get(key))

    async def set(self, key: str, value: Any, ttl: float = 300.0) -> None:
        """Store a value with optional TTL (seconds).

        Args:
            key: Cache key.
            value: Any JSON-serializable Python object.
            ttl: Time-to-live in seconds (0 = no expiry).
        """
        serialized = self._serialize(value)
        if self._use_redis:
            await self._redis_set(key, serialized, ttl)
        else:
            self._memory.set(key, serialized, ttl)

    async def delete(self, key: str) -> None:
        """Remove a key from the cache."""
        if self._use_redis:
            try:
                await self._redis.delete(key)
            except Exception:
                log.debug("Redis DELETE failed for key %r", key, exc_info=True)
                self._memory.delete(key)
        else:
            self._memory.delete(key)

    async def exists(self, key: str) -> bool:
        """Check whether a key exists in the cache."""
        if self._use_redis:
            try:
                return bool(await self._redis.exists(key))
            except Exception:
                log.debug("Redis EXISTS failed for key %r", key, exc_info=True)
                return self._memory.exists(key)
        return self._memory.exists(key)

    # ── Redis helpers ────────────────────────────────────────────────────

    async def _redis_get(self, key: str) -> Optional[Any]:
        try:
            raw = await self._redis.get(key)
            return self._deserialize(raw)
        except Exception:
            log.debug("Redis GET failed for key %r", key, exc_info=True)
            return self._deserialize(self._memory.get(key))

    async def _redis_set(self, key: str, serialized: str, ttl: float) -> None:
        try:
            if ttl > 0:
                await self._redis.setex(key, int(ttl), serialized)
            else:
                await self._redis.set(key, serialized)
        except Exception:
            log.debug("Redis SET failed for key %r", key, exc_info=True)
            self._memory.set(key, serialized, ttl)

    # ── serialization ────────────────────────────────────────────────────

    @staticmethod
    def _serialize(value: Any) -> str:
        return json.dumps(value, default=str)

    @staticmethod
    def _deserialize(raw: Optional[str]) -> Any:
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
