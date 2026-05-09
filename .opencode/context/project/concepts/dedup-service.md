<!-- Context: project/concepts/dedup-service | Priority: medium | Version: 1.1 | Updated: 2026-05-04 -->

# Concept: Deduplication Service

**Core Idea**: A unified `DeduplicationService` that consolidates inbound (message-id) and outbound (content-hash) deduplication behind a single API with stats tracking. Prevents duplicate message processing and redundant outgoing responses. Uses in-memory LRU cache for inbound fast-path and fail-open behavior on DB errors.

**Source**: `src/core/dedup.py`

---

## Key Points

- **Inbound dedup**: Two-tier — in-memory LRU cache (10K entries, 5-min TTL) as fast-path, then database fallback for persistent cross-restart dedup
- **Fail-open on DB errors**: If database throws `DatabaseError`, logs warning and returns `False` (allows message through) — dedup miss is preferable to message loss
- **Outbound dedup**: Uses xxHash (xxh64) content hash with TTL-based `BoundedOrderedDict` cache — prevents sending identical responses within the TTL window
- **Single hash path**: `check_and_record_outbound()` computes hash once for both check and record (eliminates redundant xxh64)
- **Unified stats**: Tracks inbound_hits, outbound_hits, and outbound_recordings for monitoring

---

## How It Works

```
Inbound Flow (fast-path):
  Message → DeduplicationService.is_inbound_duplicate(message_id)
    → LRU cache lookup → hit? → True (skip)
    → miss → Database.has_message(message_id) → True/False
    → on DatabaseError → log warning, return False (fail-open)

Outbound Flow (single hash):
  Response → DeduplicationService.check_and_record_outbound(chat_id, text)
    → xxh64(text) computed once → cache lookup → hit? → True (skip)
    → miss → record to cache → return False (allow)
```

---

## Dataclass

```
DedupStats:
  inbound_checks: int     # Total inbound dedup queries
  inbound_hits: int       # Duplicate inbound messages blocked
  outbound_recordings: int # Outbound hashes recorded
  outbound_hits: int      # Duplicate outbound responses blocked
```

---

## Codebase

- `src/core/dedup.py` — DeduplicationService, DedupStats, LRU inbound cache, fail-open on DB error
- `src/db/db.py` — Database.has_message() for inbound index queries
- `src/bot/_bot.py` — Calls is_inbound_duplicate() on each incoming message
- `src/scheduler.py` — Uses outbound dedup to skip identical scheduled responses
- `src/utils/__init__.py` — BoundedOrderedDict (LRU cache with TTL)

## Related

- `concepts/crash-recovery.md` — Why inbound dedup matters after crash
- `concepts/task-scheduler.md` — Where outbound dedup prevents duplicate sends
