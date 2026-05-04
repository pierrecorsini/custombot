<!-- Context: development/concepts/project-store | Priority: medium | Version: 1.0 | Updated: 2026-03-31 -->

# Concept: Project & Knowledge Store

**Purpose**: Hybrid SQLite-backed project tracking with graph relationships and vector semantic search

**Source**: Implemented in `src/project/`

---

## Core Concept

Projects are top-level containers for organizing knowledge entries (decisions, facts, requirements, notes). Knowledge entries can be linked to each other via typed, directed relationships forming a graph. The system combines structured graph traversal with vector similarity search for hybrid retrieval.

---

## Architecture

```
ProjectStore (SQLite)
├── projects          → Container (id, name, description, status, tags)
├── knowledge_entries → Per-project notes (title, text, category, source_chat_id)
├── knowledge_links   → Directed edges (from_id → to_id, relation, weight)
└── project_chats     → Cross-chat bindings (project_id, chat_id, role)

VectorMemory (sqlite-vec)      ← Existing, reused
└── Embeds knowledge entries with category="project:{id}"
```

---

## Key Design Decisions

- **SQLite only** — no new infrastructure, matches VectorMemory pattern
- **sqlite-vec reuse** — knowledge entries also embedded for semantic search
- **BFS graph traversal** — bounded depth, handles circular links via visited set
- **Hybrid recall** — vector search finds candidates → graph enriches with related entries
- **System prompt injection** — project context injected alongside MEMORY.md when chat is bound to a project

---

## Valid Enums

| Field | Values |
|-------|--------|
| `status` | active, paused, completed, archived |
| `category` | decision, fact, requirement, note, link, contact, task |
| `relation` | relates_to, depends_on, contradicts, supersedes, part_of, references |

---

## Codebase Reference

- `src/project/store.py` — Schema + CRUD (ProjectStore)
- `src/project/graph.py` — BFS traversal (ProjectGraph)
- `src/project/recall.py` — Hybrid recall (ProjectRecall)
- `src/skills/builtin/project_skills.py` — 10 LLM-callable skills

---

## Related

- `examples/project-store-schema.md` — Full SQL schema
- `examples/knowledge-skills-schema.md` — Skill definitions
- `concepts/skills-architecture.md` — Overall skills system
