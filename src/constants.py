"""
constants.py — Named constants for custombot.

Centralizes magic numbers and configuration defaults for better maintainability
and code readability. All values are documented with their purpose.

Usage:
    from src.constants import MAX_LRU_CACHE_SIZE, DEFAULT_HTTP_TIMEOUT

Categories:
    - LRU Cache: Limits for bounded caches
    - HTTP/Network: Timeouts and connection limits
    - LLM: Token limits and iteration bounds
    - WhatsApp: Client configuration
    - Memory: History and storage limits

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

# ─────────────────────────────────────────────────────────────────────────────
# LRU Cache Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of locks to retain in the LRU cache.
# Used for per-chat locks to prevent unbounded memory growth.
# Each chat gets its own lock; 1000 concurrent chats is a reasonable upper bound.
MAX_LRU_CACHE_SIZE: int = 1000

# Maximum number of pooled file handles for Database message-file appends.
# Prevents OS file-descriptor exhaustion (EMFILE / "Too many open files")
# under extreme concurrency by reusing open handles instead of open/close
# per write.  256 is well under typical OS limits (Linux soft 1024,
# Windows 512, macOS 256) and leaves headroom for other file operations.
MAX_FILE_HANDLES: int = 256

# ─────────────────────────────────────────────────────────────────────────────
# HTTP / Network Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for HTTP requests (in seconds).
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Connection timeout for HTTP requests (in seconds).
DEFAULT_HTTP_CONNECT_TIMEOUT: float = 10.0

# Maximum number of concurrent TCP connections in the httpx connection pool
# used by the OpenAI client. Under high concurrency (many chats hitting the
# LLM simultaneously), connection reuse eliminates TCP handshake overhead.
DEFAULT_HTTPX_MAX_CONNECTIONS: int = 20

# Maximum number of idle keep-alive connections to retain in the pool.
# Must be <= DEFAULT_HTTPX_MAX_CONNECTIONS.  Keep-alive avoids reconnect
# overhead for sequential requests to the same provider endpoint.
DEFAULT_HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = 10

# Timeout for the LLM connection warmup request during startup (seconds).
# Sends a lightweight models.list() call to pre-establish the TCP + TLS
# connection before the first user message arrives.  Failures are non-fatal.
LLM_WARMUP_TIMEOUT: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# LLM Configuration Defaults
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of tool call iterations in the ReAct loop.
# Prevents infinite loops when tools keep triggering more tools.
MAX_TOOL_ITERATIONS: int = 10

# Maximum number of parallel tool calls the LLM can request in a single turn.
# A confused or prompt-injected LLM could request 50+ concurrent tool calls,
# exhausting system resources (file handles, thread pool, memory).  Excess
# calls are rejected with a warning fed back to the LLM so it can prioritise.
MAX_TOOL_CALLS_PER_TURN: int = 10

# Maximum tokens for LLM responses.
# GPT-4 models typically have 4096 output token limits.
DEFAULT_MAX_TOKENS: int = 4096

# Default timeout for LLM API calls (in seconds).
# LLM calls can be slow, especially with long contexts.
DEFAULT_LLM_TIMEOUT: float = 120.0  # 2 minutes

# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum time to wait for QR code scan (in seconds).
MAX_QR_SCAN_WAIT: int = 60

# Maximum time to wait for the channel to connect during startup (in seconds).
# Covers QR scan, neonize handshake, and initial sync.  If the channel
# hasn't connected within this window, startup is aborted with a clear error.
DEFAULT_CHANNEL_STARTUP_TIMEOUT: float = 300.0  # 5 minutes

# ─────────────────────────────────────────────────────────────────────────────
# Memory / History Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of recent messages to include in LLM context.
# Balances context quality against token costs and latency.
DEFAULT_MEMORY_MAX_HISTORY: int = 50

# Heuristic: approximate characters per token for English text.
# Used for fast token estimation without external dependencies (tiktoken).
# English averages ~4 chars/token; mixed/multilingual is lower (~3).
CHARS_PER_TOKEN: int = 4

# Heuristic: approximate characters per token for CJK text
# (Chinese, Japanese, Korean).  CJK characters each represent a word or
# morpheme and tokenize to roughly 1-2 tokens per character, so we use 1.5
# as the ratio — significantly lower than the English 4 chars/token.
CJK_CHARS_PER_TOKEN: float = 1.5

# Total token budget for system prompt + history sent to the LLM.
# Set conservatively to fit within typical model context windows (128k).
# Leaves headroom for the model's response tokens.
DEFAULT_CONTEXT_TOKEN_BUDGET: int = 100_000

# ─────────────────────────────────────────────────────────────────────────────
# Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default maximum number of retry attempts for transient failures.
DEFAULT_MAX_RETRIES: int = 3

# Default delay between retries (in seconds).
DEFAULT_RETRY_DELAY: float = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Shutdown Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for graceful shutdown (in seconds).
DEFAULT_SHUTDOWN_TIMEOUT: float = 30.0

# Delay between shutdown progress log messages (in seconds).
SHUTDOWN_LOG_INTERVAL: float = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# Skill Execution Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for skill execution (in seconds).
# Skills can perform file I/O, shell commands, etc.
# Give them reasonable time but prevent indefinite hangs.
DEFAULT_SKILL_TIMEOUT: float = 60.0  # 1 minute

# Threshold for slow skill execution warning (in seconds).
# Skills exceeding this duration trigger a warning log.
SLOW_SKILL_THRESHOLD_SECONDS: float = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# LLM Streaming Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Minimum number of accumulated characters before forwarding a partial
# text delta to the stream callback.  Batching reduces the number of
# channel sends (each is a separate WhatsApp message) while still
# providing timely feedback.
STREAM_MIN_CHUNK_CHARS: int = 80

# ─────────────────────────────────────────────────────────────────────────────
# Database Operation Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for database operations (in seconds).
# File-based JSON operations should be quick.
DEFAULT_DB_TIMEOUT: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default rate limit for skill calls per chat (calls per minute).
# Prevents abuse from a single conversation.
DEFAULT_CHAT_RATE_LIMIT: int = 30

# Default rate limit for expensive skills (calls per minute).
# Expensive skills include web_search, web_fetch, etc.
DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT: int = 10

# Rate limit window size in seconds.
RATE_LIMIT_WINDOW_SECONDS: float = 60.0

# Maximum number of chats to track for rate limiting.
# Prevents unbounded memory growth.
MAX_RATE_LIMIT_TRACKED_CHATS: int = 1000

# Minimum and maximum allowed values for rate-limit env vars.
# Prevents misconfiguration (e.g. RATE_LIMIT_CHAT_PER_MINUTE=999999)
# from effectively disabling rate limiting.
RATE_LIMIT_MIN_VALUE: int = 1
RATE_LIMIT_MAX_VALUE: int = 100

# ─────────────────────────────────────────────────────────────────────────────
# Health Server Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

# Maximum HTTP requests per IP to the health server within the rate-limit window.
HEALTH_HTTP_RATE_LIMIT: int = 60

# Sliding window size (seconds) for health server rate limiting.
HEALTH_HTTP_RATE_WINDOW_SECONDS: float = 60.0

# Maximum distinct IPs to track for health server rate limiting.
HEALTH_HTTP_RATE_MAX_TRACKED_IPS: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Memory Monitoring Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default warning threshold for memory usage (percentage).
# When system memory usage exceeds this, a warning is logged.
MEMORY_WARNING_THRESHOLD_PERCENT: float = 80.0

# Default critical threshold for memory usage (percentage).
# When system memory usage exceeds this, an error is logged.
MEMORY_CRITICAL_THRESHOLD_PERCENT: float = 90.0

# Default interval for periodic memory checks (seconds).
# How often the memory monitor logs usage stats.
MEMORY_CHECK_INTERVAL_SECONDS: float = 60.0

# ─────────────────────────────────────────────────────────────────────────────
# Workspace Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Root workspace directory for all bot data.
# Contains: .data/, auth/, logs/, skills/, whatsapp_data/
WORKSPACE_DIR: str = "workspace"

# ─────────────────────────────────────────────────────────────────────────────
# Routing Engine — File Watching
# ─────────────────────────────────────────────────────────────────────────────

# Minimum interval (seconds) between stale-checks on instruction .md files.
# Prevents redundant stat() calls when match() is invoked at high frequency.
ROUTING_WATCH_DEBOUNCE_SECONDS: float = 1.0

# TTL (seconds) for the routing match result cache. Identical message signatures
# within this window return the cached match result without re-evaluating rules.
ROUTING_MATCH_CACHE_TTL_SECONDS: float = 5.0

# Maximum number of cached routing match results. Bounded to prevent unbounded
# memory growth; evicts least-recently-used entries when full.
ROUTING_MATCH_CACHE_MAX_SIZE: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breaker Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Number of consecutive LLM failures before opening the circuit breaker.
# Once this threshold is reached, new requests are rejected immediately
# without waiting for the full LLM timeout.
CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5

# Duration (seconds) the circuit breaker stays OPEN before transitioning
# to HALF_OPEN to probe whether the LLM provider has recovered.
CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 60.0

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
# ReAct Loop Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of retry attempts for transient LLM errors inside _react_loop.
# Retries rate-limit, timeout, and connection errors before propagating to the
# caller.  2 retries keeps total worst-case latency manageable (~3× LLM call).
REACT_LOOP_MAX_RETRIES: int = 2

# Initial delay (seconds) before the first retry of a transient LLM error.
# Uses exponential backoff with jitter (see calculate_delay_with_jitter).
REACT_LOOP_RETRY_INITIAL_DELAY: float = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Scheduler Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of retry attempts for transient failures in scheduled tasks.
# After all retries are exhausted, the task fails until the next scheduled interval.
SCHEDULER_MAX_RETRIES: int = 2

# Initial delay (seconds) before first retry of a failed scheduled task.
# Uses exponential backoff with jitter: ~30s, then ~60s on the second retry.
SCHEDULER_RETRY_INITIAL_DELAY: float = 30.0

# Maximum wall-clock time (seconds) for a single scheduled task execution.
# Covers the trigger callback (LLM call) plus all retry attempts with backoff.
# Prevents a stuck task from blocking the scheduler tick indefinitely.
DEFAULT_SCHEDULER_TASK_TIMEOUT: float = 300.0  # 5 minutes

# ─────────────────────────────────────────────────────────────────────────────
# Scheduled Task Error Detection
# ─────────────────────────────────────────────────────────────────────────────

# Known error prefixes returned by _react_loop() that should never be
# persisted as normal assistant messages in scheduled tasks.  Covers circuit-
# breaker responses, empty LLM output, and max-iteration warnings.
SCHEDULED_ERROR_PREFIXES: tuple[str, ...] = (
    "⚠️ Service temporarily unavailable",
    "(The assistant generated an empty response",
    "⚠️ Reached maximum tool iterations",
)

# ─────────────────────────────────────────────────────────────────────────────
# LLM Log Rotation
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of LLM log files to retain in workspace/logs/llm/.
# Each LLM call produces two files (request + response), so 200 files ≈ 100 calls.
LLM_LOG_MAX_FILES: int = 200

# Maximum age (days) for LLM log files. Files older than this are deleted during
# cleanup, regardless of the file count limit.
LLM_LOG_MAX_AGE_DAYS: int = 30

# Number of writes between automatic cleanup sweeps. Avoids scanning the directory
# on every single write while still keeping the log count bounded.
LLM_LOG_CLEANUP_INTERVAL: int = 20

# ─────────────────────────────────────────────────────────────────────────────
# Input Validation Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum allowed message length in characters.
# Messages exceeding this are rejected before reaching the LLM API,
# preventing token overflow and excessive costs.
MAX_MESSAGE_LENGTH: int = 50_000

# Maximum allowed length for chat_id in characters.
# Enforced at the IncomingMessage boundary as a defense-in-depth guard:
# prevents excessively long strings from reaching filesystem operations
# (workspace directory names, JSONL paths, metric labels).  200 chars is
# well above any real chat ID (~50 chars for WhatsApp JIDs) while staying
# within filesystem name limits (255 bytes).
MAX_CHAT_ID_LENGTH: int = 200

# ─────────────────────────────────────────────────────────────────────────────
# Message Queue Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum text length (characters) for messages persisted to the crash-recovery
# queue.  Messages longer than this are truncated during enqueue so the JSONL
# file does not grow unboundedly.  The full text is still passed through to
# the bot for normal processing — only the queue copy is capped.
MAX_QUEUED_TEXT_LENGTH: int = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# Event Bus Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of handler callbacks per event name.
# Prevents unbounded subscription growth from misbehaving plugins.
EVENT_BUS_MAX_HANDLERS_PER_EVENT: int = 50

# ─────────────────────────────────────────────────────────────────────────────
# Workspace Cleanup Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default interval (seconds) between periodic workspace size checks and cleanup.
# A typical workspace has JSONL conversation files, vector memory, LLM logs,
# and backups that accumulate over time.
WORKSPACE_CLEANUP_INTERVAL_SECONDS: float = 3600.0  # 1 hour

# Maximum age (days) for JSONL conversation files before they are archived
# into a compressed .tar.gz.  Active conversations are never touched.
WORKSPACE_ARCHIVE_MAX_AGE_DAYS: int = 30

# Maximum age (days) for backup files before they are pruned.
WORKSPACE_BACKUP_MAX_AGE_DAYS: int = 7

# Maximum age (days) for stale temporary files (e.g., .tmp from crashed
# atomic writes) before they are removed.
WORKSPACE_STALE_TEMP_MAX_AGE_HOURS: float = 1.0

# Workspace size threshold (MB) at which the health check reports DEGRADED.
# Helps operators detect unbounded disk growth.
WORKSPACE_SIZE_WARNING_MB: float = 1024.0

# ─────────────────────────────────────────────────────────────────────────────
# Outbound Message Dedup
# ─────────────────────────────────────────────────────────────────────────────

# TTL (seconds) for the outbound message dedup cache. If the same response
# content was already sent to a chat within this window, the duplicate is
# silently skipped.  Prevents double-sends when scheduler retries succeed
# after the first attempt already delivered a response.
OUTBOUND_DEDUP_TTL_SECONDS: float = 60.0

# Maximum number of dedup entries to retain. Bounded LRU eviction prevents
# unbounded memory growth.  Each entry is a SHA-256 hex digest + timestamp.
OUTBOUND_DEDUP_MAX_SIZE: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Health Check — Disk Space
# ─────────────────────────────────────────────────────────────────────────────

# Minimum free disk space (MB) for the health endpoint to report HEALTHY.
# Below this threshold the component status is DEGRADED, signalling that
# writes to JSONL, vector memory, or the message queue may start failing.
HEALTH_DISK_FREE_THRESHOLD_MB: float = 500.0

# Maximum allowed request body size (bytes) for the health server.
# Health endpoints only serve short GET requests with no body; any POST
# or PUT payload exceeding this is rejected to prevent memory exhaustion.
HEALTH_MAX_REQUEST_BODY_BYTES: int = 1024  # 1 KB

# Maximum allowed URL path length (characters) for the health server.
# Legitimate paths are short (/, /health, /metrics, /ready, /version).
# Excessively long paths are rejected to prevent memory exhaustion.
HEALTH_MAX_URL_LENGTH: int = 2048  # 2 KB

# ─────────────────────────────────────────────────────────────────────────────
# Config Hot-Reload Watcher
# ─────────────────────────────────────────────────────────────────────────────

# How often (seconds) the config watcher polls config.json for mtime changes.
# Longer intervals reduce filesystem syscalls; shorter intervals apply changes
# faster after the file is saved.
CONFIG_WATCH_INTERVAL_SECONDS: float = 5.0

# Minimum interval (seconds) between mtime checks. Prevents redundant stat()
# calls when the watch loop fires faster than expected.
CONFIG_WATCH_DEBOUNCE_SECONDS: float = 2.0

# ─────────────────────────────────────────────────────────────────────────────
# Thread Pool Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default maximum number of worker threads for the asyncio ThreadPoolExecutor.
# This executor backs all asyncio.to_thread() calls (database reads/writes,
# file I/O, psutil calls, vector memory operations).  Under high concurrency
# (many chats active simultaneously), the default pool
# (min(32, os.cpu_count()+4)) can saturate, causing to_thread calls to queue.
# 16 workers balances concurrency against thread overhead.
DEFAULT_THREAD_POOL_WORKERS: int = 16

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
# Safe-Mode Confirmation Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of invalid input attempts before auto-rejecting a safe-mode
# send confirmation.  Prevents an infinite prompt loop from misconfigured or
# automated input sources.
SAFE_MODE_MAX_CONFIRM_RETRIES: int = 3

# ─────────────────────────────────────────────────────────────────────────────
# Tool Result Persistence Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum character length for tool results persisted to the JSONL conversation
# history via buffered_persist.  Skill results exceeding this are truncated with
# a suffix indicating the full length.  The complete result is still available in
# the in-memory messages list for the current ReAct iteration.
MAX_TOOL_RESULT_PERSIST_LENGTH: int = 10_000
