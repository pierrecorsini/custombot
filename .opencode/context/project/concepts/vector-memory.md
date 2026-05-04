<!-- Context: project/concepts/vector-memory | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Concept: Vector Semantic Memory

**Core Idea**: Long-term semantic memory using sqlite-vec (vector similarity search) and OpenAI text-embedding-3-small (1536-dim embeddings). Memories are stored per-chat with category tags, enabling cosine-distance KNN search across past knowledge.

**Source**: `README.md` — Vector Semantic Memory section

---

## Key Points

- **Storage**: sqlite-vec virtual table storing float[1536] vectors alongside text entries
- **Embeddings**: OpenAI `text-embedding-3-small` model (1536 dimensions)
- **Search**: KNN (K-nearest neighbors) with cosine distance, filtered by `chat_id`
- **Three skills**: `memory_save` (embed + store), `memory_search` (query + embed + KNN), `memory_list` (recent N, no embedding)
- **Per-chat isolation**: All queries filtered by `chat_id` — no cross-chat leakage

---

## Schema

```
memory_entries                    memory_vec (virtual table)
───────────────                   ──────────────────────────
id       (PK, auto)    ──────▶   rowid
chat_id  (TEXT)                    embedding (float[1536])
text     (TEXT)                    distance_metric = cosine
category (TEXT)
created_at (REAL)
```

---

## Search Flow

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

## Skills Interface

| Skill | Action | Needs Embedding? |
|-------|--------|-----------------|
| `memory_save` | text + category → embed → store | Yes |
| `memory_search` | query → embed → KNN search | Yes |
| `memory_list` | recent N entries (no search) | No |

---

## Codebase

- `src/vector_memory/` — VectorMemory store (sqlite-vec operations, split into batch.py, health.py, _utils.py)
- `skills/builtin/memory_vss.py` — memory_save, memory_search, memory_list skills
- `workspace/.data/vector_memory.db` — Runtime database file

## Related

- `concepts/per-chat-memory.md` — File-based MEMORY.md system (complementary)
- `concepts/react-loop.md` — How memory is injected into LLM context
- `lookup/workspace-structure.md` — Where vector_memory.db lives
