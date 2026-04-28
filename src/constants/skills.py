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

# ─────────────────────────────────────────────────────────────────────────────
# Tool Result Persistence Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum character length for tool results persisted to the JSONL conversation
# history via buffered_persist.  Skill results exceeding this are truncated with
# a suffix indicating the full length.  The complete result is still available in
# the in-memory messages list for the current ReAct iteration.
MAX_TOOL_RESULT_PERSIST_LENGTH: int = 10_000
