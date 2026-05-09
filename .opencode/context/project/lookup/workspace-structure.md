<!-- Context: project/lookup/workspace-structure | Priority: high | Version: 1.1 | Updated: 2026-05-02 -->

# Lookup: Workspace Structure

**Purpose**: Complete layout of the `.workspace/` directory — all runtime files, per-chat data, and dynamic content.

**Source**: `README.md` — Workspace Isolation section

---

## Directory Tree

```
workspace/                           ← Runtime workspace (configurable via config.json)
├── config.json                      ← User configuration (API keys, settings)
├── routing.json                     ← Routing rules (priority-based matching)
├── whatsapp_session.db              ← WhatsApp session (neonize/whatsmeow)
├── whatsapp_data/
│   └── <chat_id>/
│       ├── AGENTS.md                ← Persona / custom instructions (user-editable)
│       ├── MEMORY.md                ← Persistent notes (bot-written)
│       ├── .memory_checksum         ← SHA256 corruption detection checksum
│       ├── RECOVERY.md              ← Crash recovery event log
│       ├── backups/                 ← Automatic memory file backups
│       ├── .plans/                  ← Planner task files
│       │   └── my-plan.json
│       ├── .scheduler/              ← Scheduled tasks per chat
│       │   └── tasks.json
│       └── any_file.txt             ← Files created by skills
│
├── .data/
│   ├── chats.json                   ← Chat metadata (all known chats)
│   ├── messages/
│   │   ├── chat-123.jsonl           ← Message history per chat (JSONL)
│   │   └── chat-456.jsonl
│   ├── message_queue.jsonl          ← Crash recovery queue (PENDING/COMPLETED/STALE)
│   ├── message_index.json           ← Message search index
│   ├── instructions/                ← Cached instruction templates
│   └── projects.db                  ← Project & knowledge SQLite database
│
├── logs/
│   ├── custombot.log                ← Rotating log (10MB max, 5 backups)
│   └── llm/                         ← LLM request/response JSON logs (--log-llm)
│
└── skills/                          ← User-defined markdown skills
```

---

## Key Paths

| Path | Purpose |
|------|---------|
| `workspace/routing.json` | Routing rules — editable via routing skills |
| `workspace/.data/message_queue.jsonl` | Crash recovery — PENDING → COMPLETED → STALE |
| `workspace/.data/projects.db` | Projects + knowledge graph + links |
| `workspace/whatsapp_data/<id>/MEMORY.md` | Per-chat persistent notes |
| `workspace/whatsapp_data/<id>/AGENTS.md` | Per-chat persona instructions |
| `workspace/logs/llm/` | LLM request/response JSON logs (enabled via `--log-llm`) |

---

## Security Boundaries

- **`shell` skill**: CWD = `workspace/whatsapp_data/<chat_id>/` — cannot escape
- **`read_file`/`write_file`**: Block `..` path traversal
- **Per-chat isolation**: Each `<chat_id>/` directory is sandboxed

---

## Codebase

- `src/memory.py` — Manages per-chat workspace creation
- `src/db/db.py` — Database operations in `.data/`
- `src/message_queue.py` — Queue in `.data/message_queue.jsonl`
- `src/vector_memory/` — VectorMemory store (sqlite-vec operations, batch coalescing, health monitoring)
- `src/project/store.py` — Projects DB in `.data/projects.db`
- `src/config/config_watcher.py` — Watches `workspace/config.json` for changes

## Related

- `concepts/per-chat-memory.md` — MEMORY.md and AGENTS.md details
- `concepts/vector-memory.md` — vector_memory.db schema
- `lookup/configuration.md` — config.json schema
