"""
memory.semantic_graph — Knowledge graph from extracted entities and relationships.

Stores entities and relationships as a JSON adjacency list, supporting
BFS traversal for multi-hop queries.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENTITY_TYPES = frozenset({"person", "place", "project", "concept", "date", "preference"})
RELATIONSHIP_TYPES = frozenset({"knows", "works_on", "prefers", "mentions", "related_to"})


@dataclass(slots=True)
class Entity:
    """A node in the semantic graph."""

    id: str
    name: str
    entity_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Relationship:
    """A directed edge between two entities."""

    from_id: str
    to_id: str
    rel_type: str
    weight: float = 1.0


@dataclass(slots=True)
class GraphNode:
    """Entity with its connected relationships for query results."""

    entity: Entity
    depth: int = 0
    relation_from_start: str | None = None


class SemanticMemoryGraph:
    """Adjacency-list knowledge graph persisted as JSON."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "semantic_graph.json"
        self._entities: dict[str, Entity] = {}
        self._adjacency: dict[str, list[Relationship]] = {}
        self._load()

    def _load(self) -> None:
        """Load graph from disk if it exists."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load semantic graph: %s", exc)
            return

        for ent_data in raw.get("entities", []):
            entity = Entity(
                id=ent_data["id"],
                name=ent_data["name"],
                entity_type=ent_data["entity_type"],
                properties=ent_data.get("properties", {}),
            )
            self._entities[entity.id] = entity

        for rel_data in raw.get("relationships", []):
            rel = Relationship(
                from_id=rel_data["from_id"],
                to_id=rel_data["to_id"],
                rel_type=rel_data["rel_type"],
                weight=rel_data.get("weight", 1.0),
            )
            self._adjacency.setdefault(rel.from_id, []).append(rel)

    def _save(self) -> None:
        """Persist graph to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "entity_type": e.entity_type,
                    "properties": e.properties,
                }
                for e in self._entities.values()
            ],
            "relationships": [
                {
                    "from_id": r.from_id,
                    "to_id": r.to_id,
                    "rel_type": r.rel_type,
                    "weight": r.weight,
                }
                for rels in self._adjacency.values()
                for r in rels
            ],
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_entity(self, entity: Entity) -> None:
        """Add or update an entity in the graph."""
        if entity.entity_type not in ENTITY_TYPES:
            log.warning("Unknown entity type: %s", entity.entity_type)
            return
        self._entities[entity.id] = entity
        self._adjacency.setdefault(entity.id, [])
        self._save()

    def add_relationship(self, from_id: str, to_id: str, rel_type: str) -> None:
        """Add a directed relationship between two entities."""
        if rel_type not in RELATIONSHIP_TYPES:
            log.warning("Unknown relationship type: %s", rel_type)
            return
        if from_id not in self._entities or to_id not in self._entities:
            log.warning("Cannot link unknown entities: %s -> %s", from_id, to_id)
            return
        self._adjacency.setdefault(from_id, []).append(
            Relationship(from_id=from_id, to_id=to_id, rel_type=rel_type)
        )
        self._save()

    def query(self, start_entity: str, hops: int = 2) -> list[GraphNode]:
        """BFS traversal returning entities reachable within *hops* hops."""
        if start_entity not in self._entities:
            return []

        visited: set[str] = {start_entity}
        queue: deque[tuple[str, int, str | None]] = deque([(start_entity, 0, None)])
        results: list[GraphNode] = []

        while queue:
            current_id, depth, rel = queue.popleft()
            if depth >= hops:
                continue
            for relationship in self._adjacency.get(current_id, []):
                if relationship.to_id in visited:
                    continue
                visited.add(relationship.to_id)
                entity = self._entities.get(relationship.to_id)
                if entity is None:
                    continue
                node = GraphNode(
                    entity=entity,
                    depth=depth + 1,
                    relation_from_start=relationship.rel_type,
                )
                results.append(node)
                queue.append((relationship.to_id, depth + 1, relationship.rel_type))

        return results

    def extract_and_store(self, text: str, chat_id: str) -> list[Entity]:
        """Simple keyword-based entity extraction and storage."""
        entities: list[Entity] = []

        # Extract names: capitalized words (2+ chars)
        names = set(re.findall(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b", text))
        for name in names:
            entity_id = f"person_{name.lower().replace(' ', '_')}"
            if entity_id not in self._entities:
                entity = Entity(id=entity_id, name=name, entity_type="person")
                self.add_entity(entity)
                entities.append(entity)

        # Extract dates
        dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
        for date_str in dates:
            entity_id = f"date_{date_str}"
            if entity_id not in self._entities:
                entity = Entity(id=entity_id, name=date_str, entity_type="date")
                self.add_entity(entity)
                entities.append(entity)

        # Extract project references: quoted phrases or #tags
        tags = set(re.findall(r"#(\w+)", text))
        for tag in tags:
            entity_id = f"concept_{tag.lower()}"
            if entity_id not in self._entities:
                entity = Entity(id=entity_id, name=tag, entity_type="concept")
                self.add_entity(entity)
                entities.append(entity)

        return entities
