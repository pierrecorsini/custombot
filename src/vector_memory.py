"""
vector_memory.py — SQLite-vec backed semantic memory store.

Stores text snippets with their embeddings in a shared SQLite database.
Each entry is tagged with a chat_id for per-chat isolation via metadata
filtering on KNN queries.

Uses sqlite-vec (pure C, no Faiss) with cosine distance metric.
Embeddings are generated via the OpenAI embeddings API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import struct
import time
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

import sqlite_vec

from src.db.sqlite_utils import SqliteHelper
from src.utils.retry import retry_with_backoff

log = logging.getLogger(__name__)


def _serialize_f32(vector: list[float]) -> bytes:
    """Pack a float32 list into binary BLOB for sqlite-vec."""
    return struct.pack("%sf" % len(vector), *vector)


class VectorMemory(SqliteHelper):
    """Manages a shared SQLite-vec database for semantic memory."""

    def __init__(
        self,
        db_path: str,
        openai_client: AsyncOpenAI,
        embedding_model: str = "text-embedding-3-small",
        embedding_dimensions: int = 1536,
    ) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._client = openai_client
        self._embedding_model = embedding_model
        self._dimensions = embedding_dimensions
        # LRU cache for embeddings — avoids redundant API calls for identical text
        # Protected by _lock since asyncio.to_thread can interleave access
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_cache_max = 256

    # ── lifecycle ───────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open (or create) the SQLite database and ensure schema exists."""
        self._open_connection(load_extension=False)
        # Load sqlite-vec extension
        assert self._db is not None
        self._db.enable_load_extension(True)
        sqlite_vec.load(self._db)
        self._db.enable_load_extension(False)
        self._ensure_schema()

    # ── schema ──────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        assert self._db is not None
        # Regular table for metadata + full text
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                category TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        # Vector table for similarity search
        dim = self._dimensions
        self._db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec
            USING vec0(
                embedding float[{dim}] distance_metric=cosine
            )
        """)
        self._db.commit()

    # ── embeddings ──────────────────────────────────────────────────────

    @retry_with_backoff(max_retries=2, initial_delay=0.5)
    async def _embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string, with LRU caching.

        Uses self._lock for thread safety since _embed_cache is shared
        with _insert_entry / _search_sync which run in thread pool.
        """
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            if cache_key in self._embed_cache:
                self._embed_cache.move_to_end(cache_key)
                return self._embed_cache[cache_key]

        resp = await self._client.embeddings.create(
            model=self._embedding_model,
            input=text,
        )
        embedding = resp.data[0].embedding

        # Store in cache with LRU eviction (thread-safe)
        with self._lock:
            self._embed_cache[cache_key] = embedding
            if len(self._embed_cache) > self._embed_cache_max:
                self._embed_cache.popitem(last=False)

        return embedding

    # ── public API ──────────────────────────────────────────────────────

    async def save(self, chat_id: str, text: str, category: str = "") -> int:
        """Insert a memory entry with its embedding. Returns the row ID."""
        assert self._db is not None

        embedding = await self._embed(text)
        now = time.time()

        # Run synchronous DB writes in thread pool to avoid blocking the event loop
        row_id = await asyncio.to_thread(
            self._insert_entry, chat_id, text, category, now, embedding
        )

        log.debug("Saved vector memory id=%d chat=%s", row_id, chat_id)
        return row_id

    def _insert_entry(
        self,
        chat_id: str,
        text: str,
        category: str,
        created_at: float,
        embedding: list[float],
    ) -> int:
        """Synchronous DB insert (run in thread pool)."""
        assert self._db is not None
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO memory_entries (chat_id, text, category, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, text, category, created_at),
            )
            row_id = cur.lastrowid
            self._db.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                (row_id, _serialize_f32(embedding)),
            )
            self._db.commit()
            return row_id

    async def search(
        self, chat_id: str, query: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Semantic search within a chat's memories."""
        assert self._db is not None

        query_vec = _serialize_f32(await self._embed(query))

        return await asyncio.to_thread(self._search_sync, chat_id, query_vec, limit)

    def _search_sync(
        self, chat_id: str, query_vec: bytes, limit: int
    ) -> List[Dict[str, Any]]:
        """Synchronous DB search (run in thread pool)."""
        assert self._db is not None
        with self._lock:
            rows = self._db.execute(
                """
                SELECT e.id, e.text, e.category, e.created_at, v.distance
                FROM memory_vec v
                JOIN memory_entries e ON e.id = v.rowid
                WHERE v.embedding MATCH ?
                  AND v.k = ?
                  AND e.chat_id = ?
                ORDER BY v.distance
                """,
                (query_vec, limit, chat_id),
            ).fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "category": r[2],
                "created_at": r[3],
                "distance": r[4],
            }
            for r in rows
        ]

    def list_recent(self, chat_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the N most recent memories for a chat (no embedding). Runs synchronously — wrap in asyncio.to_thread() if calling from async code."""
        assert self._db is not None
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, text, category, created_at
                FROM memory_entries
                WHERE chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "text": r[1], "category": r[2], "created_at": r[3]}
            for r in rows
        ]

    def delete(self, memory_id: int) -> bool:
        """Delete a memory entry by ID."""
        assert self._db is not None
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM memory_entries WHERE id = ?", (memory_id,)
            )
            self._db.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
            self._db.commit()
            return cur.rowcount > 0

    def count(self, chat_id: str) -> int:
        """Count memories for a chat."""
        assert self._db is not None
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return row[0] if row else 0
