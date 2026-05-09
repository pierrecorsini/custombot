"""Security constants — input validation limits, rate limiting configuration."""

from __future__ import annotations

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

# Maximum allowed length for message_id in characters.
# Enforced at the IncomingMessage boundary to prevent excessively long IDs
# from corrupting dedup indexes, logs, or metric labels.  200 chars is well
# above any real message ID (~70 chars for WhatsApp, ~36 for UUIDs).
MAX_MESSAGE_ID_LENGTH: int = 200

# Maximum allowed length for sender_id in characters.
# Enforced at the IncomingMessage boundary to prevent malicious sender IDs
# from corrupting logs or filesystem paths.  200 chars matches
# MAX_CHAT_ID_LENGTH and is well above any real sender ID.
MAX_SENDER_ID_LENGTH: int = 200

# Maximum allowed length for sender_name in characters.
# Enforced at the IncomingMessage boundary to prevent excessively long names
# from polluting logs, metric labels, or UI output.  200 chars is generous
# for display names while staying within log-line budgets.
MAX_SENDER_NAME_LENGTH: int = 200

# Maximum allowed length for correlation_id in characters.
# Enforced at the IncomingMessage boundary to prevent unreasonably long
# tracing IDs from reaching logging and observability backends.
MAX_CORRELATION_ID_LENGTH: int = 200

# Maximum allowed serialized size of the ``raw`` channel payload in bytes.
# Enforced at the IncomingMessage boundary to prevent malicious or misconfigured
# channels from injecting arbitrarily large payloads that cause memory pressure
# in the frozen dataclass, downstream logging, and crash-recovery persistence.
# 64 KB is generous for debugging purposes while capping pathological inputs.
MAX_RAW_PAYLOAD_SIZE: int = 65_536

# Reasonable bounds for the ``timestamp`` field (Unix epoch, float).
# Rejects NaN / Inf / negative values and timestamps far outside any
# plausible message-window.  0 = 1970-01-01; 4_102_444_800 = 2100-01-01.
TIMESTAMP_MIN: float = 0.0
TIMESTAMP_MAX: float = 4_102_444_800.0

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

# Advisory effective maximum for rate-limit env vars.
# Values above this are not clamped (they are still within MAX_VALUE) but
# trigger a loud warning, because in containerized deployments a
# misconfigured env var close to MAX_VALUE effectively disables rate limiting.
RATE_LIMIT_EFFECTIVE_MAX: int = 60

# Maximum allowed size for instruction .md files in bytes.
# Enforced by InstructionLoader before reading to prevent a compromised or
# accidentally huge instruction file from exhausting memory when loaded into
# the LLM context.  1 MiB is generous for text instructions while capping
# pathological inputs (security-gaps.md item #4).
MAX_INSTRUCTION_FILE_SIZE: int = 1_048_576  # 1 MiB

# ─────────────────────────────────────────────────────────────────────────────
# Injection Detection Thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Confidence threshold above which scheduled-task prompts are rejected outright
# instead of being sanitized and forwarded to the LLM.  Set at 0.8 so that
# high-confidence patterns (0.9) are always blocked while medium-confidence
# patterns (0.6) continue to be logged and allowed through with sanitization.
INJECTION_BLOCK_CONFIDENCE: float = 0.8

# ─────────────────────────────────────────────────────────────────────────────
# Error Reply Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

# Maximum error replies per chat per window.
# Prevents error-message amplification attacks where an attacker triggers
# many errors to flood a chat with bot replies.
ERROR_REPLY_RATE_LIMIT: int = 5

# Sliding window duration (seconds) for error-reply rate limiting.
ERROR_REPLY_WINDOW_SECONDS: float = 60.0
