"""
src/project — Project & Knowledge tracking module.

Hybrid SQLite-based storage combining structured graph relationships
with semantic vector search (via existing VectorMemory).

Modules:
  store.py  — SQLite schema + CRUD for projects, knowledge entries, links, chat bindings
  graph.py  — Graph traversal queries (BFS, dependency chains, context graphs)
  recall.py — Hybrid recall: vector search → graph enrichment
  dates.py  — Shared date formatting utilities
"""

from src.project.dates import fmt_ts
from src.project.graph import ProjectGraph
from src.project.recall import ProjectRecall
from src.project.store import ProjectStore

__all__ = ["ProjectStore", "ProjectGraph", "ProjectRecall", "fmt_ts"]
