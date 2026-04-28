"""Messaging constants — queue limits, outbound dedup, event bus."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Message Queue Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum text length (characters) for messages persisted to the crash-recovery
# queue.  Messages longer than this are truncated during enqueue so the JSONL
# file does not grow unboundedly.  The full text is still passed through to
# the bot for normal processing — only the queue copy is capped.
MAX_QUEUED_TEXT_LENGTH: int = 10_000

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
# Event Bus Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of handler callbacks per event name.
# Prevents unbounded subscription growth from misbehaving plugins.
EVENT_BUS_MAX_HANDLERS_PER_EVENT: int = 50
