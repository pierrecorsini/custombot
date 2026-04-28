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
