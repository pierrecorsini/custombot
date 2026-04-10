# Feature Deep-Dives

Detailed schemas and internals for every major subsystem of custombot.

---

## Message Processing Pipeline (ReAct Loop)

The core of the bot is a **ReAct (Reason + Act) loop** that processes every incoming message:

```
 WhatsApp Message
        │
        ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                        Message Processing                        │
 │                                                                  │
 │  1. Dedup Check ──── message_id already in DB? ──▶ SKIP        │
 │  2. Rate Limit ───── 30 msgs/min per chat? ─────▶ REJECT       │
 │  3. Per-Chat Lock ── one LLM call per chat at a time            │
 │  4. Enqueue ──────── persist to crash recovery queue             │
 │  5. Routing ──────── match rule → load instruction.md           │
 │  6. Build Context ── history + MEMORY.md + AGENTS.md + projects │
 │                                                                  │
 │  Then enter ReAct loop:                                         │
 │                                                                  │
 │  ┌──────────────────────────────────────────────┐               │
 │  │           ReAct Loop (max N iterations)       │               │
 │  │                                               │               │
 │  │  messages ──▶ LLM ──▶ finish_reason?          │               │
 │  │                         │                     │               │
 │  │              ┌──────────┴──────────┐          │               │
 │  │              ▼                     ▼          │               │
 │  │         "tool_calls"            "stop"        │               │
 │  │              │                     │          │               │
 │  │              ▼                     ▼          │               │
 │  │     Execute Skill(s)        Return text ──▶   │───────────▶ WhatsApp
 │  │     (with rate limiting     response          │               │
 │  │      + error handling)          │             │               │
 │  │              │                   │             │               │
 │  │              ▼                   │             │               │
 │  │     Append tool result          │             │               │
 │  │     to messages ──▶ LOOP ───────┘             │               │
 │  └──────────────────────────────────────────────┘               │
 │                                                                  │
 │  7. Persist ──────── save assistant response to DB               │
 │  8. Complete ──────── mark message done in crash queue           │
 └──────────────────────────────────────────────────────────────────┘
```

**Key properties:**
- **Deduplication**: Message IDs checked against DB before processing
- **Rate limiting**: 30 messages/minute per chat (configurable)
- **Concurrency**: LRU-based per-chat locks ensure one LLM call per chat at a time
- **Streaming**: Optional real-time tool execution updates sent to WhatsApp
- **Instruction caching**: mtime-based cache avoids repeated disk reads

---

## Message Routing Engine

Routes every incoming message to a different instruction file (persona) based on **who** sent it, **what** they said, and **where** it came from:

```
 Incoming Message
        │
        ▼
 ┌──────────────────────────────────────────────────────┐
 │               RoutingEngine.match()                   │
 │                                                        │
 │  Extract MatchingContext:                              │
 │    sender_id, chat_id, channel_type, text,            │
 │    fromMe, toMe                                        │
 │                                                        │
 │  Evaluate rules (sorted by priority, lowest first):   │
 │                                                        │
 │  Rule ──▶ fromMe? ──▶ toMe? ──▶ sender ──▶           │
 │           recipient ──▶ channel ──▶ content_regex      │
 │                                                        │
 │  First match ──▶ return (rule, instruction_file)      │
 │  No match   ──▶ return (None, None) → message ignored │
 └──────────────────────────────────────────────────────┘
        │
        ▼
   Load instruction file (with mtime cache)
        │
        ▼
   LLM receives specialized system prompt
```

**Rule schema:**

```json
{
  "id": "vip-user",
  "priority": 5,
  "sender": "1234567890",
  "recipient": "*",
  "channel": "*",
  "content_regex": "*",
  "instruction": "vip.md",
  "enabled": true,
  "fromMe": null,
  "toMe": null,
  "skillExecVerbose": "",
  "showErrors": true
}
```

| Field | Description |
|---|---|
| `priority` | Lower = evaluated first |
| `fromMe`/`toMe` | `null` = match all, `true` = only self, `false` = only others |
| `skillExecVerbose` | `""` = hidden, `"summary"` = tool list at bottom, `"full"` = real-time streaming |
| `showErrors` | Send error messages back to the channel |

**Managing routes via chat:**

```
You: "List all routing rules"          → routing_list skill
You: "Create a rule for 'order' → orders.md" → routing_add skill
You: "Delete rule 'abc-123'"           → routing_delete skill
```

---

## WhatsApp Channel (neonize)

Direct WhatsApp Web connection via **neonize** (Python ctypes binding for whatsmeow Go library). No Node.js, no HTTP bridge, no subprocess management.

```
 ┌─────────────────────────────────────────────────────────┐
 │                   NeonizeBackend                         │
 │                                                          │
 │  ┌─────────────┐         ┌──────────────────────┐       │
 │  │  Main async  │         │  Daemon Thread        │       │
 │  │  event loop  │◀───────│  neonize.connect()    │       │
 │  │             │  Queue  │  (Go event loop)      │       │
 │  │             │         │                       │       │
 │  │  on_message │         │  event handlers:      │       │
 │  │  callback   │         │   • on_message        │       │
 │  │             │         │   • on_connected      │       │
 │  └─────────────┘         │   • on_qr (pairing)   │       │
 │                          └──────────────────────┘       │
 │                                                          │
 │  Session: stored as SQLite DB (neonize default)         │
 │  First run: QR code in terminal → scan with WhatsApp    │
 │  Subsequent: auto-reconnect from saved session          │
 └─────────────────────────────────────────────────────────┘
```

**Safe mode** (`--safe` flag): Prompts Y/N before every outgoing message.

---

## Stealth / Anti-Detection

Human-like timing patterns using **log-normal distributions** for natural variation:

```
 Incoming message
        │
        ▼
 ┌──────────────────────────────────────────────────┐
 │              Stealth Delays                       │
 │                                                    │
 │  1. Read delay ─── log-normal, scaled by msg len  │
 │     <50 chars:  0.3–2.0s                          │
 │     <200 chars: 0.8–3.5s                          │
 │     200+ chars: 1.5–5.0s                          │
 │                                                    │
 │  2. Think delay ── log-normal 0.5–4.0s            │
 │                                                    │
 │  3. Send "typing..." indicator                     │
 │                                                    │
 │  4. Type delay ─── response_len / (50-80 chars/s) │
 │     capped at 8s                                   │
 │                                                    │
 │  5. Typing pause ── 30% chance, 0.5–2.0s          │
 │     (mid-typing, simulates re-reading)             │
 │                                                    │
 │  6. Per-chat cooldown: 3s minimum between replies │
 └──────────────────────────────────────────────────┘
        │
        ▼
   Send reply to WhatsApp
```

---

## Per-Chat Memory

Each chat gets an isolated file-based memory with corruption detection:

```
 .workspace/whatsapp_data/<chat_id>/
        │
        ├── AGENTS.md          ← persona / custom instructions (editable by user)
        ├── MEMORY.md          ← persistent notes (written by remember_update skill)
        ├── .memory_checksum   ← SHA256 checksum for corruption detection
        ├── RECOVERY.md        ← crash recovery event log
        ├── backups/           ← automatic memory file backups
        └── .plans/            ← planner task files
```

```
 ┌─────────────────────────────────────────────────────┐
 │                Memory Manager                        │
 │                                                       │
 │  read_memory(chat_id)                                │
 │    └──▶ MEMORY.md ──▶ mtime cache hit? ──▶ return   │
 │                                                       │
 │  write_memory(chat_id, content)                      │
 │    └──▶ write file ──▶ invalidate cache              │
 │                                                       │
 │  detect_memory_corruption(chat_id)                   │
 │    └──▶ SHA256 stored checksum vs calculated         │
 │                                                       │
 │  repair_memory_file(chat_id)                         │
 │    └──▶ backup corrupted file ──▶ clear ──▶ log     │
 │                                                       │
 │  ensure_workspace(chat_id)                           │
 │    └──▶ mkdir ──▶ seed AGENTS.md if not exists      │
 └─────────────────────────────────────────────────────┘
```

---

## Vector Semantic Memory (sqlite-vec)

Long-term semantic memory using **sqlite-vec** for vector similarity search and **OpenAI embeddings** for text encoding:

```
 ┌────────────────────────────────────────────────────────────────┐
 │                    VectorMemory (sqlite-vec)                    │
 │                                                                  │
 │  ┌─────────────────────┐     ┌──────────────────────────────┐  │
 │  │  memory_entries      │     │  memory_vec (virtual table)  │  │
 │  │  ───────────────────│     │  ───────────────────────────  │  │
 │  │  id    (PK, auto)   │────▶│  rowid                       │  │
 │  │  chat_id (TEXT)     │     │  embedding (float[1536])     │  │
 │  │  text    (TEXT)     │     │  distance_metric = cosine    │  │
 │  │  category (TEXT)    │     └──────────────────────────────┘  │
 │  │  created_at (REAL)  │                                        │
 │  └─────────────────────┘                                        │
 │                                                                  │
 │  Skills:                                                         │
 │  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐ │
 │  │  memory_save     │  │  memory_search   │  │  memory_list  │ │
 │  │  text + category │  │  query → embed   │  │  recent N     │ │
 │  │  → embed → store │  │  → KNN search   │  │  (no embed)   │ │
 │  └──────────────────┘  └──────────────────┘  └───────────────┘ │
 └────────────────────────────────────────────────────────────────┘
```

**Flow for `memory_search`:**

```
 User query text
        │
        ▼
 OpenAI Embeddings API (text-embedding-3-small)
        │
        ▼
 float[1536] vector
        │
        ▼
 sqlite-vec KNN query (cosine distance, filtered by chat_id)
        │
        ▼
 Top-N matching memories with similarity scores
```

---

## Task Scheduler

Background async scheduler that triggers LLM actions on a schedule:

```
 ┌──────────────────────────────────────────────────────────────┐
 │                     TaskScheduler                             │
 │                                                                │
 │  Background asyncio task (ticks every 30s)                    │
 │                                                                │
 │  ┌─────────────────────────────────────────────────────────┐ │
 │  │  Schedule Types:                                        │ │
 │  │                                                          │ │
 │  │  daily:    {hour, minute}       ── runs once/day         │ │
 │  │  interval: {seconds}            ── runs every N seconds  │ │
 │  │  cron:     {hour, minute, weekdays} ── runs on spec days │ │
 │  └─────────────────────────────────────────────────────────┘ │
 │                                                                │
 │  Task execution flow:                                         │
 │                                                                │
 │  Tick ──▶ for each chat_id, for each task:                    │
 │    │                                                           │
 │    ├─▶ _is_due()?                                              │
 │    │     ├─ enabled?                                           │
 │    │     ├─ schedule type check (time/interval/weekday)       │
 │    │     └─ same_day guard (prevent double-run)               │
 │    │                                                           │
 │    ├─▶ _execute_task()                                         │
 │    │     ├─ Build prompt (inject compare + last_result)       │
 │    │     ├─ on_trigger → Bot.process_scheduled() → LLM       │
 │    │     ├─ Store result + update last_run                    │
 │    │     └─ on_send → WhatsApp.deliver (with 2 retries)      │
 │    │                                                           │
 │    └─▶ _persist() → workspace/<chat_id>/.scheduler/tasks.json│
 └──────────────────────────────────────────────────────────────┘
```

**Compare mode**: When `compare: true`, the scheduler injects the previous run's result and asks the LLM to highlight changes — perfect for monitoring tasks.

**Persistence**: Tasks are stored per-chat in `workspace/<chat_id>/.scheduler/tasks.json`.

**LLM skill** (`task_scheduler`): Users create/manage tasks via natural language in WhatsApp chat.

---

## Project & Knowledge Management

Organize information into **projects** with linked **knowledge entries** and graph-based recall:

```
 ┌────────────────────────────────────────────────────────────────┐
 │                  Project & Knowledge System                     │
 │                                                                  │
 │  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐ │
 │  │   Project     │─────▶│  Knowledge   │◀─────│  Links       │ │
 │  │   Store       │      │  Entries     │      │  (Graph)     │ │
 │  │              │      │              │      │              │ │
 │  │  id          │      │  id          │      │  project_id  │ │
 │  │  name        │      │  project_id  │      │  entry_a     │ │
 │  │  description │      │  text        │      │  entry_b     │ │
 │  │  tags[]      │      │  category    │      │  relation    │ │
 │  │  status      │      │  tags[]      │      │              │ │
 │  │  chat_ids[]  │      │  created_at  │      └──────────────┘ │
 │  └──────────────┘      └──────────────┘                        │
 │                                                                  │
 │  Skills (10 LLM tools):                                        │
 │  ┌───────────────────────────────────────────────────────────┐ │
 │  │  Projects:  project_create, project_list, project_info,  │ │
 │  │             project_update, project_archive               │ │
 │  │                                                           │ │
 │  │  Knowledge: knowledge_add, knowledge_search,              │ │
 │  │             knowledge_link, knowledge_list, project_recall│ │
 │  └───────────────────────────────────────────────────────────┘ │
 │                                                                  │
 │  Recall flow (injected into every LLM call):                   │
 │                                                                  │
 │  project_recall(chat_id) ──▶ get_chat_projects ──▶             │
 │    for each project: graph.traverse → build context string     │
 └────────────────────────────────────────────────────────────────┘
```

---

## Planner / Task Tracking

Create plans with tasks, manage dependencies, and track execution order:

```
 ┌─────────────────────────────────────────────────────┐
 │                    Planner Skill                     │
 │                                                       │
 │  Storage: workspace/<chat_id>/.plans/<name>.json      │
 │                                                       │
 │  Actions:                                            │
 │  ┌────────────────────────────────────────────────┐ │
 │  │  init    → Create a new plan                    │ │
 │  │  add     → Add task with optional dependencies │ │
 │  │  list    → Show all tasks + status              │ │
 │  │  next    → Show next unblocked task             │ │
 │  │  complete→ Mark task done with summary          │ │
 │  │  status  → Overall plan progress                │ │
 │  │  plan    → Show execution order (topo sort)     │ │
 │  └────────────────────────────────────────────────┘ │
 │                                                       │
 │  Dependency resolution:                              │
 │                                                       │
 │  Task A ──▶ Task B ──▶ Task C                        │
 │  (done)      (ready)     (blocked by B)              │
 │                                                       │
 │  "next" returns the first task whose deps are all    │
 │  completed — enabling step-by-step execution.        │
 └─────────────────────────────────────────────────────┘
```

---

## Web Research Skill

Combined web search and page crawling in a single skill:

```
 ┌──────────────────────────────────────────────────────┐
 │                 WebResearchSkill                      │
 │                                                        │
 │  Actions:                                             │
 │                                                        │
 │  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐ │
 │  │   search     │  │    crawl     │  │search_and_  │ │
 │  │              │  │              │  │   crawl     │ │
 │  │  query ──▶   │  │  URLs ──▶   │  │             │ │
 │  │  DuckDuckGo  │  │  HTTP GET ──▶│  │  search ──▶ │ │
 │  │  results     │  │  extract     │  │  top URLs ──││
 │  │              │  │  content     │  │  crawl each │ │
 │  └──────────────┘  └──────────────┘  └─────────────┘ │
 │                                                        │
 │  Options:                                             │
 │    max_results: limit search results (default 5)      │
 │    selector: CSS selector for targeted extraction     │
 └──────────────────────────────────────────────────────┘
```

---

## Crash Recovery (Message Queue)

Persistent queue that survives crashes — messages in-flight are recovered on restart:

```
 ┌─────────────────────────────────────────────────────────────┐
 │                   Message Queue                              │
 │                                                               │
 │  Storage: .workspace/.data/message_queue.jsonl               │
 │                                                               │
 │  States:                                                     │
 │                                                               │
 │  ┌──────────┐     ┌───────────┐     ┌──────────┐           │
 │  │ PENDING  │────▶│ COMPLETED │     │  STALE   │           │
 │  │          │     │           │     │(timeout) │           │
 │  │ enqueued │     │ processed │     │ crashed  │           │
 │  │ before   │     │ ok        │     │ during   │           │
 │  │ process  │     │           │     │ process  │           │
 │  └──────────┘     └───────────┘     └──────────┘           │
 │       │                                     │               │
 │       │                              ┌──────┴───────┐       │
 │       │                              │  Recovery    │       │
 │       │                              │  (on startup)│       │
 │       │                              │              │       │
 │       │                              │  find stale  │       │
 │       │                              │  messages    │       │
 │       │                              │  ──▶ requeue │       │
 │       │                              │  ──▶ process │       │
 │       │                              └──────────────┘       │
 └─────────────────────────────────────────────────────────────┘
```

**Startup recovery**: `Bot.recover_pending_messages()` finds stale entries (pending > timeout) and reprocesses them.

---

## Graceful Shutdown

Ordered cleanup across all components when the bot receives SIGINT/SIGTERM:

```
 ┌─────────────────────────────────────────────────────────┐
 │              GracefulShutdown                            │
 │                                                           │
 │  Signal received (Ctrl+C / SIGTERM)                      │
 │        │                                                  │
 │        ▼                                                  │
 │  1. Stop accepting new messages                           │
 │     └── shutdown.request_shutdown()                       │
 │        │                                                  │
 │        ▼                                                  │
 │  2. Cancel message poller task                            │
 │        │                                                  │
 │        ▼                                                  │
 │  3. Wait for in-flight operations (with timeout)          │
 │     └── semaphore-based tracking                          │
 │        │                                                  │
 │        ▼                                                  │
 │  4. Stop task scheduler                                   │
 │        │                                                  │
 │        ▼                                                  │
 │  5. Stop health check server                              │
 │        │                                                  │
 │        ▼                                                  │
 │  6. Close WhatsApp channel                                │
 │        │                                                  │
 │        ▼                                                  │
 │  7. Close database connections                            │
 │        │                                                  │
 │        ▼                                                  │
 │  Done ✓                                                   │
 └─────────────────────────────────────────────────────────┘
```

---

## Health Check Server

Lightweight HTTP server for monitoring systems:

```
 GET /health
        │
        ▼
 ┌──────────────────────────────────────────────────────┐
 │               Health Report                           │
 │                                                        │
 │  {                                                     │
 │    "status": "healthy" | "degraded" | "unhealthy",    │
 │    "version": "1.0.0",                                │
 │    "components": {                                     │
 │      "database":    { status, latency_ms },           │
 │      "whatsapp":    { status, latency_ms },           │
 │      "llm":         { status, latency_ms },           │
 │      "memory":      { status, message },              │
 │      "performance": { status, message }               │
 │    },                                                  │
 │    "token_usage": {                                    │
 │      "prompt_tokens": N,                              │
 │      "completion_tokens": N,                          │
 │      "total_tokens": N,                               │
 │      "request_count": N                               │
 │    }                                                   │
 │  }                                                     │
 │                                                        │
 │  HTTP 200 if healthy/degraded, 503 if unhealthy       │
 └──────────────────────────────────────────────────────┘
```

---

## Rate Limiting

Sliding window rate limiter with separate limits per chat and per expensive skill:

```
 ┌─────────────────────────────────────────────────────────┐
 │                   Rate Limiter                           │
 │                                                           │
 │  Per-chat rate limit:    30 messages / 60s window        │
 │  Per-skill rate limit:   10 calls / 60s (expensive)     │
 │                                                           │
 │  Expensive skills:                                       │
 │    web_search, web_research, shell, memory_save          │
 │                                                           │
 │  ┌────────────────────────────────────────────────────┐ │
 │  │  Sliding Window Algorithm                          │ │
 │  │                                                    │ │
 │  │  timestamps: [t1, t2, t3, ..., tN]                 │
 │  │  window_start = now - 60s                          │
 │  │  count = timestamps after window_start             │
 │  │                                                    │ │
 │  │  count < limit? ──▶ ALLOW                         │ │
 │  │  count >= limit? ──▶ REJECT + warn                │ │
 │  │                                                    │ │
 │  │  Memory protection:                               │ │
 │  │    • Max 1000 tracked chats (LRU eviction)        │ │
 │  │    • Max 100 timestamps per chat                  │ │
 │  └────────────────────────────────────────────────────┘ │
 └─────────────────────────────────────────────────────────┘
```

---

## Monitoring & Metrics

Real-time performance tracking across all bot operations:

```
 ┌──────────────────────────────────────────────────────────┐
 │                   PerformanceMetrics                      │
 │                                                            │
 │  ┌────────────────┐  ┌────────────────┐  ┌────────────┐ │
 │  │  LLM Latency   │  │  Message       │  │  Queue     │ │
 │  │                │  │  Latency       │  │  Depth     │ │
 │  │  track every   │  │  track every   │  │  pending   │ │
 │  │  LLM API call  │  │  handle_msg    │  │  messages  │ │
 │  │                │  │  end-to-end    │  │  count     │ │
 │  └────────────────┘  └────────────────┘  └────────────┘ │
 │                                                            │
 │  ┌────────────────┐  ┌────────────────┐                  │
 │  │  Memory Monitor│  │  Token Usage   │                  │
 │  │                │  │                │                  │
 │  │  warning: 75%  │  │  per-session   │                  │
 │  │  critical: 90% │  │  prompt +      │                  │
 │  │  periodic check│  │  completion    │                  │
 │  │  cache tracking│  │  token counts  │                  │
 │  └────────────────┘  └────────────────┘                  │
 └──────────────────────────────────────────────────────────┘
```
