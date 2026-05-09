"""
src/project/recall.py — Hybrid recall combining vector search with graph traversal.

Two retrieval paths:
  1. Semantic: VectorMemory cosine search finds candidate entries
  2. Structural: ProjectGraph BFS enriches candidates with related knowledge

The recall() method assembles a full project context suitable for injecting
into the LLM system prompt.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from src.project.dates import fmt_ts

if TYPE_CHECKING:
    from src.project.graph import ProjectGraph
    from src.vector_memory import VectorMemory
    from src.project.store import ProjectStore

log = logging.getLogger(__name__)

VECTOR_CHAT_ID = "__projects__"


class ProjectRecall:
    """Hybrid vector + graph retrieval for project knowledge."""

    def __init__(
        self,
        store: ProjectStore,
        graph: ProjectGraph,
        vector_memory: Optional[VectorMemory] = None,
    ) -> None:
        self._store = store
        self._graph = graph
        self._vm = vector_memory

    async def save_knowledge(
        self,
        project_id: str,
        text: str,
        title: str = "",
        category: str = "note",
        source: str = "chat",
        source_chat_id: str = "",
        link_to: Optional[int] = None,
        link_relation: str = "relates_to",
    ) -> Dict[str, Any]:
        entry = self._store.add_knowledge(
            project_id=project_id,
            text=text,
            title=title,
            category=category,
            source=source,
            source_chat_id=source_chat_id,
        )

        if self._vm:
            vm_category = f"project:{project_id}"
            try:
                await self._vm.save(
                    chat_id=VECTOR_CHAT_ID,
                    text=f"{title}: {text}" if title else text,
                    category=vm_category,
                )
            except Exception as exc:
                log.warning("Failed to embed knowledge entry %d: %s", entry["id"], exc)

        if link_to and link_to != entry["id"]:
            self._store.link_knowledge(
                from_id=entry["id"],
                to_id=link_to,
                relation=link_relation,
            )

        return entry

    async def search(
        self,
        project_id: str,
        query: str,
        category: Optional[str] = None,
        limit: int = 5,
        graph_depth: int = 1,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 50))
        graph_depth = max(0, min(graph_depth, 5))
        results: List[Dict[str, Any]] = []

        if self._vm:
            try:
                vm_results = await self._vm.search(
                    chat_id=VECTOR_CHAT_ID,
                    query=query,
                    limit=limit * 3,
                )
                candidate_ids: set[int] = set()
                for vr in vm_results:
                    vm_cat = vr.get("category", "")
                    if not vm_cat.startswith(f"project:{project_id}"):
                        continue
                    results.append(
                        {
                            "text": vr["text"],
                            "relevance": 1 - vr.get("distance", 1.0),
                            "source": "vector",
                        }
                    )
                    candidate_ids.add(vr["id"])
            except Exception as exc:
                log.warning("Vector search failed for project %s: %s", project_id, exc)

        recent = self._store.list_knowledge(project_id, category=category, limit=limit)
        seen_texts = {r["text"] for r in results}
        for entry in recent:
            if entry["text"] not in seen_texts:
                results.append(
                    {
                        "id": entry["id"],
                        "title": entry.get("title", ""),
                        "text": entry["text"],
                        "category": entry["category"],
                        "created_at": entry["created_at"],
                        "source": "graph",
                    }
                )
                seen_texts.add(entry["text"])

        enriched: List[Dict[str, Any]] = []
        for r in results:
            if "id" in r and graph_depth > 0:
                related = self._graph.get_related(r["id"], depth=graph_depth)
                r["related"] = related[:5]
            enriched.append(r)

        return enriched[:limit]

    def recall(self, project_id: str, include_graph: bool = True) -> str:
        project = self._store.get_project(project_id)
        if not project:
            return ""

        entries = self._store.list_knowledge(project_id, limit=30)
        if not entries:
            return f"Project: {project['name']} (status: {project['status']})\nNo knowledge entries yet."

        lines = [
            f"## Project: {project['name']}",
            f"Status: {project['status']}",
        ]
        if project["description"]:
            lines.append(f"Description: {project['description']}")
        if project["tags"]:
            lines.append(f"Tags: {', '.join(project['tags'])}")

        lines.append(f"\n### Knowledge ({len(entries)} entries):\n")

        for entry in entries:
            lines.append(
                f"- [{entry['category']}] "
                f"{entry.get('title') or entry['text'][:80]} "
                f"({fmt_ts(entry['created_at'])})"
            )
            if entry["text"] and entry.get("title"):
                lines.append(f"  {entry['text'][:200]}")

        if include_graph:
            ctx = self._graph.get_context_graph(project_id)
            if ctx["edges"]:
                lines.append("\n### Relationships:\n")
                for edge in ctx["edges"][:15]:
                    lines.append(
                        f"- {edge['from_title'][:50]} → {edge['relation']} → {edge['to_title'][:50]}"
                    )

        return "\n".join(lines)
