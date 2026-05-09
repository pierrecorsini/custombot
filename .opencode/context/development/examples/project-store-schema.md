<!-- Context: development/examples/project-store-schema | Priority: low | Version: 1.0 | Updated: 2026-03-31 -->

# Example: Project Store SQL Schema

**Purpose**: Complete SQLite schema for project/knowledge tracking

**Source**: `src/project/store.py:_ensure_schema()`

---

## Schema

```sql
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,        -- slug: "my-app"
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',   -- active|paused|completed|archived
    tags        TEXT DEFAULT '[]',       -- JSON array
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE knowledge_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT DEFAULT '',
    text            TEXT NOT NULL,
    category        TEXT DEFAULT 'note', -- decision|fact|requirement|note|link|contact|task
    source          TEXT DEFAULT 'chat', -- chat|web|manual|file
    source_chat_id  TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE knowledge_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     INTEGER NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    to_id       INTEGER NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,           -- relates_to|depends_on|contradicts|supersedes|part_of|references
    weight      REAL DEFAULT 1.0,
    UNIQUE(from_id, to_id, relation)
);

CREATE TABLE project_chats (
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chat_id     TEXT NOT NULL,
    role        TEXT DEFAULT 'contributor',  -- owner|contributor|viewer
    PRIMARY KEY (project_id, chat_id)
);
```

## Indexes

```sql
CREATE INDEX idx_knowledge_project ON knowledge_entries(project_id);
CREATE INDEX idx_knowledge_category ON knowledge_entries(project_id, category);
CREATE INDEX idx_knowledge_source_chat ON knowledge_entries(source_chat_id);
CREATE INDEX idx_links_from ON knowledge_links(from_id);
CREATE INDEX idx_links_to ON knowledge_links(to_id);
```

---

## Database Location

`.workspace/.data/projects.db` (alongside `vector_memory.db`)

---

## Related

- `concepts/project-store.md` — Architecture overview
- `concepts/knowledge-graph.md` — Graph traversal
