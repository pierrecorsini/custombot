"""Scheduler constants — retry, task timeout, error detection, HMAC integrity."""

from __future__ import annotations

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
# Scheduled Task Input Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum allowed length for a scheduled task prompt in characters.
# Enforced in TaskScheduler._validate_task() to prevent oversized prompts
# from wasting LLM API credits and exceeding token budgets.
MAX_SCHEDULED_PROMPT_LENGTH: int = 10_000

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
# Scheduler HMAC Integrity
# ─────────────────────────────────────────────────────────────────────────────

# Environment variable name for the optional HMAC-SHA256 secret used to sign
# scheduler task files.  When set, tasks.json is signed on write and verified
# on load — protecting against tampering by attackers with write access to the
# workspace.  When unset (default), signing is disabled for backward
# compatibility.
SCHEDULER_HMAC_SECRET_ENV: str = "SCHEDULER_HMAC_SECRET"

# File extension for the sidecar HMAC signature file stored alongside
# tasks.json.  Contains a single line with the hex digest.
SCHEDULER_HMAC_SIG_EXT: str = ".hmac"
