"""
src/project/store.py — SQLite-backed project & knowledge storage.

Tables:
  projects          — top-level project containers
  knowledge_entries — individual facts/decisions/notes linked to a project
  knowledge_links   — directed graph edges between knowledge entries
  project_chats     — cross-chat bindings (which chats contribute to which project)

All operations are synchronous SQLite (matching VectorMemory pattern).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.db.sqlite_utils import SqliteHelper

log = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")[:64] or "untitled"


VALID_STATUSES = {"active", "paused", "completed", "archived"}
VALID_CATEGORIES = {
    "decision",
    "fact",
    "requirement",
    "note",
    "link",
    "contact",
    "task",
}
VALID_RELATIONS = {
    "relates_to",
    "depends_on",
    "contradicts",
    "supersedes",
    "part_of",
    "references",
}


class ProjectStore(SqliteHelper):
    """Manages projects, knowledge entries, and their relationships."""

    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()

    def connect(self) -> None:
        self._open_connection()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        assert self._db is not None
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                status      TEXT DEFAULT 'active',
                tags        TEXT DEFAULT '[]',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title           TEXT DEFAULT '',
                text            TEXT NOT NULL,
                category        TEXT DEFAULT 'note',
                source          TEXT DEFAULT 'chat',
                source_chat_id  TEXT DEFAULT '',
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id     INTEGER NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
                to_id       INTEGER NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
                relation    TEXT NOT NULL,
                weight      REAL DEFAULT 1.0,
                UNIQUE(from_id, to_id, relation)
            );

            CREATE TABLE IF NOT EXISTS project_chats (
                project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                chat_id     TEXT NOT NULL,
                role        TEXT DEFAULT 'contributor',
                PRIMARY KEY (project_id, chat_id)
            );

            CREATE INDEX IF NOT EXISTS idx_knowledge_project
                ON knowledge_entries(project_id);

            CREATE INDEX IF NOT EXISTS idx_knowledge_category
                ON knowledge_entries(project_id, category);

            CREATE INDEX IF NOT EXISTS idx_knowledge_source_chat
                ON knowledge_entries(source_chat_id);

            CREATE INDEX IF NOT EXISTS idx_links_from
                ON knowledge_links(from_id);

            CREATE INDEX IF NOT EXISTS idx_links_to
                ON knowledge_links(to_id);
            """
        )
        self._db.commit()

    # ── Projects ──────────────────────────────────────────────────────────

    def create_project(
        self,
        name: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pid = project_id or _slugify(name)
        now = time.time()
        tags_json = json.dumps(tags or [])
        self._execute_and_commit(
            "INSERT OR IGNORE INTO projects (id, name, description, status, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?)",
            (pid, name, description, tags_json, now, now),
        )
        return self.get_project(pid)

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT id, name, description, status, tags, created_at, updated_at "
            "FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def list_projects(
        self,
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT id, name, description, status, tags, created_at, updated_at FROM projects"
        params: list[Any] = []
        conditions: list[str] = []

        if status:
            conditions.append("status = ?")
            params.append(status)

        rows = self._execute(
            query + (" WHERE " + " AND ".join(conditions) if conditions else ""),
            params,
        ).fetchall()

        results = [self._row_to_project(r) for r in rows]
        if tag:
            results = [p for p in results if tag in p["tags"]]
        return results

    def update_project(
        self,
        project_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        existing = self.get_project(project_id)
        if not existing:
            return None

        updates: dict[str, Any] = {"updated_at": time.time()}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(f"Invalid status: {status}")
            updates["status"] = status
        if tags is not None:
            updates["tags"] = json.dumps(tags)

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [project_id]
        self._execute_and_commit(
            f"UPDATE projects SET {set_clause} WHERE id = ?", values
        )
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        cur = self._execute_and_commit(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        return cur.rowcount > 0

    def _row_to_project(self, row: tuple) -> Dict[str, Any]:
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "status": row[3],
            "tags": json.loads(row[4]),
            "created_at": row[5],
            "updated_at": row[6],
        }

    # ── Knowledge Entries ─────────────────────────────────────────────────

    def add_knowledge(
        self,
        project_id: str,
        text: str,
        title: str = "",
        category: str = "note",
        source: str = "chat",
        source_chat_id: str = "",
    ) -> Dict[str, Any]:
        if category not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category: {category}")
        now = time.time()
        assert self._db is not None
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO knowledge_entries "
                "(project_id, title, text, category, source, source_chat_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, title, text, category, source, source_chat_id, now, now),
            )
            row_id = cur.lastrowid
            self._db.commit()
        return self.get_knowledge(row_id)

    def get_knowledge(self, entry_id: int) -> Optional[Dict[str, Any]]:
        row = self._execute(
            "SELECT id, project_id, title, text, category, source, source_chat_id, "
            "created_at, updated_at FROM knowledge_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_knowledge(row)

    def update_knowledge(
        self,
        entry_id: int,
        title: Optional[str] = None,
        text: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        updates: dict[str, Any] = {"updated_at": time.time()}
        if title is not None:
            updates["title"] = title
        if text is not None:
            updates["text"] = text
        if category is not None:
            if category not in VALID_CATEGORIES:
                raise ValueError(f"Invalid category: {category}")
            updates["category"] = category

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [entry_id]
        self._execute_and_commit(
            f"UPDATE knowledge_entries SET {set_clause} WHERE id = ?", values
        )
        return self.get_knowledge(entry_id)

    def delete_knowledge(self, entry_id: int) -> bool:
        assert self._db is not None
        with self._lock:
            self._db.execute(
                "DELETE FROM knowledge_links WHERE from_id = ? OR to_id = ?",
                (entry_id, entry_id),
            )
            cur = self._db.execute(
                "DELETE FROM knowledge_entries WHERE id = ?", (entry_id,)
            )
            self._db.commit()
            return cur.rowcount > 0

    def list_knowledge(
        self,
        project_id: str,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = (
            "SELECT id, project_id, title, text, category, source, source_chat_id, "
            "created_at, updated_at FROM knowledge_entries WHERE project_id = ?"
        )
        params: list[Any] = [project_id]
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._execute(query, params).fetchall()
        return [self._row_to_knowledge(r) for r in rows]

    def count_knowledge(self, project_id: str) -> int:
        row = self._execute(
            "SELECT COUNT(*) FROM knowledge_entries WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row[0] if row else 0

    def _row_to_knowledge(self, row: tuple) -> Dict[str, Any]:
        return {
            "id": row[0],
            "project_id": row[1],
            "title": row[2],
            "text": row[3],
            "category": row[4],
            "source": row[5],
            "source_chat_id": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

    # ── Knowledge Links (Graph Edges) ─────────────────────────────────────

    def link_knowledge(
        self,
        from_id: int,
        to_id: int,
        relation: str,
        weight: float = 1.0,
    ) -> Optional[int]:
        if relation not in VALID_RELATIONS:
            raise ValueError(f"Invalid relation: {relation}")
        assert self._db is not None
        try:
            with self._lock:
                cur = self._db.execute(
                    "INSERT INTO knowledge_links (from_id, to_id, relation, weight) "
                    "VALUES (?, ?, ?, ?)",
                    (from_id, to_id, relation, weight),
                )
                self._db.commit()
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def unlink_knowledge(self, from_id: int, to_id: int, relation: str) -> bool:
        cur = self._execute_and_commit(
            "DELETE FROM knowledge_links WHERE from_id = ? AND to_id = ? AND relation = ?",
            (from_id, to_id, relation),
        )
        return cur.rowcount > 0

    def get_outgoing_links(self, entry_id: int) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT id, from_id, to_id, relation, weight FROM knowledge_links WHERE from_id = ?",
            (entry_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "from_id": r[1],
                "to_id": r[2],
                "relation": r[3],
                "weight": r[4],
            }
            for r in rows
        ]

    def get_incoming_links(self, entry_id: int) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT id, from_id, to_id, relation, weight FROM knowledge_links WHERE to_id = ?",
            (entry_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "from_id": r[1],
                "to_id": r[2],
                "relation": r[3],
                "weight": r[4],
            }
            for r in rows
        ]

    # ── Project-Chats Binding ─────────────────────────────────────────────

    def bind_chat(
        self, project_id: str, chat_id: str, role: str = "contributor"
    ) -> None:
        self._execute_and_commit(
            "INSERT OR IGNORE INTO project_chats (project_id, chat_id, role) VALUES (?, ?, ?)",
            (project_id, chat_id, role),
        )

    def unbind_chat(self, project_id: str, chat_id: str) -> bool:
        cur = self._execute_and_commit(
            "DELETE FROM project_chats WHERE project_id = ? AND chat_id = ?",
            (project_id, chat_id),
        )
        return cur.rowcount > 0

    def get_project_chats(self, project_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT project_id, chat_id, role FROM project_chats WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [{"project_id": r[0], "chat_id": r[1], "role": r[2]} for r in rows]

    def get_chat_projects(self, chat_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT p.id, p.name, p.status FROM projects p "
            "JOIN project_chats pc ON p.id = pc.project_id "
            "WHERE pc.chat_id = ? AND p.status != 'archived'",
            (chat_id,),
        ).fetchall()
        return [{"id": r[0], "name": r[1], "status": r[2]} for r in rows]

    def get_edges_batch(self, entry_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch all edges involving any of the given entry IDs in a single query."""
        if not entry_ids:
            return []
        placeholders = ",".join("?" * len(entry_ids))
        rows = self._execute(
            f"SELECT id, from_id, to_id, relation, weight "
            f"FROM knowledge_links "
            f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
            entry_ids + entry_ids,
        ).fetchall()
        return [
            {
                "id": r[0],
                "from_id": r[1],
                "to_id": r[2],
                "relation": r[3],
                "weight": r[4],
            }
            for r in rows
        ]

    def get_knowledge_batch(self, entry_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Fetch multiple knowledge entries by ID in a single query."""
        if not entry_ids:
            return {}
        placeholders = ",".join("?" * len(entry_ids))
        rows = self._execute(
            f"SELECT id, project_id, title, text, category, source, source_chat_id, "
            f"created_at, updated_at FROM knowledge_entries "
            f"WHERE id IN ({placeholders})",
            entry_ids,
        ).fetchall()
        return {r[0]: self._row_to_knowledge(r) for r in rows}
