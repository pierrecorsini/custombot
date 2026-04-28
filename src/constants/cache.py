"""Cache and pool limits — bounded caches, file-handle pools, eviction policy."""

from __future__ import annotations

from enum import Enum


class EvictionPolicy(Enum):
    """Eviction strategy when the per-chat lock cache is full and all entries are active.

    Values:
        GROW: Allow the cache to grow beyond ``max_size`` temporarily when all
              cached locks are in-use.  Safe for correctness but unbounded in memory.
        REJECT_ON_FULL: Raise ``RuntimeError`` instead of growing, preventing
              unbounded memory at the cost of rejecting messages for new chats
              when the cache is saturated.  Use for memory-constrained deployments.
    """

    GROW = "grow"
    REJECT_ON_FULL = "reject_on_full"


# Default eviction policy for the per-chat lock cache.
# Configurable via ``max_chat_lock_eviction_policy`` in config.json.
DEFAULT_LOCK_EVICTION_POLICY: EvictionPolicy = EvictionPolicy.GROW

# ─────────────────────────────────────────────────────────────────────────────
# LRU Cache Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of locks to retain in the LRU cache.
# Used for per-chat locks to prevent unbounded memory growth.
# Each chat gets its own lock; 1000 concurrent chats is a reasonable upper bound.
MAX_LRU_CACHE_SIZE: int = 1000

# Default per-chat lock cache size, configurable via config.json.
# Controls how many per-chat asyncio.Lock objects the LRULockCache retains
# before evicting the least-recently-used entry.  Under sustained load with
# more concurrent chats than this value, locks for inactive chats are evicted
# to free memory.  Active (held) locks are never evicted.
# Raise for deployments with >1000 concurrent chats.
DEFAULT_CHAT_LOCK_CACHE_SIZE: int = 1000

# Fraction of max_size at which the LRULockCache logs a pressure warning.
# When active (held) locks exceed this ratio, the cache logs actionable advice
# to raise ``max_chat_lock_cache_size`` in config.json.  0.8 means warnings
# start when 800 of 1000 cached locks are actively held.
DEFAULT_LOCK_CACHE_PRESSURE_THRESHOLD: float = 0.8

# Maximum number of pooled file handles for Database message-file appends.
# Prevents OS file-descriptor exhaustion (EMFILE / "Too many open files")
# under extreme concurrency by reusing open handles instead of open/close
# per write.  256 is well under typical OS limits (Linux soft 1024,
# Windows 512, macOS 256) and leaves headroom for other file operations.
MAX_FILE_HANDLES: int = 256

# Maximum number of pooled read-mode file handles for JSONL message retrieval.
# Read handles are reused across get_recent_messages() calls, eliminating
# per-read open/close syscalls on the hot path.  Smaller than the write pool
# because reads are bursty (triggered by incoming messages) while writes are
# continuous (every message produces one).
MAX_READ_FILE_HANDLES: int = 128

# ─────────────────────────────────────────────────────────────────────────────
# MtimeCache — Missing-File TTL
# ─────────────────────────────────────────────────────────────────────────────

# Seconds to remember that a file didn't exist before rechecking via stat().
# Avoids redundant asyncio.to_thread() hops for new chats where MEMORY.md
# (or AGENTS.md) hasn't been created yet.  30s balances latency savings
# against prompt detection of externally-created files.
MTIME_CACHE_MISSING_TTL: float = 30.0
