"""
src/project/graph.py — Graph traversal queries on knowledge_links.

Provides BFS-based traversal to discover related knowledge entries,
dependency chains, and full project context graphs.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Set

from src.project.store import ProjectStore

log = logging.getLogger(__name__)


class ProjectGraph:
    """Read-only graph traversal over knowledge_links."""

    def __init__(self, store: ProjectStore) -> None:
        self._store = store

    def get_related(
        self,
        entry_id: int,
        depth: int = 2,
        relation: str | None = None,
    ) -> List[Dict[str, Any]]:
        visited: Set[int] = {entry_id}
        queue: deque[tuple[int, int]] = deque([(entry_id, 0)])
        results: List[Dict[str, Any]] = []

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            outgoing = self._store.get_outgoing_links(current_id)
            incoming = self._store.get_incoming_links(current_id)

            all_links = []
            for link in outgoing:
                if relation is None or link["relation"] == relation:
                    all_links.append((link["to_id"], link["relation"], link["weight"]))
            for link in incoming:
                if relation is None or link["relation"] == relation:
                    all_links.append(
                        (link["from_id"], link["relation"], link["weight"])
                    )

            for neighbor_id, rel, weight in all_links:
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)
                entry = self._store.get_knowledge(neighbor_id)
                if entry:
                    entry["_relation"] = rel
                    entry["_weight"] = weight
                    entry["_depth"] = current_depth + 1
                    results.append(entry)
                    queue.append((neighbor_id, current_depth + 1))

        return results

    def get_dependencies(self, entry_id: int) -> List[Dict[str, Any]]:
        visited: Set[int] = set()
        results: List[Dict[str, Any]] = []
        self._trace_deps(entry_id, visited, results, depth=0)
        return results

    def _trace_deps(
        self,
        entry_id: int,
        visited: Set[int],
        results: List[Dict[str, Any]],
        depth: int,
    ) -> None:
        if entry_id in visited or depth > 10:
            return
        visited.add(entry_id)

        outgoing = self._store.get_outgoing_links(entry_id)
        for link in outgoing:
            if link["relation"] == "depends_on" and link["to_id"] not in visited:
                entry = self._store.get_knowledge(link["to_id"])
                if entry:
                    entry["_depth"] = depth + 1
                    results.append(entry)
                    self._trace_deps(link["to_id"], visited, results, depth + 1)

    def get_context_graph(self, project_id: str) -> Dict[str, Any]:
        """Build full context graph using batch queries (2 queries, not 3N+2)."""
        entries = self._store.list_knowledge(project_id, limit=100)
        if not entries:
            return {
                "project_id": project_id,
                "node_count": 0,
                "edge_count": 0,
                "nodes": [],
                "edges": [],
            }

        entry_ids = [e["id"] for e in entries]
        entry_map = {e["id"]: e for e in entries}

        # Batch fetch all edges involving these entries (1 query)
        all_edges = self._store.get_edges_batch(entry_ids)

        # Collect all target IDs referenced by edges (may include entries
        # outside the project's top-100 list)
        target_ids = set()
        for edge in all_edges:
            target_ids.add(edge["from_id"])
            target_ids.add(edge["to_id"])
        missing_ids = target_ids - entry_map.keys()
        if missing_ids:
            # Batch fetch any entries not already in our map (1 query)
            extra = self._store.get_knowledge_batch(list(missing_ids))
            entry_map.update(extra)

        # Build edge descriptions
        edge_results: List[Dict[str, Any]] = []
        for edge in all_edges:
            from_entry = entry_map.get(edge["from_id"])
            to_entry = entry_map.get(edge["to_id"])
            if from_entry and to_entry:
                edge_results.append(
                    {
                        "from_id": edge["from_id"],
                        "from_title": from_entry.get("title")
                        or from_entry["text"][:60],
                        "to_id": edge["to_id"],
                        "to_title": to_entry.get("title") or to_entry["text"][:60],
                        "relation": edge["relation"],
                        "weight": edge["weight"],
                    }
                )

        # Compute node degrees from edges
        degree_map: Dict[int, int] = {eid: 0 for eid in entry_ids}
        for edge in all_edges:
            degree_map[edge["from_id"]] = degree_map.get(edge["from_id"], 0) + 1
            degree_map[edge["to_id"]] = degree_map.get(edge["to_id"], 0) + 1

        node_results = [
            {
                "id": eid,
                "title": entry_map[eid].get("title") or entry_map[eid]["text"][:60],
                "category": entry_map[eid]["category"],
                "degree": degree_map.get(eid, 0),
            }
            for eid in entry_ids
            if eid in entry_map
        ]
        node_results.sort(key=lambda n: n["degree"], reverse=True)

        return {
            "project_id": project_id,
            "node_count": len(node_results),
            "edge_count": len(edge_results),
            "nodes": node_results,
            "edges": edge_results,
        }
