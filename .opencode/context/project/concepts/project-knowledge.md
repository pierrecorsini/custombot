<!-- Context: project/concepts/project-knowledge | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Project & Knowledge Management

**Core Idea**: Organize information into **projects** with linked **knowledge entries** and a graph-based recall system. Projects group related knowledge, entries store text with tags and categories, and links create semantic relationships between entries — all recalled automatically into every LLM call for that chat.

**Source**: `FEATURES.md` — Project & Knowledge Management section (archived 2026-04-16)

---

## Key Points

- **Projects**: Named containers with description, tags, status, and associated `chat_ids`
- **Knowledge entries**: Text blobs with `project_id`, category, tags, and timestamp
- **Graph links**: Bidirectional relationships between entries with a `relation` label
- **Auto-recall**: `project_recall(chat_id)` injected into every LLM call — traverses knowledge graph for active projects
- **10 LLM tools**: Full CRUD for projects, knowledge, and links via natural language

---

## Data Model

```
Project          Knowledge Entry       Links (Graph)
─────────        ───────────────       ─────────────
id               id                    project_id
name             project_id            entry_a
description      text                  entry_b
tags[]           category              relation
status           tags[]
chat_ids[]       created_at
```

---

## Skills (10 LLM Tools)

| Category | Skills |
|----------|--------|
| **Projects** | `project_create`, `project_list`, `project_info`, `project_update`, `project_archive` |
| **Knowledge** | `knowledge_add`, `knowledge_search`, `knowledge_link`, `knowledge_list` |
| **Recall** | `project_recall` (auto-injected into LLM context) |

---

## Recall Flow

```
project_recall(chat_id)
     │
     ▼
get_chat_projects()
     │
     ▼
for each project: graph.traverse → build context string
     │
     ▼
Injected into LLM system context
```

---

## Quick Example

```
User: "Create a project called 'home-renovation'"
  → project_create(name="home-renovation")

User: "Add: kitchen tiles delivered Tuesday"
  → knowledge_add(project_id, text="...", category="updates")

User: "Link the tile info with the budget entry"
  → knowledge_link(entry_a, entry_b, relation="related_to")
```

---

## Codebase

- `src/project/store.py` — Project store (SQLite backend in `.data/projects.db`)
- `skills/builtin/project_skills.py` — All 10 project/knowledge LLM tools
- `src/core/context_builder.py` — Injects `project_recall` into LLM context

## Related

- `concepts/react-loop.md` — Where recall is injected
- `concepts/per-chat-memory.md` — Complementary file-based memory
- `lookup/workspace-structure.md` — Where projects.db lives
