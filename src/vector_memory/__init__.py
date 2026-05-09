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
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import sqlite_vec

from src.db.sqlite_pool import DEFAULT_PRAGMAS
from src.db.sqlite_utils import SqliteHelper
from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import DiskSpaceError
from src.utils import BoundedOrderedDict, DEFAULT_MIN_DISK_SPACE, check_disk_space
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.locking import ThreadLock
from src.utils.retry import retry_with_backoff
from src.vector_memory._utils import _cache_key, _serialize_f32, _track_embed_cache_event
from src.vector_memory.batch import BatchEmbedMixin
from src.vector_memory.health import EmbeddingHealthMixin

if TYPE_CHECKING:
    from openai import AsyncOpenAI

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
        embed_cache_size: int = 256,
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
        self._embed_cache_size = embed_cache_size
        self._embed_cache: BoundedOrderedDict[str, list[float]] = BoundedOrderedDict(
            max_size=embed_cache_size, eviction="half"
        )
        # In-flight deduplication: tracks pending embedding requests so
        # concurrent calls for the same text share one API call.  Bounded
        # by _MAX_INFLIGHT to prevent unbounded memory growth under high
        # concurrency — additional callers wait on the semaphore instead
        # of creating new Future entries.
        self._inflight: dict[str, asyncio.Future[list[float]]] = {}
        self._MAX_INFLIGHT = 64
        self._inflight_semaphore = asyncio.Semaphore(self._MAX_INFLIGHT)
        # Per-thread read connection pool — each thread reuses one read-only
        # connection instead of opening/closing per query, eliminating
        # ~5ms sqlite-vec extension loading overhead on every read.
        self._thread_local = threading.local()
        self._read_connections: list[sqlite3.Connection] = []
        self._read_pool_lock = ThreadLock()
        # Circuit breaker for embedding API health — avoids repeated full-timeout
        # waits when the endpoint is down.  5 failures within 60 seconds opens
        # the circuit; search returns [], save queues for retry.
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            cooldown_seconds=60.0,
        )

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
        self._check_embedding_model()
        self._load_retry_queue()

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
            self._embed_cache = BoundedOrderedDict(max_size=self._embed_cache_size, eviction="half")
        # Cancel pending batch flush
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        # Cancel any pending save futures
        for _, future in self._pending_saves:
            if not future.done():
                future.cancel()
        self._pending_saves.clear()
        # Report and persist any queued retry saves so they survive restart
        if self._pending_retries:
            self._persist_retry_queue()
            self._pending_retries.clear()
        # Cancel any pending in-flight embedding futures
        for key, future in list(self._inflight.items()):
            if not future.done():
                future.cancel()
        self._inflight.clear()
        self._close_read_connections()
        super().close()
        log.debug("VectorMemory closed (cache cleared, read pool released, DB connection released)")

    # ── retry queue persistence ──────────────────────────────────────────

    _RETRY_QUEUE_FILE = "_retry_queue.json"

    def _persist_retry_queue(self) -> None:
        """Write pending retry saves to a JSON sidecar file for next startup."""
        if not self._pending_retries:
            return
        import json

        retry_path = self._db_path.parent / self._RETRY_QUEUE_FILE
        try:
            data = [
                {"chat_id": cid, "text": txt, "category": cat, "queued_at": ts}
                for cid, txt, cat, ts in self._pending_retries
            ]
            retry_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            log.info(
                "Persisted %d retry saves to %s for next startup",
                len(data),
                retry_path,
            )
        except OSError as exc:
            log.warning("Failed to persist retry queue: %s", exc)

    def _load_retry_queue(self) -> None:
        """Reload persisted retry saves from a previous shutdown."""
        import json

        from src.utils import safe_json_parse, JsonParseMode

        retry_path = self._db_path.parent / self._RETRY_QUEUE_FILE
        if not retry_path.exists():
            return
        try:
            data = safe_json_parse(
                retry_path.read_text(encoding="utf-8"),
                default=[],
                expected_type=list,
                mode=JsonParseMode.STRICT,
            )
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict) and all(k in entry for k in ("chat_id", "text")):
                        self._pending_retries.append(
                            (
                                entry["chat_id"],
                                entry["text"],
                                entry.get("category", ""),
                                entry.get("queued_at", time.time()),
                            )
                        )
                if self._pending_retries:
                    log.info(
                        "Loaded %d retry saves from previous session",
                        len(self._pending_retries),
                    )
            # Clean up the file after loading
            retry_path.unlink()
        except OSError as exc:
            log.warning("Failed to load retry queue: %s", exc)

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
                    encoding_format="float",
                ),
                timeout=timeout,
            )
            if not resp.data:
                return False, "Empty response from embedding API"
            actual_dims = len(resp.data[0].embedding)
            await self._mark_embedding_api_healthy()
            return True, f"dims={actual_dims}"
        except asyncio.TimeoutError:
            return False, f"Timeout after {timeout}s"
        except Exception as exc:
            await self._mark_embedding_api_unhealthy()
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
        except OSError as exc:
            log.warning("Could not verify disk space for %s: %s", self._db_path, exc)

    # ── read connections ────────────────────────────────────────────────

    def _get_read_connection(self) -> sqlite3.Connection:
        """Return a per-thread pooled read-only connection.

        Each thread gets exactly one read connection that is reused across
        calls, eliminating the sqlite-vec extension loading overhead (~5ms)
        on every read.  Uses URI mode with ``?mode=ro`` so the connection
        cannot write.  WAL mode allows concurrent reads with writes.

        Read connections are registered in the shared ``SqliteConnectionPool``
        (when configured) so that ``close_all()`` during shutdown can clean
        them up alongside write connections.
        """
        conn: sqlite3.Connection | None = getattr(self._thread_local, "read_conn", None)
        if conn is not None:
            return conn

        assert self._db is not None, "VectorMemory not connected — call connect() first"
        resolved = self._db_path.resolve()
        uri = f"file:{resolved}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        for pragma in DEFAULT_PRAGMAS:
            conn.execute(pragma)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        self._thread_local.read_conn = conn
        with self._read_pool_lock:
            self._read_connections.append(conn)

        # Register in the shared pool so shutdown can close all connections.
        pool = SqliteHelper._pool
        if pool is not None:
            tid = threading.get_ident()
            pool.register(f"VectorMemory.read.{tid}", conn, self._db_path)
        return conn

    def _close_read_connections(self) -> None:
        """Close all pooled read connections across every thread."""
        pool = SqliteHelper._pool
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
        # Unregister all read connections from the shared pool.
        if pool is not None:
            for name in list(pool.active_connections):
                if name.startswith("VectorMemory.read."):
                    pool.unregister(name)
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
        row = self._db.execute("SELECT value FROM _schema_meta WHERE key = 'version'").fetchone()
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

    def _check_embedding_model(self) -> None:
        """Detect embedding model changes across restarts.

        Stores the configured embedding model name and dimensions in
        ``_schema_meta`` on first write.  On subsequent startups, compares
        the stored values against the current configuration and emits a
        loud warning when they differ — existing vectors become silently
        incompatible when the model or dimensionality changes.
        """
        assert self._db is not None
        stored_model = self._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'embedding_model'"
        ).fetchone()
        stored_dims = self._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'embedding_dimensions'"
        ).fetchone()

        model_changed = stored_model is not None and stored_model[0] != self._embedding_model
        dims_changed = stored_dims is not None and int(stored_dims[0]) != self._dimensions

        if model_changed:
            log.warning(
                "Embedding model changed since last run: "
                "stored=%r, current=%r. Existing vectors are INCOMPATIBLE "
                "and will return incorrect results. Consider re-indexing "
                "or deleting the vector memory database.",
                stored_model[0],
                self._embedding_model,
            )
        if dims_changed:
            log.warning(
                "Embedding dimensions changed since last run: "
                "stored=%s, current=%d. Existing vectors are INCOMPATIBLE "
                "and will return incorrect results. Consider re-indexing "
                "or deleting the vector memory database.",
                stored_dims[0],
                self._dimensions,
            )

        # Always stamp current config so a fresh DB records the model on first run
        self._db.execute(
            "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES ('embedding_model', ?)",
            (self._embedding_model,),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO _schema_meta (key, value) VALUES ('embedding_dimensions', ?)",
            (str(self._dimensions),),
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
        cache_key = _cache_key(text)

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
            async with self._inflight_semaphore:
                resp = await self._client.embeddings.create(
                    model=self._embedding_model,
                    input=text,
                    encoding_format="float",
                )
                embedding = resp.data[0].embedding

            await self._mark_embedding_api_healthy()

            # Store in cache (BoundedOrderedDict handles eviction)
            with self._cache_lock:
                self._embed_cache[cache_key] = embedding

            # Resolve the future so waiters get the result
            future.set_result(embedding)
            return embedding
        except Exception as exc:
            await self._mark_embedding_api_unhealthy()
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

        DB-level errors (corruption, sqlite-vec unavailable) are not queued
        for retry since retrying won't fix the underlying problem.
        """
        assert self._db is not None

        try:
            await self._check_embedding_api_health()
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
        except ConnectionError:
            # Circuit breaker open — skip embedding, queue for retry
            log.info(
                "Vector save skipped for chat %s (embedding circuit breaker open), queuing",
                chat_id,
            )
            self._queue_for_retry(chat_id, text, category)
            return -1
        except sqlite3.Error as exc:
            # DB-level error — not retryable, don't queue
            log_noncritical(
                NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                f"Database error during save(chat={chat_id}), not queuing for retry: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return -1
        except Exception as exc:
            await self._mark_embedding_api_unhealthy()
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
            await self._check_embedding_api_health()
            texts = [text for text, _ in items]
            embeddings = await self._embed_batch(texts)
            now = time.time()

            rows = await asyncio.to_thread(self._insert_entries, chat_id, items, now, embeddings)

            log.debug("Batch-saved %d vector memories chat=%s", len(rows), chat_id)

            # Opportunistically flush queued retries on success
            if self._pending_retries:
                asyncio.ensure_future(self._retry_pending_saves())

            return rows
        except sqlite3.Error as exc:
            # DB-level error — not retryable, don't queue
            log_noncritical(
                NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                f"Database error during save_batch(chat={chat_id}, "
                f"{len(items)} items), not queuing for retry: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return []
        except Exception as exc:
            await self._mark_embedding_api_unhealthy()
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

        All inserts are wrapped in a single explicit ``BEGIN IMMEDIATE``
        transaction so the SQLite write lock is acquired up-front and all
        fsync overhead is deferred to the final ``COMMIT``.  This reduces
        disk I/O by 10-100× compared to individual autocommit inserts.
        """
        assert self._db is not None
        self._check_disk_space_before_write()
        row_ids: list[int] = []
        with self._write_lock:
            self._db.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
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
            except BaseException:
                self._db.rollback()
                raise
        return row_ids

    async def search(self, chat_id: str, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Semantic search within a chat's memories.

        When the circuit breaker is open (embedding API degraded), returns an
        empty list immediately without waiting for timeouts.  DB-level errors
        are **propagated** so callers can fall back to text-based search.
        """
        assert self._db is not None

        if not query.strip():
            return []

        # Phase 1: Embedding — API errors are non-fatal, return empty.
        try:
            await self._check_embedding_api_health()
            query_vec = _serialize_f32(await self._embed(query))
        except ConnectionError:
            # Circuit breaker open — return empty without blocking
            log.info(
                "Vector search skipped for chat %s (embedding circuit breaker open)",
                chat_id,
            )
            return []
        except Exception as exc:
            await self._mark_embedding_api_unhealthy()
            log_noncritical(
                NonCriticalCategory.EMBEDDING,
                f"Embedding API unreachable during search(chat={chat_id}); returning empty: {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return []

        # Phase 2: DB query — DB errors propagate so callers can fall back
        # to text-based search (regex/grep over MEMORY.md).
        return await asyncio.to_thread(self._search_sync, chat_id, query_vec, limit)

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
        Returns an empty list on DB errors so callers can fall back to
        text-based MEMORY.md without raising.
        """
        try:
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
        except sqlite3.Error as exc:
            log_noncritical(
                NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                f"Database error in list_recent(chat={chat_id}): {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return []

    def delete(self, memory_id: int) -> bool:
        """Delete a memory entry by ID."""
        assert self._db is not None
        with self._write_lock:
            cur = self._db.execute("DELETE FROM memory_entries WHERE id = ?", (memory_id,))
            self._db.execute("DELETE FROM memory_vec WHERE rowid = ?", (memory_id,))
            self._db.commit()
            return cur.rowcount > 0

    def count(self, chat_id: str) -> int:
        """Count memories for a chat. Uses the per-thread pooled read connection.

        Returns 0 on DB errors so callers degrade gracefully.
        """
        try:
            conn = self._get_read_connection()
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return row[0] if row else 0
        except sqlite3.Error as exc:
            log_noncritical(
                NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                f"Database error in count(chat={chat_id}): {exc}",
                logger=log,
                level=logging.WARNING,
            )
            return 0

    def health_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of embedding health for monitoring.

        Thread-safe — acquires ``_cache_lock`` briefly to read state.
        Returns a dict with:
            - ``embedding_api_healthy``: whether the circuit breaker is closed
            - ``retry_queue_depth``: number of pending retry saves
            - ``retry_queue_capacity``: queue_depth / max_queue_size (0.0–1.0)
            - ``circuit_breaker_state``: current breaker state string
        """
        from src.utils.circuit_breaker import CircuitState

        breaker_state = self._circuit_breaker.state
        api_healthy = breaker_state == CircuitState.CLOSED

        with self._cache_lock:
            queue_depth = len(self._pending_retries)

        from src.vector_memory.health import _MAX_RETRY_QUEUE_SIZE

        return {
            "embedding_api_healthy": api_healthy,
            "retry_queue_depth": queue_depth,
            "retry_queue_capacity": round(queue_depth / _MAX_RETRY_QUEUE_SIZE, 2),
            "circuit_breaker_state": breaker_state.value,
        }


__all__ = ["VectorMemory", "_serialize_f32"]
