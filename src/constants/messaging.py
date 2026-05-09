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
# Batched Fsync Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Number of pending writes to accumulate before issuing an os.fsync().
# Each fsync costs ~1-5ms on HDD/NFS; batching amortises this cost across
# multiple messages under burst traffic.  Set to 1 to disable batching
# (every write is immediately fsynced for maximum durability).
# Default of 10 trades ~50ms worst-case data loss for 10× throughput gain.
QUEUE_FSYNC_BATCH_SIZE: int = 10

# Maximum time (seconds) to hold writes before flushing, even if the batch
# size threshold has not been reached.  Caps worst-case data loss to this
# window during low-throughput periods.
QUEUE_FSYNC_INTERVAL_SECONDS: float = 0.05  # 50ms

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

# Maximum number of buffered outbound recordings before an early flush is
# forced.  During burst delivery, record_outbound() appends (chat_id, text)
# pairs to a plain list that is flushed lazily.  Without a cap, a sustained
# burst could grow the list unboundedly, spiking memory.  When the cap is
# hit, the buffer is flushed immediately and a warning is logged.
OUTBOUND_DEDUP_BUFFER_MAX_SIZE: int = 1_000

# ─────────────────────────────────────────────────────────────────────────────
# Inbound Message Dedup LRU Cache
# ─────────────────────────────────────────────────────────────────────────────

# TTL (seconds) for the inbound dedup LRU cache.  After a message_id is looked
# up in the database, the result is cached to avoid repeated async DB calls.
# True duplicates arrive within seconds; unique IDs never need re-checking
# after the first miss ages out.
INBOUND_DEDUP_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes

# Maximum number of inbound dedup cache entries.  Bounded LRU eviction
# prevents unbounded memory growth under high-throughput bursts.
INBOUND_DEDUP_CACHE_MAX_SIZE: int = 10_000

# ─────────────────────────────────────────────────────────────────────────────
# Request Dedup (content-hash within per-chat lock scope)
# ─────────────────────────────────────────────────────────────────────────────

# TTL (seconds) for the per-chat request dedup cache.  When a message or
# scheduled task enters the per-chat lock scope, its content is hashed and
# checked against recently-seen inputs for the same chat.  A short window
# (30 s) catches double-sends and scheduled-vs-manual collisions without
# blocking legitimate follow-up messages.
REQUEST_DEDUP_TTL_SECONDS: float = 30.0

# Maximum number of per-chat request dedup entries.  Bounded LRU eviction
# prevents unbounded memory growth.  Each entry is an xxh64 hex digest.
REQUEST_DEDUP_MAX_SIZE: int = 500

# Maximum text length (characters) included in the request dedup hash.
# Longer texts are truncated before hashing so that very long messages do
# not produce unique hashes due to trailing variation.
REQUEST_DEDUP_HASH_TEXT_LENGTH: int = 500

# Maximum text length (characters) included in outbound dedup xxh64 hashes.
# Matches the request-dedup truncation pattern: prevents slow hashing on
# huge LLM responses while still catching duplicate content (responses that
# agree on the first 4096 characters are overwhelmingly identical).
MAX_OUTBOUND_HASH_TEXT_LENGTH: int = 4096

# ─────────────────────────────────────────────────────────────────────────────
# Inbound Dedup Batch Size
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of message IDs processed in a single batch_check_inbound call.
# Caps memory and CPU overhead when crash-recovery backlogs contain thousands
# of stale messages.  Larger values reduce overhead but increase per-batch
# latency; 50 balances both concerns for typical deployments.
DEDUP_BATCH_SIZE: int = 50

# ─────────────────────────────────────────────────────────────────────────────
# Event Bus Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of handler callbacks per event name.
# Prevents unbounded subscription growth from misbehaving plugins.
EVENT_BUS_MAX_HANDLERS_PER_EVENT: int = 50
