<!-- Context: project/concepts/react-loop | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Concept: ReAct Loop Pipeline

**Core Idea**: Every incoming WhatsApp message passes through an 8-stage pipeline — dedup check, rate limit, per-chat lock, queue persistence, routing, context assembly, then a ReAct (Reason+Act) loop where the LLM iteratively calls tools until it produces a final text response.

**Source**: `README.md` — Message Processing Pipeline section

---

## Key Points

- **Deduplication**: Message IDs checked against DB before processing — prevents double-handling
- **Rate limiting**: 30 messages/minute per chat (configurable) — sliding window rejection
- **Concurrency**: LRU-based per-chat locks ensure only one LLM call per chat at a time
- **ReAct loop**: LLM outputs either `tool_calls` (execute skill → append result → loop) or `stop` (return text to WhatsApp)
- **Max iteration guard**: Loop capped at `max_tool_iterations` (default 10) to prevent infinite loops

---

## Pipeline Stages

```
1. Dedup Check    — message_id in DB? → SKIP
2. Rate Limit     — 30 msgs/min per chat? → REJECT
3. Per-Chat Lock  — one LLM call per chat at a time
4. Enqueue        — persist to crash recovery queue
5. Routing        — match rule → load instruction.md
6. Build Context  — history + MEMORY.md + AGENTS.md + projects
7. ReAct Loop     — messages → LLM → tool_calls? → execute → loop
                   └─ or → stop → return text → WhatsApp
8. Persist        — save response to DB, mark queue done
```

---

## ReAct Loop Detail

```
messages ──▶ LLM ──▶ finish_reason?
                    │
           ┌────────┴────────┐
           ▼                 ▼
      "tool_calls"        "stop"
           │                 │
           ▼                 ▼
    Execute Skill(s)    Return text ──▶ WhatsApp
    (rate limited,
     error handled)
           │
           ▼
    Append tool result
    to messages ──▶ LOOP
```

---

## Key Properties

| Property | Detail |
|----------|--------|
| Streaming | Optional real-time tool execution updates sent to WhatsApp |
| Instruction caching | mtime-based cache avoids repeated disk reads |
| Crash recovery | Queue entry marked PENDING → COMPLETED on success |
| Context injection | Project recall (knowledge graph) injected into every LLM call |

---

## Codebase

- `src/bot.py` — Core ReAct loop orchestrator (`handle_message`, `_process_tool_calls`)
- `src/core/context_builder.py` — Assembles history + memory + instructions
- `src/core/tool_executor.py` — Executes skills with rate limiting + metrics
- `src/message_queue.py` — Persistent queue for crash recovery
- `src/rate_limiter.py` — Sliding window per-chat limits

## Related

- `concepts/routing-engine.md` — How messages get routed before the loop
- `concepts/per-chat-memory.md` — MEMORY.md + AGENTS.md context sources
- `lookup/workspace-structure.md` — Where queue and data files live
