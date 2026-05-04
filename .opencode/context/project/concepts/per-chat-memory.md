<!-- Context: project/concepts/per-chat-memory | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Concept: Per-Chat Memory System

**Core Idea**: Each WhatsApp chat gets an isolated file-based memory workspace with two editable files (`MEMORY.md` for persistent notes, `AGENTS.md` for persona/instructions), backed by mtime caching for performance and SHA256 checksums for corruption detection.

**Source**: `README.md` — Per-Chat Memory section

---

## Key Points

- **Two memory files**: `MEMORY.md` (bot-written persistent notes) and `AGENTS.md` (user-editable persona)
- **mtime caching**: Files are cached by modification time — avoids disk reads on unchanged files
- **Corruption detection**: SHA256 checksum stored in `.memory_checksum` — detects bit rot or manual edits gone wrong
- **Auto-repair**: Corrupted files are backed up, cleared, and logged automatically
- **Workspace isolation**: Each chat's files live in `workspace/whatsapp_data/<chat_id>/`

---

## File Layout

```
workspace/whatsapp_data/<chat_id>/
    ├── AGENTS.md          ← persona / custom instructions (user-editable)
    ├── MEMORY.md          ← persistent notes (written by remember_update skill)
    ├── .chat_id           ← original chat_id for JID reverse lookup
    ├── .memory_checksum   ← SHA256 checksum for corruption detection
    ├── RECOVERY.md        ← crash recovery event log
    ├── backups/           ← automatic memory file backups
    └── .plans/            ← planner task files
```

---

## Memory Manager Operations

```
read_memory(chat_id)
    └── MEMORY.md → mtime cache hit? → return cached
                          │ miss → read file → update cache → return

write_memory(chat_id, content)
    └── write file → invalidate cache → update checksum

detect_memory_corruption(chat_id)
    └── SHA256 stored checksum vs calculated → mismatch? → corruption!

repair_memory_file(chat_id)
    └── backup corrupted file → clear content → log event

ensure_workspace(chat_id)
    └── mkdir → seed AGENTS.md if not exists → write .chat_id metadata
```

---

## Context Assembly

Both files are injected into the LLM context on every call:
```
Context = chat_history + MEMORY.md + AGENTS.md + project_recall
```

The `remember_update` skill writes to MEMORY.md, `remember_read` reads it.

---

## Codebase

- `src/memory.py` — MemoryManager (read/write/cache/corruption detection)
- `skills/builtin/` — `remember_update`, `remember_read` skills
- `src/core/context_builder.py` — Assembles memory into LLM context

## Related

- `concepts/react-loop.md` — How memory feeds into the ReAct loop
- `concepts/vector-memory.md` — Semantic search across memories
- `lookup/workspace-structure.md` — Full workspace directory layout
