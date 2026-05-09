"""Shutdown constants — graceful shutdown timeout, log interval, cleanup step timeout."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Shutdown Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for graceful shutdown (in seconds).
DEFAULT_SHUTDOWN_TIMEOUT: float = 30.0

# Delay between shutdown progress log messages (in seconds).
SHUTDOWN_LOG_INTERVAL: float = 5.0

# Timeout per individual cleanup step during shutdown (in seconds).
# Prevents a single hung component from blocking the entire shutdown sequence.
CLEANUP_STEP_TIMEOUT: float = 10.0

# Maximum number of messages processed concurrently by Application._on_message().
# Caps memory usage and LLM rate-limit pressure under load without blocking the
# event loop — excess messages wait for a free slot via asyncio.Semaphore.
DEFAULT_MAX_CONCURRENT_MESSAGES: int = 10

# Default per-chat processing timeout (in seconds).
# Wraps the entire _process() call (context assembly + ReAct loop + response
# delivery) inside asyncio.wait_for().  When exceeded, the stuck turn is
# cancelled and the per-chat lock is released so subsequent messages can be
# processed.  300s (5 min) accommodates multi-tool ReAct turns while preventing
# indefinite blocking from hung LLM calls or tool executions.
DEFAULT_PER_CHAT_TIMEOUT: float = 300.0
