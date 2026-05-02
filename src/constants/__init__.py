"""
src.constants — Named constants for custombot.

Centralizes magic numbers and configuration defaults for better maintainability
and code readability.  All values are documented with their purpose.

Usage (unchanged from the single-file version)::

    from src.constants import MAX_LRU_CACHE_SIZE, DEFAULT_HTTP_TIMEOUT

Constants are organised into domain sub-modules:

    - cache: LRU cache limits, file-handle pools, eviction policy
    - network: HTTP/network timeouts, channel configuration
    - llm: LLM config defaults, circuit breaker, streaming, ReAct retry, log rotation
    - db: Database timeouts, write circuit breakers, retry, SQLite, compression
    - scheduler: Scheduler retry, task error detection, HMAC integrity
    - memory: Memory/history limits, token estimation, monitoring thresholds
    - health: Health server rate limiting, disk-space checks
    - routing: Routing engine file watching, match cache
    - workspace: Workspace directory, cleanup, audit log rotation, config watcher
    - security: Input validation limits, rate limiting configuration
    - skills: Skill execution timeouts, safe-mode, tool result persistence
    - shutdown: Graceful shutdown configuration
    - messaging: Message queue limits, outbound dedup, event bus

Threading / Asyncio Model:
    This project uses both threading.Lock and asyncio.Lock. The rule is:
    - asyncio.Lock: Use in async code paths (bot.py, db.py, message_queue.py)
      where the lock guards async operations and the code runs on the event loop.
    - threading.Lock: Use in code that may be called from asyncio.to_thread()
      or from background daemon threads (vector_memory.py, rate_limiter.py,
      whatsapp.py NeonizeBackend). These modules use sync I/O that runs in
      thread pools where asyncio locks would deadlock.
    - Never mix: Do not acquire a threading.Lock inside an async function
      without asyncio.to_thread(), and do not use asyncio.Lock in code that
      runs in daemon threads (no event loop available).
"""

from __future__ import annotations

# Re-export all public names from sub-modules so existing
# ``from src.constants import X`` imports continue to work unchanged.

from src.constants.cache import (  # noqa: F401
    DEFAULT_CHAT_LOCK_CACHE_SIZE,
    DEFAULT_LOCK_CACHE_PRESSURE_THRESHOLD,
    DEFAULT_LOCK_EVICTION_POLICY,
    EvictionPolicy,
    MAX_FILE_HANDLES,
    MAX_LRU_CACHE_SIZE,
    MAX_READ_FILE_HANDLES,
    MTIME_CACHE_MISSING_TTL,
)

from src.constants.network import (  # noqa: F401
    DEFAULT_CHANNEL_STARTUP_TIMEOUT,
    DEFAULT_HTTPX_MAX_CONNECTIONS,
    DEFAULT_HTTPX_MAX_KEEPALIVE_CONNECTIONS,
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_HTTP_CONNECT_TIMEOUT,
    LLM_WARMUP_TIMEOUT,
    MAX_QR_SCAN_WAIT,
)

from src.constants.llm import (  # noqa: F401
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_RETRY_DELAY,
    LLM_HEALTH_PROBE_INTERVAL_SECONDS,
    LLM_LOG_CLEANUP_INTERVAL,
    LLM_LOG_MAX_AGE_DAYS,
    LLM_LOG_MAX_FILES,
    MAX_TOOL_CALLS_PER_TURN,
    MAX_TOOL_ITERATIONS,
    REACT_LOOP_MAX_RETRIES,
    REACT_LOOP_RETRY_INITIAL_DELAY,
    STREAM_MIN_CHUNK_CHARS,
)

from src.constants.db import (  # noqa: F401
    COMPRESSION_KEEP_RECENT,
    COMPRESSION_LINE_THRESHOLD,
    DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    DB_WRITE_MAX_RETRIES,
    DB_WRITE_RETRY_INITIAL_DELAY,
    DEFAULT_DB_TIMEOUT,
    MAX_CHAT_GENERATIONS,
    SQLITE_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    SQLITE_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    SQLITE_WRITE_MAX_RETRIES,
    SQLITE_WRITE_RETRY_INITIAL_DELAY,
)

from src.constants.scheduler import (  # noqa: F401
    DEFAULT_SCHEDULER_TASK_TIMEOUT,
    MAX_SCHEDULED_PROMPT_LENGTH,
    SCHEDULED_ERROR_PREFIXES,
    SCHEDULER_HMAC_SECRET_ENV,
    SCHEDULER_HMAC_SIG_EXT,
    SCHEDULER_LOOP_BACKOFF_CAP,
    SCHEDULER_MAX_RETRIES,
    SCHEDULER_MAX_SLEEP_SECONDS,
    SCHEDULER_MIN_SLEEP_SECONDS,
    SCHEDULER_RETRY_INITIAL_DELAY,
)

from src.constants.memory import (  # noqa: F401
    CHARS_PER_TOKEN,
    CJK_CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_TOKEN_BUDGET,
    DEFAULT_MEMORY_MAX_HISTORY,
    MEMORY_CHECK_INTERVAL_SECONDS,
    MEMORY_CRITICAL_THRESHOLD_PERCENT,
    MEMORY_WARNING_THRESHOLD_PERCENT,
)

from src.constants.health import (  # noqa: F401
    HEALTH_DISK_FREE_THRESHOLD_MB,
    HEALTH_HTTP_RATE_LIMIT,
    HEALTH_HTTP_RATE_MAX_TRACKED_IPS,
    HEALTH_HTTP_RATE_WINDOW_SECONDS,
    HEALTH_MAX_REQUEST_BODY_BYTES,
    HEALTH_MAX_URL_LENGTH,
)

from src.constants.routing import (  # noqa: F401
    ROUTING_MATCH_CACHE_MAX_SIZE,
    ROUTING_MATCH_CACHE_TTL_SECONDS,
    ROUTING_WATCH_DEBOUNCE_SECONDS,
)

from src.constants.workspace import (  # noqa: F401
    AUDIT_LOG_MAX_AGE_DAYS,
    AUDIT_LOG_MAX_FILES,
    CONFIG_WATCH_DEBOUNCE_SECONDS,
    CONFIG_WATCH_INTERVAL_SECONDS,
    DEFAULT_THREAD_POOL_WORKERS,
    WORKSPACE_ARCHIVE_MAX_AGE_DAYS,
    WORKSPACE_BACKUP_MAX_AGE_DAYS,
    WORKSPACE_CLEANUP_INTERVAL_SECONDS,
    WORKSPACE_DIR,
    WORKSPACE_SIZE_WARNING_MB,
    WORKSPACE_STALE_TEMP_MAX_AGE_HOURS,
)

from src.constants.security import (  # noqa: F401
    DEFAULT_CHAT_RATE_LIMIT,
    DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT,
    MAX_CHAT_ID_LENGTH,
    MAX_MESSAGE_ID_LENGTH,
    MAX_MESSAGE_LENGTH,
    MAX_RATE_LIMIT_TRACKED_CHATS,
    MAX_SENDER_ID_LENGTH,
    RATE_LIMIT_MAX_VALUE,
    RATE_LIMIT_MIN_VALUE,
    RATE_LIMIT_WINDOW_SECONDS,
)

from src.constants.skills import (  # noqa: F401
    DEFAULT_SKILL_TIMEOUT,
    MAX_TOOL_RESULT_PERSIST_LENGTH,
    SAFE_MODE_MAX_CONFIRM_RETRIES,
    SLOW_SKILL_THRESHOLD_SECONDS,
)

from src.constants.shutdown import (  # noqa: F401
    CLEANUP_STEP_TIMEOUT,
    DEFAULT_MAX_CONCURRENT_MESSAGES,
    DEFAULT_SHUTDOWN_TIMEOUT,
    SHUTDOWN_LOG_INTERVAL,
)

from src.constants.messaging import (  # noqa: F401
    EVENT_BUS_MAX_HANDLERS_PER_EVENT,
    MAX_QUEUED_TEXT_LENGTH,
    OUTBOUND_DEDUP_MAX_SIZE,
    OUTBOUND_DEDUP_TTL_SECONDS,
    QUEUE_FSYNC_BATCH_SIZE,
    QUEUE_FSYNC_INTERVAL_SECONDS,
)
