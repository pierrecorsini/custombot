"""
vector_memory — SQLite-vec backed semantic memory store.

Stores text snippets with their embeddings in a shared SQLite database.
Each entry is tagged with a chat_id for per-chat isolation via metadata
filtering on KNN queries.

Uses sqlite-vec (pure C, no Faiss) with cosine distance metric.
Embeddings are generated via the OpenAI embeddings API.

Lock model: WAL mode allows concurrent reads from multiple threads.
Write operations (insert, delete) are serialized via _write_lock.
Embedding cache access is protected by _cache_lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite_vec
from openai import AsyncOpenAI

from src.db.sqlite_utils import SqliteHelper
from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import DiskSpaceError
from src.utils import BoundedOrderedDict, DEFAULT_MIN_DISK_SPACE, check_disk_space
from src.utils.locking import ThreadLock
from src.utils.retry import retry_with_backoff
from src.vector_memory._utils import _serialize_f32, _track_embed_cache_event
from src.vector_memory.batch import BatchEmbedMixin
from src.vector_memory.health import EmbeddingHealthMixin

# Bump this when the schema changes.  Migrations in _MIGRATIONS below will
# bring older databases up to this version incrementally.
_SCHEMA_VERSION = 1

# Ordered list of migrations: (target_version, [sql, ...]).
# Each entry runs ALTER/CREATE statements to move the schema from
# (target_version - 1) → target_version.
_MIGRATIONS: list[tuple[int, list[str]]] = [
    # Example for a future migration:
    # (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
]

log = logging.getLogger(__name__)


class VectorMemory(EmbeddingHealthMixin, BatchEmbedMixin, SqliteHelper):
    """Manages a shared SQLite-vec database for semantic memory."""

    def __init__(
        self,
        db_path: str,
        openai_client: AsyncOpenAI,
        embedding_model: str = "text-embedding-3-small",
        embedding_dimensions: int = 1536,
    ) -> None:
        self._db_path = Path(db_path)
        self._write_lock = ThreadLock()
        self._cache_lock = ThreadLock()
        # Alias for SqliteHelper mixin methods (_execute, _commit, etc.)
        self._lock = self._write_lock
        self._client = openai_client
        self._embedding_model = embedding_model
        self._dimensions = embedding_dimensions
        # LRU cache for embeddings — avoids redundant API calls for identical text
        self._embed_cache: BoundedOrderedDict[str, list[float]] = BoundedOrderedDict(max_size=256, eviction="half")
        # In-flight deduplication: tracks pending embedding requests so
        # concurrent calls for the same text share one API call
        self._inflight: dict[str, asyncio.Future[list[float]]] = {}
        # Per-thread read connection pool — each thread reuses one read-only
        # connection instead of opening/closing per query, eliminating
        # ~5ms sqlite-vec extension loading overhead on every read.
        self._thread_local = threading.local()
        self._read_connections: list[sqlite3.Connection] = []
        self._read_pool_lock = ThreadLock()
        # Embedding API health cache — avoids repeated full-timeout waits when
        # the endpoint is down.  Protected by _cache_lock (already used for
        # the embed LRU cache, so no additional lock needed).
        self._embed_api_healthy: bool = True
        self._embed_api_last_check: float = 0.0

        # Batch coalescing: rapid save() calls are queued and flushed together
        # through _embed_batch() after a short debounce window, reducing the
        # number of individual embedding API calls.
        self._pending_saves: list[tuple[str, asyncio.Future[list[float]]]] = []
        self._flush_handle: asyncio.TimerHandle | None = None

        # Retry queue for saves that failed due to embedding API outages.
        # Each entry is (chat_id, text, category, queued_at_timestamp).
        # Flushed opportunistically when a subsequent save succeeds.
        self._pending_retries: list[tuple[str, str, str, float]] = []

    # ── lifecycle ───────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open (or create) the SQLite database and ensure schema exists.

        Uses WAL journal mode (via SqliteHelper defaults) so that reads
        can proceed concurrently while writes are serialized by _write_lock.
        """
        self._open_connection(load_extension=False)
        assert self._db is not None
        # Load sqlite-vec extension (extension loading was NOT enabled by
        # _open_connection when load_extension=False)
        self._db.enable_load_extension(True)
        sqlite_vec.load(self._db)
        self._db.enable_load_extension(False)
        self._ensure_schema()

    def warmup(self) -> None:
        """Pre-warm one read connection so the first user query avoids the
        sqlite-vec extension loading latency (~5ms).

        Safe to call from the startup orchestrator on the main thread.
        Subsequent reads from other threads create their own connections.
        """
        try:
            self._get_read_connection()
            log.debug("VectorMemory read connection pre-warmed")
        except Exception as exc:
            log.debug("VectorMemory read connection warmup skipped: %s", exc)

    def close(self) -> None:
        """Release all resources: embed cache, in-flight futures, read pool, and DB connection."""
        with self._cache_lock:
            self._embed_cache = BoundedOrderedDict(max_size=256, eviction="half")
        # Cancel pending batch flush
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        # Cancel any pending save futures
        for _, future in self._pending_saves:
            if not future.done():
                future.cancel()
        self._pending_saves.clear()
        # Report and clear any queued retry saves
        if self._pending_retries:
            log.info(
                "VectorMemory shutting down with %d queued retry saves (dropped)",
                len(self._pending_retries),
            )
            self._pending_retries.clear()
        # Cancel any pending in-flight embedding futures
        for key, future in list(self._inflight.items()):
            if not future.done():
                future.cancel()
        self._inflight.clear()
        self._close_read_connections()
        super().close()
        log.debug("VectorMemory closed (cache cleared, read pool released, DB connection released)")

    async def probe_embedding_model(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Probe the embedding API to validate the configured model is reachable.

        Returns ``(success, message)``.  Embeds a short test string to catch
        misconfigured model names, invalid API keys, or unreachable endpoints
        before the bot starts accepting messages.
        """
        try:
            resp = await asyncio.wait_for(
                self._client.embeddings.create(
                    model=self._embedding_model,
                    input="health",
                ),
                timeout=timeout,
            )
            if not resp.data:
                return False, "Empty response from embedding API"
            actual_dims = len(resp.data[0].embedding)
            self._mark_embedding_api_healthy()
            return True, f"dims={actual_dims}"
        except asyncio.TimeoutError:
            return False, f"Timeout after {timeout}s"
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            return False, f"{type(exc).__name__}: {exc}"

    # ── disk safety ──────────────────────────────────────────────────────

    def _check_disk_space_before_write(self) -> None:
        """Raise DiskSpaceError if the database volume is too full."""
        try:
            result = check_disk_space(self._db_path, min_bytes=DEFAULT_MIN_DISK_SPACE)
            if not result.has_sufficient_space:
                raise DiskSpaceError(
                    "Insufficient disk space for vector memory write",
                    path=str(self._db_path),
                    free_mb=round(result.free_mb, 2),
                    required_mb=round(DEFAULT_MIN_DISK_SPACE / (1024 * 1024), 2),
                )
        except OSError as e:
            log.warning("Could not verify disk space for %s: %s", self._db_path, e)

    # ── read connections ────────────────────────────────────────────────

    def _get_read_connection(self) -> sqlite3.Connection:
        """Return a per-thread pooled read-only connection.

        Each thread gets exactly one read connection that is reused across
        calls, eliminating the sqlite-vec extension loading overhead (~5ms)
        on every read.  Uses URI mode with ``?mode=ro`` so the connection
        cannot write.  WAL mode allows concurrent reads with writes.
        """
        conn: sqlite3.Connection | None = getattr(self._thread_local, "read_conn", None)
        if conn is not None:
            return conn

        assert self._db is not None, "VectorMemory not connected — call connect() first"
        resolved = self._db_path.resolve()
        uri = f"file:{resolved}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        self._thread_local.read_conn = conn
        with self._read_pool_lock:
            self._read_connections.append(conn)
        return conn

    def _close_read_connections(self) -> None:
        """Close all pooled read connections across every thread."""
        with self._read_pool_lock:
            for conn in self._read_connections:
                try:
                    conn.close()
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.CONNECTION_CLEANUP,
                        "Failed to close read connection during shutdown",
                        logger=log,
                    )
            self._read_connections.clear()
        # Reset thread-local so next access on any thread creates a fresh conn
        self._thread_local = threading.local()

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
        # Schema version tracking table
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS _schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._db.commit()
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Apply incremental schema migrations up to _SCHEMA_VERSION."""
        assert self._db is not None
        row = self._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'version'"
        ).fetchone()
        current = int(row[0]) if row else 0

        if current >= _SCHEMA_VERSION:
            return

        for target_version, statements in _MIGRATIONS:
            if current < target_version:
                for sql in statements:
                    self._db.execute(sql)
                self._db.execute(
                    "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES ('version', ?)",
                    (str(target_version),),
                )
                self._db.commit()
                log.info("VectorMemory schema migrated to version %d", target_version)

        # Record final version (covers fresh databases where _MIGRATIONS is empty)
        self._db.execute(
            "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES ('version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        self._db.commit()

    # ── embeddings ──────────────────────────────────────────────────────

    @retry_with_backoff(max_retries=2, initial_delay=0.5)
    async def _embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string, with LRU caching
        and in-flight deduplication.

        If two concurrent calls request the same text, the second one
        awaits the first's result instead of making a duplicate API call.

        Cache access is protected by _cache_lock; DB writes use _write_lock.
        """
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # 1. Check LRU cache (fast path)
        with self._cache_lock:
            if cache_key in self._embed_cache:
                _track_embed_cache_event(hit=True)
                return self._embed_cache[cache_key]

        # 2. Check in-flight requests (dedup concurrent calls)
        _track_embed_cache_event(hit=False)
        loop = asyncio.get_running_loop()
        if cache_key in self._inflight:
            return await self._inflight[cache_key]

        # 3. This is the first request for this key — create a Future
        future: asyncio.Future[list[float]] = loop.create_future()
        self._inflight[cache_key] = future

        try:
            resp = await self._client.embeddings.create(
                model=self._embedding_model,
                input=text,
            )
            embedding = resp.data[0].embedding

            self._mark_embedding_api_healthy()

            # Store in cache (BoundedOrderedDict handles eviction)
            with self._cache_lock:
                self._embed_cache[cache_key] = embedding

            # Resolve the future so waiters get the result
            future.set_result(embedding)
            return embedding
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            # Propagate error to waiters
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            # Clean up in-flight entry
            self._inflight.pop(cache_key, None)

    # ── public API ──────────────────────────────────────────────────────

    async def save(self, chat_id: str, text: str, category: str = "") -> int:
        """Insert a memory entry with its embedding. Returns the row ID.

        Uses batch coalescing: rapid ``save()`` calls are grouped into a
        single embedding API request via a short debounce window, reducing
        API overhead when many memories are saved in quick succession.

        If the embedding API is unreachable mid-session, the save is queued
        for retry and ``-1`` is returned — callers never see an exception.
        """
        assert self._db is not None

        try:
            self._check_embedding_api_health()
            embedding = await self._batched_embed(text)
            now = time.time()

            # Run synchronous DB writes in thread pool to avoid blocking the event loop
            row_id = await asyncio.to_thread(
                self._insert_entry, chat_id, text, category, now, embedding
            )

            log.debug("Saved vector memory id=%d chat=%s", row_id, chat_id)

            # Opportunistically flush queued retries on success
            if self._pending_retries:
                asyncio.ensure_future(self._retry_pending_saves())

            return row_id
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            log_noncritical(
                NonCriticalCategory.EMBEDDING,
                f"Embedding API unreachable during save(chat={chat_id}); queuing for retry: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            self._queue_for_retry(chat_id, text, category)
            return -1

    async def save_batch(
        self,
        chat_id: str,
        items: list[tuple[str, str]],
    ) -> list[int]:
        """Insert multiple memory entries using a single batched embedding call.

        Args:
            chat_id: Chat identifier for all entries.
            items: List of ``(text, category)`` tuples to save.

        Returns:
            List of row IDs in the same order as *items*.
        """
        assert self._db is not None

        if not items:
            return []

        try:
            self._check_embedding_api_health()
            texts = [text for text, _ in items]
            embeddings = await self._embed_batch(texts)
            now = time.time()

            rows = await asyncio.to_thread(
                self._insert_entries, chat_id, items, now, embeddings
            )

            log.debug("Batch-saved %d vector memories chat=%s", len(rows), chat_id)

            # Opportunistically flush queued retries on success
            if self._pending_retries:
                asyncio.ensure_future(self._retry_pending_saves())

            return rows
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            log_noncritical(
                NonCriticalCategory.EMBEDDING,
                f"Embedding API unreachable during save_batch(chat={chat_id}, "
                f"{len(items)} items); queuing for retry: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            for text, category in items:
                self._queue_for_retry(chat_id, text, category)
            return []

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
        self._check_disk_space_before_write()
        with self._write_lock:
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

    def _insert_entries(
        self,
        chat_id: str,
        items: list[tuple[str, str]],
        created_at: float,
        embeddings: list[list[float]],
    ) -> list[int]:
        """Synchronous batch DB insert (run in thread pool).

        All inserts are committed in a single transaction for efficiency.
        """
        assert self._db is not None
        self._check_disk_space_before_write()
        row_ids: list[int] = []
        with self._write_lock:
            for (text, category), embedding in zip(items, embeddings):
                cur = self._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (chat_id, text, category, created_at),
                )
                row_id = cur.lastrowid
                self._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(embedding)),
                )
                row_ids.append(row_id)
            self._db.commit()
        return row_ids

    async def search(self, chat_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Semantic search within a chat's memories.

        If the embedding API is unreachable, returns an empty list instead
        of propagating the exception — search failure must never break the
        caller's control flow.
        """
        assert self._db is not None

        if not query.strip():
            return []

        try:
            self._check_embedding_api_health()
            query_vec = _serialize_f32(await self._embed(query))

            return await asyncio.to_thread(self._search_sync, chat_id, query_vec, limit)
        except Exception as exc:
            self._mark_embedding_api_unhealthy()
            log_noncritical(
                NonCriticalCategory.EMBEDDING,
                f"Embedding API unreachable during search(chat={chat_id}); returning empty: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return []

    def _search_sync(self, chat_id: str, query_vec: bytes, limit: int) -> List[Dict[str, Any]]:
        """Synchronous DB search using the per-thread pooled read connection.

        WAL mode guarantees snapshot isolation across concurrent reads and writes.
        """
        conn = self._get_read_connection()
        rows = conn.execute(
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
        """Return the N most recent memories for a chat (no embedding).

        Uses the per-thread pooled read connection for true concurrency.
        """
        conn = self._get_read_connection()
        rows = conn.execute(
            """
            SELECT id, text, category, created_at
            FROM memory_entries
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [{"id": r[0], "text": r[1], "category": r[2], "created_at": r[3]} for r in rows]

    def delete(self, memory_id: int) -> bool:
        """Delete a memory entry by ID."""
        assert self._db is not None
        with self._write_lock:
            cur = self._db.execute("DELETE FROM memory_entries WHERE id = ?", (memory_id,))
            self._db.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
            self._db.commit()
            return cur.rowcount > 0

    def count(self, chat_id: str) -> int:
        """Count memories for a chat. Uses the per-thread pooled read connection."""
        conn = self._get_read_connection()
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row[0] if row else 0


__all__ = ["VectorMemory", "_serialize_f32"]
