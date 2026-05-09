<!-- Context: project/concepts/crash-recovery | Priority: high | Version: 1.1 | Updated: 2026-05-04 -->

# Concept: Crash Recovery (Message Queue)

**Core Idea**: Persistent message queue survives process crashes. Messages enter as PENDING, transition to COMPLETED on success, or become STALE if the process dies mid-processing. On restart, stale messages are automatically requeued. Uses msgpack for fast serialization with JSON fallback for crash recovery readability.

**Source**: `src/message_queue.py`, `src/message_queue_persistence.py`

---

## Key Points

- **Persistent storage**: `.workspace/.data/message_queue.jsonl` — survives process termination
- **Three states**: `PENDING` → `COMPLETED` (success) or `STALE` (crash/timeout)
- **Auto-recovery on startup**: `Bot.recover_pending_messages()` finds stale entries and reprocesses them
- **Stale detection**: PENDING entries exceeding timeout threshold are marked STALE
- **Ordered processing**: Messages enqueued before LLM call, marked done after response persisted
- **msgpack persistence**: ~3–5× faster than JSON for 10-field `QueuedMessage` objects; JSON fallback for crash recovery
- **Swap-buffers flush**: Write buffer detached under lock, flushed without blocking enqueue/complete
- **Timeout safety**: `_message_queue.complete()` called in `except asyncio.TimeoutError` handler to prevent stale reprocessing

---

## State Machine

```
PENDING ──────▶ COMPLETED          STALE
(enqueued      (processed OK)      (crashed during
 before                             processing)
 process)
                                         │
                                    ┌────┴──────┐
                                    │ Recovery   │
                                    │ (startup)  │
                                    │            │
                                    │ find stale │
                                    │ → requeue  │
                                    │ → process  │
                                    └───────────┘
```

---

## Pipeline Integration

```
Message arrives
     │
     ▼
1. Dedup check (skip if already in DB)
     │
     ▼
2. Enqueue as PENDING in message_queue.jsonl
     │
     ▼
3. Process: routing → context → ReAct loop → response
     │
     ├── Success ──▶ mark COMPLETED in queue
     └── Crash ────▶ entry stays PENDING → becomes STALE on next startup
```

---

## Quick Example

```jsonl
{"id":"msg-123","chat_id":"chat-456","status":"COMPLETED","ts":1712400000}
{"id":"msg-789","chat_id":"chat-456","status":"PENDING","ts":1712400060}
```

On restart, `msg-789` would be detected as stale and reprocessed.

---

## Codebase

- `src/message_queue.py` — Queue logic, recovery, swap-buffers flush (QueuedMessage with `__slots__`)
- `src/message_queue_persistence.py` — JSONL/msgpack file I/O, crash-recovery logic
- `src/bot/crash_recovery.py` — `recover_pending_messages()` called on startup
- `workspace/.data/message_queue.jsonl` — Runtime queue file

## Related

- `concepts/react-loop.md` — Full pipeline where crash recovery integrates
- `concepts/graceful-shutdown.md` — Clean shutdown prevents stale messages
- `lookup/workspace-structure.md` — Where queue file lives
