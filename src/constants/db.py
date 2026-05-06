"""Database constants — timeouts, write circuit breakers, retry, SQLite, compression, generation counter."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Database Operation Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for database operations (in seconds).
# File-based JSON operations should be quick.
DEFAULT_DB_TIMEOUT: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# Database Write Circuit Breaker Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Number of consecutive database write failures before opening the write
# circuit breaker.  When the filesystem is degraded (disk full, NFS dropout),
# every DB write individually times out after DEFAULT_DB_TIMEOUT (10s).
# Under sustained failure this creates a backlog of blocked coroutines
# starving the event loop.  The write breaker fast-fails once this many
# consecutive writes have failed.
DB_WRITE_CIRCUIT_FAILURE_THRESHOLD: int = 5

# Duration (seconds) the DB write circuit breaker stays OPEN before
# transitioning to HALF_OPEN to probe whether the filesystem has recovered.
# Shorter than the LLM cooldown because disk issues often resolve quickly
# (e.g. NFS reconnect, temp space freed).
DB_WRITE_CIRCUIT_COOLDOWN_SECONDS: float = 30.0

# ─────────────────────────────────────────────────────────────────────────────
# Database Write Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of retry attempts for transient file I/O errors in
# save_messages_batch().  Protects against the most common transient failure
# mode (brief disk I/O error, NFS hiccup) without masking persistent issues.
# 2 retries keeps total worst-case latency manageable (~3× write attempt).
DB_WRITE_MAX_RETRIES: int = 2

# Initial delay (seconds) before first retry of a transient DB write failure.
# Uses exponential backoff with jitter (see calculate_delay_with_jitter).
# Shorter than LLM retry delays because disk I/O issues often resolve quickly.
DB_WRITE_RETRY_INITIAL_DELAY: float = 0.5

# ─────────────────────────────────────────────────────────────────────────────
# SQLite Write Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of retry attempts for transient sqlite3.OperationalError
# ("database is locked", "database is busy") in SqliteHelper write methods.
# Lock contention is typically brief (milliseconds), so 3 retries with backoff
# covers the common case without masking persistent issues.
SQLITE_WRITE_MAX_RETRIES: int = 3

# Initial delay (seconds) before first retry of a transient SQLite write error.
# Uses exponential backoff with jitter (see calculate_delay_with_jitter).
# SQLite lock contention usually resolves in < 100ms, but we allow extra
# margin for concurrent writers under heavy load.
SQLITE_WRITE_RETRY_INITIAL_DELAY: float = 0.05

# Number of consecutive SQLite write failures before opening the circuit
# breaker.  Once open, all SQLite writes are fast-failed until the cooldown
# expires and a probe succeeds.
SQLITE_WRITE_CIRCUIT_FAILURE_THRESHOLD: int = 5

# Duration (seconds) the SQLite write circuit breaker stays OPEN before
# transitioning to HALF_OPEN.  Shorter than file-write cooldown because
# SQLite lock contention typically resolves quickly.
SQLITE_WRITE_CIRCUIT_COOLDOWN_SECONDS: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# Conversation History Compression
# ─────────────────────────────────────────────────────────────────────────────

# Number of message lines (excluding header) in a chat's JSONL file that
# triggers automatic compression.  When exceeded, the oldest messages are
# archived and replaced with a summary stored alongside the JSONL file.
# This keeps disk I/O and reverse-seek latency bounded for long-lived chats.
COMPRESSION_LINE_THRESHOLD: int = 5000

# Number of recent message lines to retain after compression.  The summary
# record plus these messages form the new file content.  Must be well below
# COMPRESSION_LINE_THRESHOLD to prevent immediate re-compression.
COMPRESSION_KEEP_RECENT: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Generation Counter (Write-Conflict Detection)
# ─────────────────────────────────────────────────────────────────────────────

# Maximum entries in the per-chat generation counter dict.
# Prevents unbounded memory growth for long-running bots with thousands of chats.
# Uses FIFO eviction (oldest entries removed first) when the cap is exceeded.
MAX_CHAT_GENERATIONS: int = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# SQLite Connection Pool
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of concurrent SQLite connections tracked by the shared pool.
# Caps file handle usage across all databases (vector_memory, projects, etc.).
# Each component typically holds 1 write connection; VectorMemory adds per-thread
# read connections.  20 accommodates the main writer threads plus several read
# threads without exhausting file descriptors.
SQLITE_POOL_MAX_CONNECTIONS: int = 20

# Maximum number of idle connections retained for reuse in the shared pool.
# Idle connections are LRU-evicted when this cap is exceeded, reducing
# connection setup overhead (directory creation, PRAGMA execution) for
# repeated access to the same database paths.
SQLITE_POOL_MAX_IDLE_CONNECTIONS: int = 5
