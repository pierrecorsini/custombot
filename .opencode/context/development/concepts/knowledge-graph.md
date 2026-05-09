<!-- Context: development/concepts/knowledge-graph | Priority: low | Version: 1.0 | Updated: 2026-03-31 -->

# Concept: Knowledge Graph Traversal

**Purpose**: BFS-based graph traversal on knowledge_links for relationship discovery

**Source**: `src/project/graph.py`

---

## Core Concept

Knowledge entries are connected via typed directed edges (`knowledge_links`). ProjectGraph provides BFS traversal bounded by depth, dependency chain tracing, and full project context graph extraction. Circular links are handled via a visited set.

---

## Key Points

- **BFS traversal** — `get_related(entry_id, depth=2)` follows both outgoing and incoming links
- **Dependency chains** — `get_dependencies(entry_id)` traces only `depends_on` edges
- **Context graph** — `get_context_graph(project_id)` returns all nodes + edges with degree centrality
- **Circular safety** — Visited set prevents infinite loops, max depth 10 on deps
- **Degree sorting** — Context graph nodes sorted by connection count (most connected first)

---

## Quick Example

```python
graph = ProjectGraph(store)
related = graph.get_related(entry_id=1, depth=2)
# Returns entries reachable within 2 hops, with _relation and _depth metadata

deps = graph.get_dependencies(entry_id=5)
# Traces depends_on chain: 5 → depends_on → 3 → depends_on → 1

ctx = graph.get_context_graph("my-project")
# Returns {"nodes": [...], "edges": [...], "node_count": N, "edge_count": M}
```

---

## Codebase Reference

- `src/project/graph.py` — ProjectGraph class
- `src/project/store.py` — get_outgoing_links, get_incoming_links

---

## Related

- `concepts/project-store.md` — Overall store architecture
