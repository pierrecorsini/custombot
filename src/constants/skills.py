"""Skill execution constants — timeouts, safe-mode limits, tool result persistence."""

from __future__ import annotations

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
# Safe-Mode Confirmation Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of invalid input attempts before auto-rejecting a safe-mode
# send confirmation.  Prevents an infinite prompt loop from misconfigured or
# automated input sources.
SAFE_MODE_MAX_CONFIRM_RETRIES: int = 3

# Timeout (in seconds) for each stdin read during safe-mode confirmation.
# Prevents indefinite blocking when stdin is a pipe or misconfigured environment.
SAFE_MODE_CONFIRM_TIMEOUT: float = 60.0

# ─────────────────────────────────────────────────────────────────────────────
# Tool Result Persistence Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum character length for tool results persisted to the JSONL conversation
# history via buffered_persist.  Skill results exceeding this are truncated with
# a suffix indicating the full length.  The complete result is still available in
# the in-memory messages list for the current ReAct iteration.
MAX_TOOL_RESULT_PERSIST_LENGTH: int = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# Per-Skill Circuit Breaker
# ─────────────────────────────────────────────────────────────────────────────

# Number of consecutive failures before a skill's circuit breaker opens.
# Once open, the skill is fast-failed (not executed) until the cooldown elapses.
SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3

# Seconds a skill's circuit breaker stays open before transitioning to HALF_OPEN.
# In HALF_OPEN a single probe call is allowed to test recovery.
SKILL_CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 60.0

# Maximum number of per-skill circuit breakers tracked in SkillBreakerRegistry.
# Once exceeded, the least-recently-used breaker is evicted, preventing unbounded
# memory growth from adversarial tool-call inputs that generate many unique names.
MAX_TRACKED_SKILL_BREAKERS: int = 200
