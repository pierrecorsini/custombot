"""LLM provider constants — config defaults, circuit breaker, streaming, ReAct retry, log rotation."""

from __future__ import annotations

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
# LLM Streaming Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Minimum number of accumulated characters before forwarding a partial
# text delta to the stream callback.  Batching reduces the number of
# channel sends (each is a separate WhatsApp message) while still
# providing timely feedback.
STREAM_MIN_CHUNK_CHARS: int = 80

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

# Interval (seconds) between proactive health probes while the LLM circuit
# breaker is OPEN.  A background task polls models.list() at this interval;
# on success the breaker is force-closed, allowing traffic to resume without
# waiting for the full cooldown.  Shorter intervals detect recovery faster
# but generate more API requests; 10s balances speed against overhead.
LLM_HEALTH_PROBE_INTERVAL_SECONDS: float = 10.0

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
# Retry Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default maximum number of retry attempts for transient failures.
DEFAULT_MAX_RETRIES: int = 3

# Default delay between retries (in seconds).
DEFAULT_RETRY_DELAY: float = 1.0

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
