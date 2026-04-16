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

# ─────────────────────────────────────────────────────────────────────────────
# HTTP / Network Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for HTTP requests (in seconds).
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Connection timeout for HTTP requests (in seconds).
DEFAULT_HTTP_CONNECT_TIMEOUT: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# LLM Configuration Defaults
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of tool call iterations in the ReAct loop.
# Prevents infinite loops when tools keep triggering more tools.
MAX_TOOL_ITERATIONS: int = 10

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

# ─────────────────────────────────────────────────────────────────────────────
# Memory / History Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of recent messages to include in LLM context.
# Balances context quality against token costs and latency.
DEFAULT_MEMORY_MAX_HISTORY: int = 50

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
# Input Validation Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum allowed message length in characters.
# Messages exceeding this are rejected before reaching the LLM API,
# preventing token overflow and excessive costs.
MAX_MESSAGE_LENGTH: int = 50_000
