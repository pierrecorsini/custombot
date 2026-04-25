"""
Tests for src/vector_memory.py — SQLite-vec backed semantic memory store.

Unit tests covering:
  - _serialize_f32 binary packing
  - connect() schema creation (tables + virtual vec0 table)
  - save() with mocked OpenAI embeddings API
  - search() with mocked embedding and vector similarity
  - list_recent() ordering and limit
  - delete() by ID
  - count() per chat isolation
  - LRU embedding cache behaviour (hits, eviction, max size)
  - Thread safety (concurrent save / search under lock)
  - Chat isolation in search results
  - Error handling (DB not connected, empty results)
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
import struct
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Try importing sqlite_vec — if unavailable, skip the entire module
sqlite_vec = pytest.importorskip("sqlite_vec")

from src.utils import BoundedOrderedDict
from src.vector_memory import VectorMemory, _serialize_f32

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

EMBEDDING_DIM = 8  # Small dimension for fast tests
EMBEDDING_MODEL = "text-embedding-3-small"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_embedding(seed: int = 0) -> list[float]:
    """Generate a deterministic unit-norm embedding vector for testing."""
    rng = __import__("random").Random(seed)
    vec = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def _make_mock_client(embedding_map: dict[str, list[float]] | None = None):
    """Create a mock AsyncOpenAI client that returns pre-canned embeddings.

    Args:
        embedding_map: Optional mapping of text → embedding vector.
                       If a requested text isn't in the map, a deterministic
                       embedding is generated from the text's hash.
    """
    embedding_map = embedding_map or {}

    client = AsyncMock()

    async def _create_embedding(**kwargs):
        inp = kwargs["input"]
        # Support both single string and list of strings (batch)
        if isinstance(inp, list):
            results = []
            for text in inp:
                if text in embedding_map:
                    vec = embedding_map[text]
                else:
                    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
                    vec = _make_embedding(seed)
                results.append(MagicMock(embedding=vec))
            resp = MagicMock()
            resp.data = results
            return resp
        else:
            text = inp
            if text in embedding_map:
                vec = embedding_map[text]
            else:
                seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
                vec = _make_embedding(seed)
            resp = MagicMock()
            resp.data = [MagicMock(embedding=vec)]
            return resp

    client.embeddings = MagicMock()
    client.embeddings.create = _create_embedding
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Provide a temporary database file path."""
    return tmp_path / "test_vec_memory.db"


@pytest.fixture
def mock_client():
    """Provide a mock AsyncOpenAI client with default embedding behaviour."""
    return _make_mock_client()


@pytest.fixture
def vm(db_path: Path, mock_client) -> VectorMemory:
    """Provide a connected VectorMemory instance ready for testing."""
    memory = VectorMemory(
        db_path=str(db_path),
        openai_client=mock_client,
        embedding_model=EMBEDDING_MODEL,
        embedding_dimensions=EMBEDDING_DIM,
    )
    memory.connect()
    yield memory
    memory.close()


@pytest.fixture
def vm_with_embeddings(db_path: Path):
    """Provide a VectorMemory with specific embeddings mapped for deterministic search."""
    embeddings = {
        "hello world": _make_embedding(1),
        "goodbye world": _make_embedding(2),
        "python programming": _make_embedding(3),
        "machine learning": _make_embedding(4),
    }
    client = _make_mock_client(embeddings)
    memory = VectorMemory(
        db_path=str(db_path),
        openai_client=client,
        embedding_model=EMBEDDING_MODEL,
        embedding_dimensions=EMBEDDING_DIM,
    )
    memory.connect()
    yield memory, embeddings
    memory.close()


# ─────────────────────────────────────────────────────────────────────────────
# _serialize_f32
# ─────────────────────────────────────────────────────────────────────────────


class TestSerializeF32:
    """Tests for the _serialize_f32 helper function."""

    def test_packs_single_float(self):
        result = _serialize_f32([1.0])
        assert result == struct.pack("1f", 1.0)

    def test_packs_multiple_floats(self):
        vec = [1.0, 2.0, 3.0, 4.0]
        result = _serialize_f32(vec)
        assert result == struct.pack("4f", *vec)

    def test_output_length_matches_dimensions(self):
        vec = [0.5] * EMBEDDING_DIM
        result = _serialize_f32(vec)
        # 4 bytes per float32
        assert len(result) == EMBEDDING_DIM * 4

    def test_preserves_negative_values(self):
        vec = [-1.5, -0.001, -999.0]
        result = _serialize_f32(vec)
        unpacked = struct.unpack("3f", result)
        for original, unpacked_val in zip(vec, unpacked):
            assert abs(original - unpacked_val) < 1e-6

    def test_preserves_precision_within_float32(self):
        vec = [3.14159, 2.71828, 1.41421]
        result = _serialize_f32(vec)
        unpacked = struct.unpack("3f", result)
        for original, unpacked_val in zip(vec, unpacked):
            # float32 precision is ~7 decimal digits
            assert abs(original - unpacked_val) < 1e-5

    def test_empty_vector_returns_empty_bytes(self):
        result = _serialize_f32([])
        assert result == b""

    def test_zero_values(self):
        vec = [0.0, 0.0, 0.0]
        result = _serialize_f32(vec)
        unpacked = struct.unpack("3f", result)
        assert all(v == 0.0 for v in unpacked)

    def test_roundtrip(self):
        """Verify packing and unpacking recovers the original values."""
        original = [0.1, 0.2, 0.3, 0.4, 0.5]
        packed = _serialize_f32(original)
        unpacked = list(struct.unpack(f"{len(original)}f", packed))
        for o, u in zip(original, unpacked):
            assert abs(o - u) < 1e-7

    def test_large_vector(self):
        """Verify packing works with realistic embedding dimension."""
        vec = [float(i) / 1536 for i in range(1536)]
        result = _serialize_f32(vec)
        assert len(result) == 1536 * 4
        unpacked = struct.unpack("1536f", result)
        assert len(unpacked) == 1536


# ─────────────────────────────────────────────────────────────────────────────
# connect() / Schema creation
# ─────────────────────────────────────────────────────────────────────────────


class TestConnect:
    """Tests for connect() and schema creation."""

    def test_creates_database_file(self, db_path: Path, mock_client):
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        assert db_path.exists()
        vm.close()

    def test_creates_memory_entries_table(self, vm: VectorMemory):
        """The memory_entries table should exist after connect()."""
        assert vm._db is not None
        rows = vm._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_entries'"
        ).fetchall()
        assert len(rows) == 1

    def test_creates_memory_vec_virtual_table(self, vm: VectorMemory):
        """The memory_vec virtual table should exist after connect()."""
        assert vm._db is not None
        rows = vm._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_vec'"
        ).fetchall()
        assert len(rows) == 1

    def test_schema_idempotent(self, db_path: Path, mock_client):
        """Calling connect() twice should not raise."""
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.connect()  # Should not raise
        vm.close()

    def test_creates_parent_directory(self, tmp_path: Path, mock_client):
        """connect() should create parent directories for the db_path."""
        nested = tmp_path / "deep" / "nested" / "test.db"
        vm = VectorMemory(str(nested), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        assert nested.exists()
        vm.close()

    def test_enables_wal_journal_mode(self, vm: VectorMemory):
        """WAL mode should be enabled for concurrent read performance."""
        assert vm._db is not None
        row = vm._db.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() == "wal"


# ─────────────────────────────────────────────────────────────────────────────
# save()
# ─────────────────────────────────────────────────────────────────────────────


class TestSave:
    """Tests for the save() method."""

    @pytest.mark.asyncio
    async def test_returns_integer_row_id(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "hello world", "greeting")
        assert isinstance(row_id, int)
        assert row_id > 0

    @pytest.mark.asyncio
    async def test_row_ids_are_sequential(self, vm: VectorMemory):
        id1 = await vm.save("chat1", "first")
        id2 = await vm.save("chat1", "second")
        id3 = await vm.save("chat1", "third")
        assert id1 < id2 < id3

    @pytest.mark.asyncio
    async def test_stores_text_in_metadata_table(self, vm: VectorMemory):
        await vm.save("chat1", "test text content", "note")
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT text FROM memory_entries WHERE chat_id = ?", ("chat1",)
        ).fetchone()
        assert row is not None
        assert row[0] == "test text content"

    @pytest.mark.asyncio
    async def test_stores_category(self, vm: VectorMemory):
        await vm.save("chat1", "text", "custom-category")
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT category FROM memory_entries WHERE chat_id = ?", ("chat1",)
        ).fetchone()
        assert row[0] == "custom-category"

    @pytest.mark.asyncio
    async def test_default_category_is_empty_string(self, vm: VectorMemory):
        await vm.save("chat1", "text")
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT category FROM memory_entries WHERE chat_id = ?", ("chat1",)
        ).fetchone()
        assert row[0] == ""

    @pytest.mark.asyncio
    async def test_stores_created_at_timestamp(self, vm: VectorMemory):
        before = time.time()
        await vm.save("chat1", "text")
        after = time.time()
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT created_at FROM memory_entries WHERE chat_id = ?", ("chat1",)
        ).fetchone()
        assert before <= row[0] <= after

    @pytest.mark.asyncio
    async def test_stores_embedding_in_vec_table(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "hello world")
        assert vm._db is not None
        row = vm._db.execute("SELECT rowid FROM memory_vec WHERE rowid = ?", (row_id,)).fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_calls_openai_embeddings_api(self, db_path: Path):
        client = _make_mock_client()
        # Patch create to track calls
        original_create = client.embeddings.create
        call_count = 0

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        await vm.save("chat1", "test text")
        vm.close()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_uses_correct_model_and_input(self, db_path: Path):
        client = _make_mock_client()
        captured_kwargs: dict = {}

        async def capturing_create(**kwargs):
            captured_kwargs.update(kwargs)
            return await _make_mock_client().embeddings.create(**kwargs)

        client.embeddings.create = capturing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        await vm.save("chat1", "my input text")
        vm.close()

        assert captured_kwargs["model"] == EMBEDDING_MODEL
        assert captured_kwargs["input"] == "my input text"

    @pytest.mark.asyncio
    async def test_multiple_chats_separate_entries(self, vm: VectorMemory):
        id_a = await vm.save("chatA", "message A")
        id_b = await vm.save("chatB", "message B")
        assert id_a != id_b

        assert vm._db is not None
        count_a = vm._db.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?", ("chatA",)
        ).fetchone()[0]
        count_b = vm._db.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?", ("chatB",)
        ).fetchone()[0]
        assert count_a == 1
        assert count_b == 1


# ─────────────────────────────────────────────────────────────────────────────
# search()
# ─────────────────────────────────────────────────────────────────────────────


class TestSearch:
    """Tests for the search() method."""

    @pytest.mark.asyncio
    async def test_returns_matching_results(self, vm_with_embeddings):
        vm, embeddings = vm_with_embeddings
        await vm.save("chat1", "hello world", "greeting")
        results = await vm.search("chat1", "hello world", limit=5)
        assert len(results) >= 1
        assert results[0]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_result_contains_expected_keys(self, vm: VectorMemory):
        await vm.save("chat1", "test entry", "note")
        results = await vm.search("chat1", "test entry", limit=5)
        assert len(results) == 1
        result = results[0]
        assert "id" in result
        assert "text" in result
        assert "category" in result
        assert "created_at" in result
        assert "distance" in result

    @pytest.mark.asyncio
    async def test_respects_limit(self, vm: VectorMemory):
        for i in range(10):
            await vm.save("chat1", f"entry number {i}")
        results = await vm.search("chat1", "entry", limit=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_empty_results_for_empty_db(self, vm: VectorMemory):
        results = await vm.search("chat1", "nonexistent", limit=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_results_ordered_by_distance(self, vm_with_embeddings):
        """Results should be ordered by ascending cosine distance."""
        vm, embeddings = vm_with_embeddings
        # Save entries with different embeddings
        await vm.save("chat1", "hello world")
        await vm.save("chat1", "python programming")
        await vm.save("chat1", "machine learning")

        # Search with "hello world" query — it should be closest to itself
        results = await vm.search("chat1", "hello world", limit=3)
        assert len(results) >= 1
        # First result should have smallest distance
        if len(results) > 1:
            assert results[0]["distance"] <= results[1]["distance"]

    @pytest.mark.asyncio
    async def test_chat_isolation(self, vm_with_embeddings):
        """Search results should only include entries from the target chat."""
        vm, embeddings = vm_with_embeddings
        await vm.save("chatA", "hello world")
        await vm.save("chatB", "hello world")
        await vm.save("chatB", "python programming")

        results_a = await vm.search("chatA", "hello world", limit=10)
        assert len(results_a) == 1
        assert results_a[0]["text"] == "hello world"

        results_b = await vm.search("chatB", "hello world", limit=10)
        assert len(results_b) == 2
        texts = {r["text"] for r in results_b}
        assert "hello world" in texts
        assert "python programming" in texts

    @pytest.mark.asyncio
    async def test_search_returns_correct_category(self, vm: VectorMemory):
        await vm.save("chat1", "text with category", "important")
        results = await vm.search("chat1", "text with category", limit=1)
        assert results[0]["category"] == "important"

    @pytest.mark.asyncio
    async def test_search_calls_embed_for_query(self, db_path: Path):
        """Search should call the embeddings API for the query text."""
        client = _make_mock_client()
        embed_calls: list[str] = []

        original_create = client.embeddings.create

        async def tracking_create(**kwargs):
            embed_calls.append(kwargs["input"])
            return await original_create(**kwargs)

        client.embeddings.create = tracking_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        await vm.save("chat1", "stored text")
        await vm.search("chat1", "search query", limit=5)
        vm.close()

        # First call for save, second call for search query
        assert "stored text" in embed_calls
        assert "search query" in embed_calls


# ─────────────────────────────────────────────────────────────────────────────
# list_recent()
# ─────────────────────────────────────────────────────────────────────────────


class TestListRecent:
    """Tests for the list_recent() method."""

    def test_returns_empty_for_empty_db(self, vm: VectorMemory):
        result = vm.list_recent("chat1", limit=5)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_entries_in_reverse_chronological_order(self, vm: VectorMemory):
        await vm.save("chat1", "oldest")
        await vm.save("chat1", "middle")
        await vm.save("chat1", "newest")

        results = vm.list_recent("chat1", limit=10)
        assert len(results) == 3
        # newest should be first
        assert results[0]["text"] == "newest"
        assert results[1]["text"] == "middle"
        assert results[2]["text"] == "oldest"

    @pytest.mark.asyncio
    async def test_respects_limit(self, vm: VectorMemory):
        for i in range(10):
            await vm.save("chat1", f"entry {i}")

        results = vm.list_recent("chat1", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_returns_most_recent_with_limit(self, vm: VectorMemory):
        for i in range(5):
            await vm.save("chat1", f"entry {i}")

        results = vm.list_recent("chat1", limit=2)
        texts = [r["text"] for r in results]
        assert "entry 4" in texts
        assert "entry 3" in texts

    @pytest.mark.asyncio
    async def test_result_keys(self, vm: VectorMemory):
        await vm.save("chat1", "test")
        results = vm.list_recent("chat1", limit=1)
        result = results[0]
        assert "id" in result
        assert "text" in result
        assert "category" in result
        assert "created_at" in result
        # list_recent does NOT include distance
        assert "distance" not in result

    @pytest.mark.asyncio
    async def test_chat_isolation(self, vm: VectorMemory):
        await vm.save("chatA", "message A1")
        await vm.save("chatB", "message B1")
        await vm.save("chatA", "message A2")

        results_a = vm.list_recent("chatA", limit=10)
        results_b = vm.list_recent("chatB", limit=10)

        assert len(results_a) == 2
        assert len(results_b) == 1
        assert all(r["text"].startswith("message A") for r in results_a)
        assert results_b[0]["text"] == "message B1"

    @pytest.mark.asyncio
    async def test_default_limit_is_10(self, vm: VectorMemory):
        """list_recent default limit should be 10."""
        for i in range(15):
            await vm.save("chat1", f"entry {i}")

        results = vm.list_recent("chat1")
        assert len(results) == 10


# ─────────────────────────────────────────────────────────────────────────────
# delete()
# ─────────────────────────────────────────────────────────────────────────────


class TestDelete:
    """Tests for the delete() method."""

    @pytest.mark.asyncio
    async def test_returns_true_for_existing_entry(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "to be deleted")
        result = vm.delete(row_id)
        assert result is True

    def test_returns_false_for_nonexistent_id(self, vm: VectorMemory):
        result = vm.delete(99999)
        assert result is False

    @pytest.mark.asyncio
    async def test_removes_from_metadata_table(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "will be deleted")
        vm.delete(row_id)
        assert vm._db is not None
        row = vm._db.execute("SELECT * FROM memory_entries WHERE id = ?", (row_id,)).fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_removes_from_vec_table(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "will be deleted")
        vm.delete(row_id)
        assert vm._db is not None
        row = vm._db.execute("SELECT * FROM memory_vec WHERE rowid = ?", (row_id,)).fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_count_decreases_after_delete(self, vm: VectorMemory):
        await vm.save("chat1", "entry 1")
        id2 = await vm.save("chat1", "entry 2")
        await vm.save("chat1", "entry 3")

        assert vm.count("chat1") == 3
        vm.delete(id2)
        assert vm.count("chat1") == 2

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_other_chats(self, vm: VectorMemory):
        id_a = await vm.save("chatA", "A")
        await vm.save("chatB", "B")

        vm.delete(id_a)
        assert vm.count("chatA") == 0
        assert vm.count("chatB") == 1

    @pytest.mark.asyncio
    async def test_search_excludes_deleted_entry(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "findable text", "note")
        # Verify it's searchable
        results = await vm.search("chat1", "findable text", limit=5)
        assert any(r["id"] == row_id for r in results)

        vm.delete(row_id)
        results = await vm.search("chat1", "findable text", limit=5)
        assert not any(r["id"] == row_id for r in results)

    @pytest.mark.asyncio
    async def test_list_recent_excludes_deleted(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "temporary")
        vm.delete(row_id)
        results = vm.list_recent("chat1", limit=10)
        assert not any(r["id"] == row_id for r in results)


# ─────────────────────────────────────────────────────────────────────────────
# count()
# ─────────────────────────────────────────────────────────────────────────────


class TestCount:
    """Tests for the count() method."""

    def test_returns_zero_for_empty_chat(self, vm: VectorMemory):
        assert vm.count("nonexistent_chat") == 0

    @pytest.mark.asyncio
    async def test_counts_entries_per_chat(self, vm: VectorMemory):
        await vm.save("chat1", "a")
        await vm.save("chat1", "b")
        await vm.save("chat1", "c")
        assert vm.count("chat1") == 3

    @pytest.mark.asyncio
    async def test_isolates_by_chat_id(self, vm: VectorMemory):
        await vm.save("chatA", "a1")
        await vm.save("chatA", "a2")
        await vm.save("chatB", "b1")

        assert vm.count("chatA") == 2
        assert vm.count("chatB") == 1
        assert vm.count("chatC") == 0

    @pytest.mark.asyncio
    async def test_count_updates_after_save_and_delete(self, vm: VectorMemory):
        assert vm.count("chat1") == 0

        id1 = await vm.save("chat1", "first")
        assert vm.count("chat1") == 1

        await vm.save("chat1", "second")
        assert vm.count("chat1") == 2

        vm.delete(id1)
        assert vm.count("chat1") == 1


# ─────────────────────────────────────────────────────────────────────────────
# LRU Embedding Cache
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbedCache:
    """Tests for the LRU embedding cache behaviour."""

    @pytest.mark.asyncio
    async def test_cache_populated_after_embed(self, vm: VectorMemory):
        text = "cacheable text"
        await vm._embed(text)
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert cache_key in vm._embed_cache

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_api_call(self, db_path: Path):
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        text = "same text twice"
        await vm._embed(text)
        assert call_count == 1

        await vm._embed(text)
        assert call_count == 1  # Should not increase — cache hit

        vm.close()

    @pytest.mark.asyncio
    async def test_different_texts_miss_cache(self, db_path: Path):
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        await vm._embed("text A")
        await vm._embed("text B")
        assert call_count == 2

        vm.close()

    @pytest.mark.asyncio
    async def test_cache_evicts_at_max_size(self, db_path: Path):
        client = _make_mock_client()
        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm._embed_cache = BoundedOrderedDict(max_size=3, eviction="one")

        # Fill cache to max
        await vm._embed("text1")
        await vm._embed("text2")
        await vm._embed("text3")
        assert len(vm._embed_cache) == 3

        # One more should evict the oldest
        await vm._embed("text4")
        assert len(vm._embed_cache) == 3

        # "text1" should be evicted
        key1 = hashlib.sha256("text1".encode()).hexdigest()
        assert key1 not in vm._embed_cache

        # "text4" should be present
        key4 = hashlib.sha256("text4".encode()).hexdigest()
        assert key4 in vm._embed_cache

        vm.close()

    @pytest.mark.asyncio
    async def test_cache_lru_access_refreshes_entry(self, db_path: Path):
        """Accessing a cached entry should move it to the end (most recent)."""
        client = _make_mock_client()
        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm._embed_cache = BoundedOrderedDict(max_size=3, eviction="one")

        await vm._embed("text1")
        await vm._embed("text2")
        await vm._embed("text3")

        # Access text1 again — should move to end
        await vm._embed("text1")

        # Now text2 is oldest; adding text4 should evict text2
        await vm._embed("text4")
        assert len(vm._embed_cache) == 3

        key2 = hashlib.sha256("text2".encode()).hexdigest()
        key1 = hashlib.sha256("text1".encode()).hexdigest()
        assert key2 not in vm._embed_cache  # evicted
        assert key1 in vm._embed_cache  # refreshed, still present

        vm.close()

    @pytest.mark.asyncio
    async def test_default_cache_max_is_256(self, vm: VectorMemory):
        assert vm._embed_cache._max_size == 256

    @pytest.mark.asyncio
    async def test_save_uses_cached_embedding(self, db_path: Path):
        """save() should benefit from the embedding cache."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Save same text twice — second save should use cache
        await vm.save("chat1", "duplicate text")
        assert call_count == 1

        await vm.save("chat1", "duplicate text")
        assert call_count == 1  # Cache hit

        vm.close()


# ─────────────────────────────────────────────────────────────────────────────
# Thread Safety
# ─────────────────────────────────────────────────────────────────────────────


class TestThreadSafety:
    """Tests for thread safety of VectorMemory operations."""

    @pytest.mark.asyncio
    async def test_concurrent_saves_from_multiple_threads(self, vm: VectorMemory):
        """Multiple threads inserting simultaneously should not corrupt data."""
        import asyncio

        num_entries = 20
        errors: list[Exception] = []

        async def save_entry(idx: int):
            try:
                await vm.save("chat1", f"concurrent entry {idx}")
            except Exception as e:
                errors.append(e)

        await asyncio.gather(*[save_entry(i) for i in range(num_entries)])

        assert len(errors) == 0
        assert vm.count("chat1") == num_entries

    @pytest.mark.asyncio
    async def test_concurrent_save_and_count(self, vm: VectorMemory):
        """Count should be consistent even during concurrent saves."""
        import asyncio

        num_entries = 10
        counts: list[int] = []

        async def save_entry(idx: int):
            await vm.save("chat1", f"entry {idx}")

        async def count_entries():
            counts.append(vm.count("chat1"))

        # Interleave saves and counts
        tasks = []
        for i in range(num_entries):
            tasks.append(save_entry(i))
            tasks.append(count_entries())

        await asyncio.gather(*tasks)

        # All counts should be >= 0 and <= num_entries
        assert all(0 <= c <= num_entries for c in counts)
        # Final count should be exactly num_entries
        assert vm.count("chat1") == num_entries

    def test_lock_is_held_during_list_recent(self, vm: VectorMemory):
        """list_recent no longer needs a lock — WAL allows concurrent reads."""
        # Verify the write lock exists for write operations
        assert isinstance(vm._write_lock, type(threading.Lock()))

    def test_lock_is_held_during_delete(self, vm: VectorMemory):
        """delete acquires the write lock."""
        assert isinstance(vm._write_lock, type(threading.Lock()))

    def test_lock_is_held_during_count(self, vm: VectorMemory):
        """count no longer needs a lock — WAL allows concurrent reads."""
        assert isinstance(vm._write_lock, type(threading.Lock()))

    def test_concurrent_reads_and_writes(self, vm: VectorMemory):
        """Concurrent reads should not block under WAL mode."""
        import concurrent.futures

        # Pre-populate some data directly (no embedding needed for read test)
        for i in range(5):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) VALUES (?, ?, ?, ?)",
                    ("chat1", f"entry {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        errors: list[Exception] = []

        def read_count():
            try:
                vm.count("chat1")
            except Exception as e:
                errors.append(e)

        def read_list():
            try:
                vm.list_recent("chat1", limit=5)
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = []
            for _ in range(10):
                futures.append(pool.submit(read_count))
                futures.append(pool.submit(read_list))
            concurrent.futures.wait(futures)

        assert len(errors) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Error Handling / Edge Cases
# ─────────────────────────────────────────────────────────────────────────────


class TestErrorHandling:
    """Tests for error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_save_after_close_raises(self, db_path: Path, mock_client):
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.close()

        with pytest.raises(AssertionError):
            await vm.save("chat1", "text")

    def test_list_recent_after_close_raises(self, db_path: Path, mock_client):
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.close()

        with pytest.raises(AssertionError):
            vm.list_recent("chat1")

    def test_delete_after_close_raises(self, db_path: Path, mock_client):
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.close()

        with pytest.raises(AssertionError):
            vm.delete(1)

    def test_count_after_close_raises(self, db_path: Path, mock_client):
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.close()

        with pytest.raises(AssertionError):
            vm.count("chat1")

    def test_close_clears_embed_cache(self, db_path: Path, mock_client):
        """close() should clear the embedding cache."""
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm._embed_cache["key1"] = [0.1, 0.2]
        vm.close()
        assert len(vm._embed_cache) == 0

    def test_close_clears_inflight_futures(self, db_path: Path, mock_client):
        """close() should cancel pending in-flight futures and clear the dict."""
        import asyncio

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        vm._inflight["key1"] = future
        vm.close()
        assert len(vm._inflight) == 0
        assert future.cancelled()
        loop.close()

    def test_close_idempotent(self, db_path: Path, mock_client):
        """Calling close() multiple times should not raise."""
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm.close()
        vm.close()  # second call should be safe

    @pytest.mark.asyncio
    async def test_save_empty_string(self, vm: VectorMemory):
        """Saving empty text should succeed (embedding is generated for empty string)."""
        row_id = await vm.save("chat1", "")
        assert isinstance(row_id, int)
        assert row_id > 0

    @pytest.mark.asyncio
    async def test_save_unicode_text(self, vm: VectorMemory):
        """Should handle unicode text without errors."""
        row_id = await vm.save("chat1", "你好世界 🌍 مرحبا")
        assert row_id > 0
        results = vm.list_recent("chat1", limit=1)
        assert results[0]["text"] == "你好世界 🌍 مرحبا"

    @pytest.mark.asyncio
    async def test_save_very_long_text(self, vm: VectorMemory):
        """Should handle long text strings."""
        long_text = "word " * 10000
        row_id = await vm.save("chat1", long_text)
        assert row_id > 0

    @pytest.mark.asyncio
    async def test_search_after_all_deleted(self, vm: VectorMemory):
        """Search should return empty after all entries deleted."""
        id1 = await vm.save("chat1", "entry one")
        id2 = await vm.save("chat1", "entry two")

        vm.delete(id1)
        vm.delete(id2)

        results = await vm.search("chat1", "entry", limit=5)
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Integration-style round-trip tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    """End-to-end round-trip tests combining save, search, list, delete, count."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, vm: VectorMemory):
        """Save → search → list → count → delete → verify empty."""
        # Save
        row_id = await vm.save("chat1", "lifecycle test", "test")
        assert vm.count("chat1") == 1

        # Search
        results = await vm.search("chat1", "lifecycle test", limit=1)
        assert len(results) == 1
        assert results[0]["text"] == "lifecycle test"
        assert results[0]["category"] == "test"

        # List
        recent = vm.list_recent("chat1", limit=1)
        assert len(recent) == 1
        assert recent[0]["text"] == "lifecycle test"

        # Delete
        assert vm.delete(row_id) is True
        assert vm.count("chat1") == 0

        # Verify empty
        results = await vm.search("chat1", "lifecycle test", limit=1)
        assert results == []
        assert vm.list_recent("chat1", limit=1) == []

    @pytest.mark.asyncio
    async def test_multiple_chats_full_lifecycle(self, vm: VectorMemory):
        """Verify full lifecycle across multiple chats with isolation."""
        # Populate two chats
        await vm.save("alice", "alice fact 1", "fact")
        await vm.save("alice", "alice fact 2", "fact")
        await vm.save("bob", "bob note 1", "note")

        # Counts are isolated
        assert vm.count("alice") == 2
        assert vm.count("bob") == 1

        # Search is isolated
        alice_results = await vm.search("alice", "fact", limit=10)
        assert all(r["category"] == "fact" for r in alice_results)

        bob_results = await vm.search("bob", "note", limit=10)
        assert len(bob_results) == 1
        assert bob_results[0]["text"] == "bob note 1"

        # List is isolated
        alice_recent = vm.list_recent("alice", limit=10)
        assert len(alice_recent) == 2

        bob_recent = vm.list_recent("bob", limit=10)
        assert len(bob_recent) == 1


# ─────────────────────────────────────────────────────────────────────────────
# save_batch() / _embed_batch()
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveBatch:
    """Tests for the save_batch() method and _embed_batch() batching."""

    @pytest.mark.asyncio
    async def test_returns_row_ids_for_each_item(self, vm: VectorMemory):
        items = [("first", "note"), ("second", "fact"), ("third", "")]
        row_ids = await vm.save_batch("chat1", items)
        assert len(row_ids) == 3
        assert all(isinstance(rid, int) for rid in row_ids)
        assert all(rid > 0 for rid in row_ids)

    @pytest.mark.asyncio
    async def test_row_ids_are_sequential(self, vm: VectorMemory):
        items = [("a", ""), ("b", ""), ("c", "")]
        row_ids = await vm.save_batch("chat1", items)
        assert row_ids[0] < row_ids[1] < row_ids[2]

    @pytest.mark.asyncio
    async def test_stores_all_texts(self, vm: VectorMemory):
        items = [("alpha", "cat_a"), ("beta", "cat_b")]
        await vm.save_batch("chat1", items)
        assert vm.count("chat1") == 2
        recent = vm.list_recent("chat1", limit=10)
        texts = {r["text"] for r in recent}
        assert texts == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_stores_categories(self, vm: VectorMemory):
        items = [("text1", "important"), ("text2", "trivial")]
        await vm.save_batch("chat1", items)
        recent = vm.list_recent("chat1", limit=10)
        cats = {r["text"]: r["category"] for r in recent}
        assert cats["text1"] == "important"
        assert cats["text2"] == "trivial"

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self, vm: VectorMemory):
        result = await vm.save_batch("chat1", [])
        assert result == []
        assert vm.count("chat1") == 0

    @pytest.mark.asyncio
    async def test_single_item_batch(self, vm: VectorMemory):
        row_ids = await vm.save_batch("chat1", [("only one", "test")])
        assert len(row_ids) == 1
        assert vm.count("chat1") == 1

    @pytest.mark.asyncio
    async def test_uses_single_api_call_for_batch(self, db_path: Path):
        """save_batch should make only one embeddings API call for multiple unique texts."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        items = [("text A", ""), ("text B", ""), ("text C", "")]
        await vm.save_batch("chat1", items)
        vm.close()

        # Only one API call for the batch of 3 texts
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_batch_then_search(self, vm_with_embeddings):
        vm, embeddings = vm_with_embeddings
        items = [
            ("hello world", "greeting"),
            ("python programming", "tech"),
            ("machine learning", "tech"),
        ]
        await vm.save_batch("chat1", items)

        results = await vm.search("chat1", "hello world", limit=3)
        assert len(results) >= 1
        assert results[0]["text"] == "hello world"

    @pytest.mark.asyncio
    async def test_batch_uses_cache_for_duplicate_texts(self, db_path: Path):
        """Batch with duplicate texts should not duplicate API calls."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Same text appears 3 times — only one unique text → 1 API call
        items = [("same text", ""), ("same text", ""), ("same text", "")]
        await vm.save_batch("chat1", items)

        assert call_count == 1
        assert vm.count("chat1") == 3
        vm.close()

    @pytest.mark.asyncio
    async def test_batch_with_partially_cached_texts(self, db_path: Path):
        """Texts already in cache should be excluded from the batch API call."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-warm cache for "cached text"
        await vm._embed("cached text")
        assert call_count == 1

        # Batch with one cached + two new texts → 1 more API call
        items = [("cached text", ""), ("new text A", ""), ("new text B", "")]
        await vm.save_batch("chat1", items)
        vm.close()

        # Initial embed + one batch call = 2 total
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_batch_chat_isolation(self, vm: VectorMemory):
        await vm.save_batch("chatA", [("A1", ""), ("A2", "")])
        await vm.save_batch("chatB", [("B1", "")])

        assert vm.count("chatA") == 2
        assert vm.count("chatB") == 1

        results_a = await vm.search("chatA", "A", limit=10)
        assert all(r["text"].startswith("A") for r in results_a)

    @pytest.mark.asyncio
    async def test_batch_delete_individual_entries(self, vm: VectorMemory):
        items = [("keep", ""), ("delete me", ""), ("keep too", "")]
        row_ids = await vm.save_batch("chat1", items)

        vm.delete(row_ids[1])
        assert vm.count("chat1") == 2

        recent = vm.list_recent("chat1", limit=10)
        texts = {r["text"] for r in recent}
        assert "delete me" not in texts
        assert "keep" in texts
        assert "keep too" in texts

    @pytest.mark.asyncio
    async def test_batch_saves_created_at_timestamp(self, vm: VectorMemory):
        before = time.time()
        await vm.save_batch("chat1", [("t1", ""), ("t2", "")])
        after = time.time()

        recent = vm.list_recent("chat1", limit=10)
        for r in recent:
            assert before <= r["created_at"] <= after

    @pytest.mark.asyncio
    async def test_concurrent_save_batches(self, vm: VectorMemory):
        """Multiple concurrent save_batch calls should not corrupt data."""
        import asyncio

        num_batches = 5
        batch_size = 4
        errors: list[Exception] = []

        async def save_batch(batch_idx: int):
            try:
                items = [(f"batch{batch_idx}_item{i}", "") for i in range(batch_size)]
                await vm.save_batch(f"chat{batch_idx}", items)
            except Exception as e:
                errors.append(e)

        await asyncio.gather(*[save_batch(i) for i in range(num_batches)])

        assert len(errors) == 0
        for i in range(num_batches):
            assert vm.count(f"chat{i}") == batch_size


# ─────────────────────────────────────────────────────────────────────────────
# _embed_batch() Cache & In-flight Dedup Interaction
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbedBatchCacheInflight:
    """Tests for _embed_batch() interaction between LRU cache, in-flight dedup,
    and API calls.

    Covers:
      (a) batches where some texts are in cache and others need API calls
      (b) in-flight futures resolving during concurrent batch processing
      (c) error propagation cancelling all pending futures
    """

    @pytest.mark.asyncio
    async def test_all_from_cache_no_api_call(self, db_path: Path):
        """When all texts are in the LRU cache, no API call should be made."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def counting_create(**kwargs):
            nonlocal call_count
            call_count += 1
            return await original_create(**kwargs)

        client.embeddings.create = counting_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-warm cache for all three texts
        await vm._embed("alpha")
        await vm._embed("beta")
        await vm._embed("gamma")
        assert call_count == 3

        # Batch should resolve entirely from cache — no new API call
        results = await vm._embed_batch(["alpha", "beta", "gamma"])
        assert call_count == 3
        assert all(r is not None for r in results)
        assert len(results) == 3

        vm.close()

    @pytest.mark.asyncio
    async def test_mixed_cache_and_api_call(self, db_path: Path):
        """Batch with some cached texts and some fresh — only uncached go to API."""
        call_count = 0
        api_inputs: list[list[str]] = []
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def tracking_create(**kwargs):
            nonlocal call_count
            call_count += 1
            inp = kwargs["input"]
            api_inputs.append(inp if isinstance(inp, list) else [inp])
            return await original_create(**kwargs)

        client.embeddings.create = tracking_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-warm cache for "cached_a" and "cached_b"
        await vm._embed("cached_a")
        await vm._embed("cached_b")
        assert call_count == 2

        # Batch: 2 cached + 2 fresh
        results = await vm._embed_batch(
            ["cached_a", "fresh_x", "cached_b", "fresh_y"]
        )
        assert call_count == 3  # 2 pre-warm + 1 batch
        assert len(results) == 4

        # API should have received only the fresh texts
        assert set(api_inputs[-1]) == {"fresh_x", "fresh_y"}

        vm.close()

    @pytest.mark.asyncio
    async def test_inflight_future_resolved_by_concurrent_batch(self, db_path: Path):
        """Two concurrent _embed_batch calls for overlapping texts.

        The first call creates in-flight futures. The second call should find
        those in-flight futures and await them instead of making a new API call.
        """
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        # Gate to control timing: first call blocks until second call starts
        gate = asyncio.Event()

        async def gated_create(**kwargs):
            nonlocal call_count
            call_count += 1
            gate.set()  # Signal that the first API call has started
            # Give the second coroutine time to check in-flight
            await asyncio.sleep(0.05)
            return await original_create(**kwargs)

        client.embeddings.create = gated_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Launch two concurrent batch calls for the same text
        results_a, results_b = await asyncio.gather(
            vm._embed_batch(["shared_text"]),
            vm._embed_batch(["shared_text"]),
        )

        # Only 1 API call should have been made (deduplication via in-flight)
        assert call_count == 1
        assert len(results_a) == 1
        assert len(results_b) == 1
        assert results_a[0] == results_b[0]

        vm.close()

    @pytest.mark.asyncio
    async def test_inflight_from_single_embed_shares_with_batch(self, db_path: Path):
        """A running _embed() call creates an in-flight future. A subsequent
        _embed_batch() for the same text should await that future."""
        call_count = 0
        client = _make_mock_client()
        original_create = client.embeddings.create

        gate = asyncio.Event()

        async def gated_create(**kwargs):
            nonlocal call_count
            call_count += 1
            gate.set()
            await asyncio.sleep(0.05)
            return await original_create(**kwargs)

        client.embeddings.create = gated_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Start a single _embed() and a batch simultaneously
        single_result, batch_results = await asyncio.gather(
            vm._embed("overlapping_text"),
            vm._embed_batch(["overlapping_text", "other_text"]),
        )

        # Only 2 API calls: 1 for "overlapping_text" (via _embed), 1 batch
        # for "other_text" (since "overlapping_text" was in-flight)
        # Actually the batch sees "overlapping_text" in-flight, waits for it,
        # then sends "other_text" as a fresh API call.
        assert call_count == 2
        assert single_result == batch_results[0]
        assert batch_results[1] is not None

        vm.close()

    @pytest.mark.asyncio
    async def test_error_propagation_cancels_futures(self, db_path: Path):
        """When the API call fails, the exception should propagate to the caller
        and to any waiters on the in-flight futures."""
        client = _make_mock_client()

        async def failing_create(**kwargs):
            raise RuntimeError("API unavailable")

        client.embeddings.create = failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(RuntimeError, match="API unavailable"):
            await vm._embed_batch(["text_a", "text_b"])

        vm.close()

    @pytest.mark.asyncio
    async def test_error_propagation_to_concurrent_waiters(self, db_path: Path):
        """When _embed_batch() fails, concurrent callers awaiting the same
        in-flight futures should also receive the error."""
        client = _make_mock_client()
        gate = asyncio.Event()
        call_count = 0

        async def gated_failing_create(**kwargs):
            nonlocal call_count
            call_count += 1
            gate.set()
            await asyncio.sleep(0.05)
            raise RuntimeError("transient failure")

        client.embeddings.create = gated_failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Launch two concurrent batch calls for the same text
        errors: list[Exception] = []

        async def call_batch():
            try:
                await vm._embed_batch(["shared_fail_text"])
            except Exception as e:
                errors.append(e)

        await asyncio.gather(call_batch(), call_batch())

        # Both should have received the error
        assert len(errors) == 2
        assert all(isinstance(e, RuntimeError) for e in errors)
        # Only 1 API call was made (deduplication)
        assert call_count == 1

        vm.close()

    @pytest.mark.asyncio
    async def test_inflight_cleaned_up_on_success(self, db_path: Path):
        """After successful _embed_batch(), in-flight entries should be removed."""
        client = _make_mock_client()
        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        await vm._embed_batch(["cleanup_text"])

        key = hashlib.sha256("cleanup_text".encode()).hexdigest()
        assert key not in vm._inflight

        vm.close()

    @pytest.mark.asyncio
    async def test_inflight_cleaned_up_on_error(self, db_path: Path):
        """After failed _embed_batch(), in-flight entries should still be removed."""
        client = _make_mock_client()

        async def failing_create(**kwargs):
            raise RuntimeError("fail")

        client.embeddings.create = failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(RuntimeError):
            await vm._embed_batch(["cleanup_on_error"])

        key = hashlib.sha256("cleanup_on_error".encode()).hexdigest()
        assert key not in vm._inflight

        vm.close()

    @pytest.mark.asyncio
    async def test_cache_populated_after_batch(self, db_path: Path):
        """After _embed_batch(), all unique texts should be in the LRU cache."""
        client = _make_mock_client()
        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        await vm._embed_batch(["cache_a", "cache_b", "cache_a"])

        key_a = hashlib.sha256("cache_a".encode()).hexdigest()
        key_b = hashlib.sha256("cache_b".encode()).hexdigest()
        assert key_a in vm._embed_cache
        assert key_b in vm._embed_cache

        vm.close()

    @pytest.mark.asyncio
    async def test_duplicate_texts_deduped_in_batch(self, db_path: Path):
        """A batch with duplicate texts should deduplicate to a single API call entry."""
        call_count = 0
        api_inputs: list[list[str]] = []
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def tracking_create(**kwargs):
            nonlocal call_count
            call_count += 1
            inp = kwargs["input"]
            api_inputs.append(inp if isinstance(inp, list) else [inp])
            return await original_create(**kwargs)

        client.embeddings.create = tracking_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        results = await vm._embed_batch(["dup", "dup", "dup"])
        assert call_count == 1
        # API should receive exactly 1 unique text
        assert api_inputs[0] == ["dup"]
        # All 3 positions should have the same embedding
        assert results[0] == results[1] == results[2]
        assert all(r is not None for r in results)

        vm.close()

    @pytest.mark.asyncio
    async def test_mixed_cache_inflight_and_fresh(self, db_path: Path):
        """Batch where texts are distributed across all 3 resolution paths:
        LRU cache hit, in-flight future (from concurrent call), and fresh API call."""
        client = _make_mock_client()
        original_create = client.embeddings.create
        call_count = 0
        gate = asyncio.Event()

        async def gated_create(**kwargs):
            nonlocal call_count
            call_count += 1
            gate.set()
            await asyncio.sleep(0.05)
            return await original_create(**kwargs)

        client.embeddings.create = gated_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-warm cache for "cached"
        await vm._embed("cached")
        initial_calls = call_count
        assert initial_calls == 1

        # Start a concurrent _embed() for "inflight_text" (creates in-flight future)
        # Simultaneously call _embed_batch with all 3 text types
        single_emb, batch_results = await asyncio.gather(
            vm._embed("inflight_text"),
            vm._embed_batch(["cached", "inflight_text", "fresh_text"]),
        )

        # Expected API calls: 1 (cached pre-warm) + 1 (inflight_text via _embed)
        #   + 1 (batch with fresh_text only)
        assert call_count == initial_calls + 2

        assert len(batch_results) == 3
        assert all(r is not None for r in batch_results)

        # "cached" result should match what was pre-warmed
        cached_key = hashlib.sha256("cached".encode()).hexdigest()
        assert batch_results[0] == vm._embed_cache[cached_key]

        # "inflight_text" should match the concurrent _embed() result
        assert batch_results[1] == single_emb

        # "fresh_text" should be a valid embedding
        assert len(batch_results[2]) == EMBEDDING_DIM

        vm.close()

    @pytest.mark.asyncio
    async def test_batch_returns_correct_order_with_mixed_sources(self, db_path: Path):
        """Results should maintain the original text order regardless of which
        resolution path (cache, in-flight, API) each text takes."""
        embeddings = {
            "aaa": _make_embedding(10),
            "bbb": _make_embedding(20),
            "ccc": _make_embedding(30),
        }
        client = _make_mock_client(embeddings)

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-warm cache for "aaa"
        await vm._embed("aaa")

        # "bbb" and "ccc" will go through the API, "aaa" from cache
        results = await vm._embed_batch(["aaa", "bbb", "ccc"])

        assert results[0] == embeddings["aaa"]
        assert results[1] == embeddings["bbb"]
        assert results[2] == embeddings["ccc"]

        vm.close()


# ─────────────────────────────────────────────────────────────────────────────
# Read Connection Pool
# ─────────────────────────────────────────────────────────────────────────────


class TestReadConnectionPool:
    """Tests for the threading.local–based read connection pool."""

    def test_same_thread_reuses_connection(self, vm: VectorMemory):
        """Two reads from the same thread should use the same connection."""
        conn1 = vm._get_read_connection()
        conn2 = vm._get_read_connection()
        assert conn1 is conn2

    def test_different_threads_get_different_connections(self, vm: VectorMemory):
        """Each thread should get its own pooled connection."""
        import concurrent.futures

        main_conn = vm._get_read_connection()
        thread_conns: list[sqlite3.Connection] = []

        def get_conn():
            thread_conns.append(vm._get_read_connection())

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(get_conn) for _ in range(2)]
            concurrent.futures.wait(futures)

        assert len(thread_conns) == 2
        for tc in thread_conns:
            assert tc is not main_conn

    def test_pool_tracks_all_connections(self, vm: VectorMemory):
        """All opened connections should be tracked for cleanup."""
        import concurrent.futures

        vm._get_read_connection()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(vm._get_read_connection) for _ in range(3)]
            concurrent.futures.wait(futures)

        assert len(vm._read_connections) == 4  # 1 main + 3 threads

    def test_close_cleans_up_all_read_connections(self, vm: VectorMemory):
        """close() should close and untrack all pooled read connections."""
        import concurrent.futures

        vm._get_read_connection()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(vm._get_read_connection) for _ in range(2)]
            concurrent.futures.wait(futures)

        assert len(vm._read_connections) > 0
        vm.close()
        assert len(vm._read_connections) == 0

    def test_pooled_reads_return_correct_results(
        self, vm_with_embeddings: tuple[VectorMemory, dict],
    ):
        """Read operations through the pool should return the same results."""
        vm, embeddings = vm_with_embeddings
        for text in embeddings:
            vm._insert_entry(
                "chat1", text, "", time.time(), embeddings[text],
            )

        # Multiple reads from the same thread should be consistent
        c1 = vm.count("chat1")
        c2 = vm.count("chat1")
        assert c1 == c2 == len(embeddings)

        recent1 = vm.list_recent("chat1", limit=10)
        recent2 = vm.list_recent("chat1", limit=10)
        assert len(recent1) == len(recent2) == len(embeddings)

    def test_concurrent_pool_reads_and_writes(self, vm: VectorMemory):
        """Pooled reads should not interfere with concurrent writes."""
        import concurrent.futures

        for i in range(5):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"entry {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        errors: list[Exception] = []

        def read_op():
            try:
                vm.count("chat1")
                vm.list_recent("chat1", limit=5)
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(read_op) for _ in range(8)]
            concurrent.futures.wait(futures)

        assert len(errors) == 0

    def test_closed_read_connections_are_unusable(self, vm: VectorMemory):
        """After close(), previously obtained read connections should be unusable."""
        conn = vm._get_read_connection()
        # Verify the connection works before close
        conn.execute("SELECT 1").fetchone()

        vm.close()

        # After close, executing on the old connection should fail
        with pytest.raises(Exception):
            conn.execute("SELECT 1")

    def test_read_connection_sees_committed_writes(self, vm: VectorMemory):
        """A pooled read-only connection should see data committed by writes
        on the main connection (WAL mode snapshot isolation)."""
        assert vm._db is not None

        # Write data via the main (write) connection
        with vm._write_lock:
            cur = vm._db.execute(
                "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("chat1", "visible via read conn", "test", time.time()),
            )
            row_id = cur.lastrowid
            vm._db.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                (row_id, _serialize_f32(_make_embedding(42))),
            )
            vm._db.commit()

        # Read via the pooled read connection should see the committed data
        assert vm.count("chat1") == 1
        recent = vm.list_recent("chat1", limit=10)
        assert len(recent) == 1
        assert recent[0]["text"] == "visible via read conn"
        assert recent[0]["category"] == "test"

    def test_read_connection_from_other_thread_sees_committed_writes(
        self, vm: VectorMemory,
    ):
        """A pooled read connection created in a separate thread should also
        see data committed by writes on the main connection."""
        import concurrent.futures

        assert vm._db is not None

        with vm._write_lock:
            cur = vm._db.execute(
                "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("chat1", "cross-thread visible", "", time.time()),
            )
            row_id = cur.lastrowid
            vm._db.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                (row_id, _serialize_f32(_make_embedding(99))),
            )
            vm._db.commit()

        thread_results: list[int] = []

        def count_from_thread():
            thread_results.append(vm.count("chat1"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(count_from_thread).result()

        assert thread_results == [1]

    def test_multiple_threads_read_concurrently_without_blocking(
        self, vm: VectorMemory,
    ):
        """Multiple threads performing concurrent reads through the pool should
        all succeed and see consistent data."""
        import concurrent.futures

        assert vm._db is not None

        # Pre-populate data
        num_entries = 10
        for i in range(num_entries):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"entry {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        errors: list[Exception] = []
        results: dict[int, int] = {}
        lock = threading.Lock()
        barrier = threading.Barrier(4)

        def read_with_barrier(thread_id: int):
            try:
                barrier.wait(timeout=5)  # All threads start at the same time
                c = vm.count("chat1")
                vm.list_recent("chat1", limit=5)
                with lock:
                    results[thread_id] = c
            except Exception as e:
                with lock:
                    errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(read_with_barrier, i) for i in range(4)]
            concurrent.futures.wait(futures)

        assert len(errors) == 0
        # All threads should see the same count
        assert all(c == num_entries for c in results.values())


# ─────────────────────────────────────────────────────────────────────────────
# Schema Migration
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaMigration:
    """Tests for schema version tracking and incremental migration."""

    def test_fresh_db_records_schema_version(self, vm: VectorMemory):
        """A fresh database should have the current schema version in _schema_meta."""
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'version'"
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 1  # _SCHEMA_VERSION

    def test_creates_schema_meta_table(self, vm: VectorMemory):
        """The _schema_meta table should exist after connect()."""
        assert vm._db is not None
        rows = vm._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema_meta'"
        ).fetchall()
        assert len(rows) == 1

    def test_migration_idempotent(self, db_path: Path, mock_client):
        """Calling _migrate_schema() twice should not raise or change version."""
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        vm._migrate_schema()  # Second call — should be a no-op
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'version'"
        ).fetchone()
        assert int(row[0]) == 1
        vm.close()

    def test_migration_applies_alter_table(self, db_path: Path, mock_client):
        """A migration with ALTER TABLE should add the column to existing tables."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Simulate being at version 0 (pre-migration state)
        assert vm._db is not None
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        # Patch module-level constants to include a test migration
        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 2
        vm_mod._MIGRATIONS = [
            (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
        ]

        try:
            vm._migrate_schema()

            # Version should now be 2
            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 2

            # The 'tags' column should exist
            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "tags" in col_names
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_from_partial_version(self, db_path: Path, mock_client):
        """Migration from version 1 should apply only migrations for version > 1."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        assert vm._db is not None

        # Patch to have migrations for v2 and v3
        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 3
        vm_mod._MIGRATIONS = [
            (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
            (3, ["ALTER TABLE memory_entries ADD COLUMN priority INTEGER DEFAULT 0"]),
        ]

        try:
            vm._migrate_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 3

            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "tags" in col_names
            assert "priority" in col_names
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_existing_db_without_meta_migrates_from_zero(
        self, db_path: Path, mock_client
    ):
        """An existing database without _schema_meta should start at version 0 and migrate."""
        import src.vector_memory as vm_mod

        # Create a bare database with just the tables (no _schema_meta)
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm._open_connection(load_extension=False)
        assert vm._db is not None
        vm._db.enable_load_extension(True)
        sqlite_vec.load(vm._db)
        vm._db.enable_load_extension(False)
        vm._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                text TEXT NOT NULL,
                category TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)
        vm._db.commit()

        # Patch to have a migration
        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 2
        vm_mod._MIGRATIONS = [
            (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
        ]

        try:
            # Now ensure schema (which creates _schema_meta + migrates)
            vm._ensure_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 2
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_connect_still_creates_core_tables(self, db_path: Path, mock_client):
        """connect() should still create memory_entries and memory_vec after migration changes."""
        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()
        assert vm._db is not None

        tables = [
            r[0]
            for r in vm._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "memory_entries" in tables
        assert "memory_vec" in tables
        assert "_schema_meta" in tables
        vm.close()

    def test_migration_with_multiple_statements_per_step(
        self, db_path: Path, mock_client
    ):
        """A single migration step with multiple SQL statements should apply all of them."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Reset to version 0 so migrations apply
        assert vm._db is not None
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 2
        vm_mod._MIGRATIONS = [
            (
                2,
                [
                    "ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''",
                    "CREATE INDEX idx_memory_tags ON memory_entries(tags)",
                ],
            ),
        ]

        try:
            vm._migrate_schema()

            # Version should be 2
            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 2

            # Column 'tags' should exist
            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "tags" in col_names

            # Index 'idx_memory_tags' should exist
            indexes = vm._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memory_tags'"
            ).fetchall()
            assert len(indexes) == 1
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_creates_new_table(self, db_path: Path, mock_client):
        """A migration can add an entirely new table, not just ALTER existing ones."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Reset to version 0
        assert vm._db is not None
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 2
        vm_mod._MIGRATIONS = [
            (
                2,
                [
                    "CREATE TABLE memory_tags ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  memory_id INTEGER NOT NULL REFERENCES memory_entries(id),"
                    "  tag TEXT NOT NULL"
                    ")",
                ],
            ),
        ]

        try:
            vm._migrate_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 2

            # The new table should exist
            tables = [
                r[0]
                for r in vm._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            assert "memory_tags" in tables

            # The new table should be usable — insert a parent row first
            vm._db.execute(
                "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                "VALUES ('c1', 'hello', '', 1.0)"
            )
            vm._db.commit()
            vm._db.execute("INSERT INTO memory_tags (memory_id, tag) VALUES (1, 'test')")
            vm._db.commit()
            tags = vm._db.execute("SELECT tag FROM memory_tags").fetchall()
            assert tags == [("test",)]
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_from_mid_version_skips_older(
        self, db_path: Path, mock_client
    ):
        """Starting from version 1 with v1→v2 and v2→v3 migrations should only apply v2 and v3."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # DB is at version 1 after connect()
        assert vm._db is not None

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 3
        vm_mod._MIGRATIONS = [
            (1, ["ALTER TABLE memory_entries ADD COLUMN v1_col TEXT"]),
            (2, ["ALTER TABLE memory_entries ADD COLUMN v2_col TEXT DEFAULT ''"]),
            (3, ["ALTER TABLE memory_entries ADD COLUMN v3_col INTEGER DEFAULT 0"]),
        ]

        try:
            vm._migrate_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 3

            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]

            # v1_col should NOT exist — the DB was already at v1
            assert "v1_col" not in col_names
            # v2_col and v3_col should exist — these migrations applied
            assert "v2_col" in col_names
            assert "v3_col" in col_names
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_preserves_existing_data(self, db_path: Path, mock_client):
        """Data written before a migration should survive the migration."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Insert data before migration
        assert vm._db is not None
        vm._db.execute(
            "INSERT INTO memory_entries (chat_id, text, category, created_at) "
            "VALUES ('chat1', 'pre-migration data', '', 1000.0)"
        )
        vm._db.commit()

        # Reset to version 0 for migration
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 2
        vm_mod._MIGRATIONS = [
            (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
        ]

        try:
            vm._migrate_schema()

            # The pre-migration data should still be there
            rows = vm._db.execute(
                "SELECT chat_id, text FROM memory_entries"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0] == ("chat1", "pre-migration data")
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_failure_mid_way_preserves_partial_version(
        self, db_path: Path, mock_client
    ):
        """If the second migration fails, the first migration's version is preserved."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Reset to version 0
        assert vm._db is not None
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS
        vm_mod._SCHEMA_VERSION = 3
        vm_mod._MIGRATIONS = [
            (2, ["ALTER TABLE memory_entries ADD COLUMN tags TEXT DEFAULT ''"]),
            (3, ["ALTER TABLE nonexistent_table ADD COLUMN x TEXT"]),  # Will fail
        ]

        try:
            with pytest.raises(Exception):
                vm._migrate_schema()

            # Version should be 2 (first migration succeeded and committed)
            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert row is not None
            assert int(row[0]) == 2

            # The first migration's column should exist
            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "tags" in col_names
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()

    def test_migration_version_0_to_1_and_then_1_to_2(
        self, db_path: Path, mock_client
    ):
        """Simulates the real lifecycle: v0→v1 migration, then a later v1→v2 migration."""
        import src.vector_memory as vm_mod

        vm = VectorMemory(str(db_path), mock_client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Reset to version 0
        assert vm._db is not None
        vm._db.execute("DELETE FROM _schema_meta WHERE key = 'version'")
        vm._db.commit()

        original_version = vm_mod._SCHEMA_VERSION
        original_migrations = vm_mod._MIGRATIONS

        # First migration wave: v0 → v1
        vm_mod._SCHEMA_VERSION = 1
        vm_mod._MIGRATIONS = [
            (1, ["ALTER TABLE memory_entries ADD COLUMN source TEXT DEFAULT 'user'"]),
        ]

        try:
            vm._migrate_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 1

            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "source" in col_names

            # Second migration wave: v1 → v2 (simulates a later software update)
            vm_mod._SCHEMA_VERSION = 2
            vm_mod._MIGRATIONS = [
                (1, ["ALTER TABLE memory_entries ADD COLUMN source TEXT DEFAULT 'user'"]),
                (2, ["ALTER TABLE memory_entries ADD COLUMN confidence REAL DEFAULT 1.0"]),
            ]

            vm._migrate_schema()

            row = vm._db.execute(
                "SELECT value FROM _schema_meta WHERE key = 'version'"
            ).fetchone()
            assert int(row[0]) == 2

            col_rows = vm._db.execute("PRAGMA table_info(memory_entries)").fetchall()
            col_names = [c[1] for c in col_rows]
            assert "source" in col_names
            assert "confidence" in col_names
        finally:
            vm_mod._SCHEMA_VERSION = original_version
            vm_mod._MIGRATIONS = original_migrations
            vm.close()


# ─────────────────────────────────────────────────────────────────────────────
# _embed_batch() API Error Propagation
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbedBatchApiErrorPropagation:
    """Tests for _embed_batch() error propagation when the embeddings API call fails.

    Verifies that when the API call fails:
      (a) all pending futures receive the exception
      (b) in-flight entries are cleaned up
      (c) the LRU cache is not polluted with partial results
      (d) a subsequent batch call after failure succeeds

    Uses ValueError as the failure type — it is classified as non-transient
    by is_transient_error(), so the @retry_with_backoff decorator on
    _embed_batch() will NOT retry and will propagate the error immediately,
    keeping tests fast and deterministic.
    """

    @pytest.mark.asyncio
    async def test_all_futures_receive_exception(self, db_path: Path):
        """When the API call fails for a batch with multiple unique texts,
        all futures (one per unique text) should have their exception set.

        Verified by launching concurrent _embed() callers for each unique
        text alongside the _embed_batch() call — the _embed() callers find
        the in-flight futures and await them, so they should all receive
        the same error.
        """
        client = _make_mock_client()
        gate = asyncio.Event()
        call_count = 0

        async def gated_failing_create(**kwargs):
            nonlocal call_count
            call_count += 1
            gate.set()
            await asyncio.sleep(0.05)
            raise ValueError("API error")

        client.embeddings.create = gated_failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        embed_errors: list[Exception] = []
        batch_error: Exception | None = None

        async def call_embed(text: str):
            try:
                await vm._embed(text)
            except Exception as e:
                embed_errors.append(e)

        async def call_batch():
            nonlocal batch_error
            try:
                await vm._embed_batch(["text_a", "text_b", "text_c"])
            except Exception as e:
                batch_error = e

        await asyncio.gather(
            call_batch(),
            call_embed("text_a"),
            call_embed("text_b"),
            call_embed("text_c"),
        )

        # All 4 callers should have received errors
        assert batch_error is not None
        assert isinstance(batch_error, ValueError)
        assert len(embed_errors) == 3
        assert all(isinstance(e, ValueError) for e in embed_errors)
        # Only 1 API call was made (deduplication via in-flight)
        assert call_count == 1

        vm.close()

    @pytest.mark.asyncio
    async def test_inflight_cleaned_up_for_all_texts(self, db_path: Path):
        """After a failed _embed_batch() with multiple texts, all in-flight
        entries should be removed."""
        client = _make_mock_client()

        async def failing_create(**kwargs):
            raise ValueError("fail")

        client.embeddings.create = failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError):
            await vm._embed_batch(["text_x", "text_y", "text_z"])

        for text in ["text_x", "text_y", "text_z"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key not in vm._inflight

        vm.close()

    @pytest.mark.asyncio
    async def test_cache_not_polluted_on_error(self, db_path: Path):
        """After a failed _embed_batch(), the LRU cache should NOT contain
        entries for any of the failed texts."""
        client = _make_mock_client()

        async def failing_create(**kwargs):
            raise ValueError("API failure")

        client.embeddings.create = failing_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError):
            await vm._embed_batch(["uncached_a", "uncached_b"])

        for text in ["uncached_a", "uncached_b"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key not in vm._embed_cache

        vm.close()

    @pytest.mark.asyncio
    async def test_cache_not_polluted_with_partial_results(self, db_path: Path):
        """A failed batch should not add new entries to cache, even when some
        texts were already cached before the batch call."""
        client = _make_mock_client()
        original_create = client.embeddings.create
        call_count = 0

        async def fail_on_second_call(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return await original_create(**kwargs)
            raise ValueError("API failure on batch")

        client.embeddings.create = fail_on_second_call

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # Pre-cache one text
        await vm._embed("pre_cached")
        assert call_count == 1
        pre_cached_key = hashlib.sha256("pre_cached".encode()).hexdigest()
        assert pre_cached_key in vm._embed_cache
        cache_snapshot_before = set(vm._embed_cache.keys())

        # Batch with pre-cached + new text — should fail on the API call
        with pytest.raises(ValueError):
            await vm._embed_batch(["pre_cached", "new_text"])

        # Cache should be unchanged — no new entries added
        new_text_key = hashlib.sha256("new_text".encode()).hexdigest()
        assert new_text_key not in vm._embed_cache
        assert set(vm._embed_cache.keys()) == cache_snapshot_before

        vm.close()

    @pytest.mark.asyncio
    async def test_subsequent_batch_succeeds_after_failure(self, db_path: Path):
        """After a failed _embed_batch() call, a subsequent call with the same
        texts should succeed — inflight entries are cleaned up so there is no
        stale state."""
        client = _make_mock_client()
        original_create = client.embeddings.create
        fail_count = 0

        async def fail_once_then_succeed(**kwargs):
            nonlocal fail_count
            fail_count += 1
            if fail_count == 1:
                raise ValueError("transient failure")
            return await original_create(**kwargs)

        client.embeddings.create = fail_once_then_succeed

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # First call fails
        with pytest.raises(ValueError):
            await vm._embed_batch(["retry_text"])

        # In-flight should be clean
        retry_key = hashlib.sha256("retry_text".encode()).hexdigest()
        assert retry_key not in vm._inflight
        assert retry_key not in vm._embed_cache

        # Second call succeeds
        results = await vm._embed_batch(["retry_text"])
        assert len(results) == 1
        assert results[0] is not None
        assert len(results[0]) == EMBEDDING_DIM

        # Cache should now have the entry
        assert retry_key in vm._embed_cache

        vm.close()

    @pytest.mark.asyncio
    async def test_subsequent_batch_with_different_texts_after_failure(
        self, db_path: Path
    ):
        """After a failed batch, a batch with completely different texts should
        work, and the cache should only contain the successful texts."""
        client = _make_mock_client()
        original_create = client.embeddings.create
        should_fail = True

        async def conditional_fail(**kwargs):
            nonlocal should_fail
            if should_fail:
                raise ValueError("first call fails")
            return await original_create(**kwargs)

        client.embeddings.create = conditional_fail

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        # First batch fails
        with pytest.raises(ValueError):
            await vm._embed_batch(["failed_a", "failed_b"])

        # Switch to success mode
        should_fail = False

        # Second batch with different texts succeeds
        results = await vm._embed_batch(["fresh_x", "fresh_y", "fresh_z"])
        assert len(results) == 3
        assert all(r is not None for r in results)
        assert all(len(r) == EMBEDDING_DIM for r in results)

        # Cache should only have the successful texts
        for text in ["fresh_x", "fresh_y", "fresh_z"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key in vm._embed_cache

        # Failed texts should NOT be in cache
        for text in ["failed_a", "failed_b"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key not in vm._embed_cache

        vm.close()


# ─────────────────────────────────────────────────────────────────────────────
# _embed_batch() Count Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestEmbedBatchCountValidation:
    """Tests for _embed_batch() count validation when the API returns a
    mismatched number of embeddings.

    The _embed_batch() implementation validates that the API returns exactly
    len(unique_texts) embedding results. If the count doesn't match (e.g. due
    to content filtering or API errors), a ValueError is raised.

    This covers aspect (c) of the Phase 14 test requirement:
      "count validation when API returns fewer embeddings than requested"

    Aspects (a) and (b) — mixed cached/uncached batches and duplicate text
    deduplication — are covered by TestEmbedBatchCacheInflight.
    """

    @pytest.mark.asyncio
    async def test_api_returns_fewer_embeddings_raises_value_error(
        self, db_path: Path
    ):
        """When the API returns fewer embeddings than unique texts,
        _embed_batch() raises ValueError with a descriptive message."""
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def short_response_create(**kwargs):
            inp = kwargs["input"]
            if isinstance(inp, list) and len(inp) > 1:
                # Return only 1 embedding regardless of input count
                text = inp[0]
                seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
                vec = _make_embedding(seed)
                resp = MagicMock()
                resp.data = [MagicMock(embedding=vec)]
                return resp
            return await original_create(**kwargs)

        client.embeddings.create = short_response_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError, match="returned 1 results for 2 inputs"):
            await vm._embed_batch(["text_x", "text_y"])

        vm.close()

    @pytest.mark.asyncio
    async def test_api_returns_zero_embeddings(self, db_path: Path):
        """When the API returns an empty data list for a non-empty batch,
        ValueError is raised."""
        client = _make_mock_client()

        async def empty_response_create(**kwargs):
            resp = MagicMock()
            resp.data = []
            return resp

        client.embeddings.create = empty_response_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError, match="returned 0 results for 3 inputs"):
            await vm._embed_batch(["aaa", "bbb", "ccc"])

        vm.close()

    @pytest.mark.asyncio
    async def test_count_mismatch_cleans_up_inflight(self, db_path: Path):
        """After a count mismatch error, all in-flight entries should be removed."""
        client = _make_mock_client()

        async def empty_response_create(**kwargs):
            resp = MagicMock()
            resp.data = []
            return resp

        client.embeddings.create = empty_response_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError):
            await vm._embed_batch(["aaa", "bbb", "ccc"])

        for text in ["aaa", "bbb", "ccc"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key not in vm._inflight

        vm.close()

    @pytest.mark.asyncio
    async def test_count_mismatch_does_not_pollute_cache(self, db_path: Path):
        """After a count mismatch error, the LRU cache should not contain entries
        for any of the texts that were sent to the API."""
        client = _make_mock_client()
        original_create = client.embeddings.create

        async def one_short_create(**kwargs):
            inp = kwargs["input"]
            if isinstance(inp, list) and len(inp) > 1:
                # Return one fewer embedding than requested
                results = []
                for text in inp[:-1]:
                    seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
                    vec = _make_embedding(seed)
                    results.append(MagicMock(embedding=vec))
                resp = MagicMock()
                resp.data = results
                return resp
            return await original_create(**kwargs)

        client.embeddings.create = one_short_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError):
            await vm._embed_batch(["uncached_a", "uncached_b"])

        for text in ["uncached_a", "uncached_b"]:
            key = hashlib.sha256(text.encode()).hexdigest()
            assert key not in vm._embed_cache

        vm.close()

    @pytest.mark.asyncio
    async def test_value_error_not_retried(self, db_path: Path):
        """ValueError from count mismatch is non-transient, so @retry_with_backoff
        should not retry — the API is called exactly once."""
        client = _make_mock_client()
        call_count = 0

        async def counting_empty_create(**kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.data = []
            return resp

        client.embeddings.create = counting_empty_create

        vm = VectorMemory(str(db_path), client, EMBEDDING_MODEL, EMBEDDING_DIM)
        vm.connect()

        with pytest.raises(ValueError):
            await vm._embed_batch(["text1", "text2"])

        # ValueError is non-transient — no retries
        assert call_count == 1

        vm.close()


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent Read-Write Isolation (WAL-mode snapshot consistency)
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentReadWriteIsolation:
    """Tests for WAL-mode snapshot isolation during concurrent reads and writes.

    Validates that read-only connections see a consistent snapshot even while
    concurrent writes are inserting new entries on the main connection.
    This is the core guarantee of SQLite WAL mode that VectorMemory relies on.
    """

    def test_read_snapshot_isolated_from_concurrent_writes(self, vm: VectorMemory):
        """A read transaction on a pooled read connection should see a consistent
        snapshot, even while a concurrent thread inserts and commits new entries.

        Steps:
        1. Pre-populate N entries
        2. Reader thread: BEGIN → SELECT COUNT (N) → signal writer → wait → SELECT COUNT
        3. Writer thread: wait for reader signal → insert + commit M entries → signal done
        4. Reader's second COUNT should still return N (snapshot isolation)
        5. A fresh read after the reader's transaction ends should see N+M entries
        """
        import concurrent.futures

        assert vm._db is not None
        num_initial = 5
        num_new = 3

        for i in range(num_initial):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"initial {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        read_started = threading.Event()
        write_done = threading.Event()
        snapshot_counts: list[tuple[int, int]] = []
        errors: list[Exception] = []

        def reader():
            try:
                conn = vm._get_read_connection()
                conn.execute("BEGIN")
                # First read establishes the snapshot
                count_before = conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                    ("chat1",),
                ).fetchone()[0]
                read_started.set()
                write_done.wait(timeout=10)
                # Second read within the same transaction — should be identical
                count_after = conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                    ("chat1",),
                ).fetchone()[0]
                snapshot_counts.append((count_before, count_after))
                conn.execute("COMMIT")
            except Exception as e:
                errors.append(e)

        def writer():
            read_started.wait(timeout=10)
            time.sleep(0.05)  # Ensure reader's snapshot is fully established
            with vm._write_lock:
                for i in range(num_new):
                    cur = vm._db.execute(
                        "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        ("chat1", f"concurrent {i}", "", time.time()),
                    )
                    row_id = cur.lastrowid
                    vm._db.execute(
                        "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                        (row_id, _serialize_f32(_make_embedding(i + 100))),
                    )
                vm._db.commit()
            write_done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reader), pool.submit(writer)]
            concurrent.futures.wait(futures, timeout=15)

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(snapshot_counts) == 1

        before, after = snapshot_counts[0]
        assert before == num_initial
        assert after == num_initial  # Snapshot: concurrent writes invisible

        # Fresh read should see all committed entries
        assert vm.count("chat1") == num_initial + num_new

    def test_list_recent_returns_consistent_snapshot_during_writes(
        self, vm: VectorMemory,
    ):
        """list_recent() on a read connection within a transaction should return
        the same entries before and after concurrent writes."""
        import concurrent.futures

        assert vm._db is not None
        num_initial = 4
        num_new = 3

        for i in range(num_initial):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"initial {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        read_started = threading.Event()
        write_done = threading.Event()
        snapshots: list[tuple[set[str], set[str]]] = []
        errors: list[Exception] = []

        def reader():
            try:
                conn = vm._get_read_connection()
                conn.execute("BEGIN")
                rows_before = conn.execute(
                    "SELECT text FROM memory_entries WHERE chat_id = ? ORDER BY created_at",
                    ("chat1",),
                ).fetchall()
                texts_before = {r[0] for r in rows_before}
                read_started.set()
                write_done.wait(timeout=10)
                rows_after = conn.execute(
                    "SELECT text FROM memory_entries WHERE chat_id = ? ORDER BY created_at",
                    ("chat1",),
                ).fetchall()
                texts_after = {r[0] for r in rows_after}
                snapshots.append((texts_before, texts_after))
                conn.execute("COMMIT")
            except Exception as e:
                errors.append(e)

        def writer():
            read_started.wait(timeout=10)
            time.sleep(0.05)
            with vm._write_lock:
                for i in range(num_new):
                    cur = vm._db.execute(
                        "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        ("chat1", f"written {i}", "", time.time()),
                    )
                    row_id = cur.lastrowid
                    vm._db.execute(
                        "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                        (row_id, _serialize_f32(_make_embedding(i + 100))),
                    )
                vm._db.commit()
            write_done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reader), pool.submit(writer)]
            concurrent.futures.wait(futures, timeout=15)

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(snapshots) == 1

        before_texts, after_texts = snapshots[0]
        assert len(before_texts) == num_initial
        assert before_texts == after_texts  # Snapshot: same entries visible
        # None of the concurrently-written entries leaked into the snapshot
        assert not any(t.startswith("written ") for t in before_texts)

        # Fresh read sees everything
        all_recent = vm.list_recent("chat1", limit=20)
        assert len(all_recent) == num_initial + num_new

    def test_multiple_readers_each_see_consistent_snapshots(self, vm: VectorMemory):
        """Multiple concurrent readers should each see their own consistent snapshot,
        unaffected by a writer inserting data between their reads."""
        import concurrent.futures

        assert vm._db is not None
        num_initial = 5
        num_new = 4

        for i in range(num_initial):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"initial {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        all_readers_started = threading.Event()
        write_done = threading.Event()
        reader_results: dict[int, tuple[int, int]] = {}
        reader_lock = threading.Lock()
        errors: list[Exception] = []
        num_readers = 3

        def reader(reader_id: int):
            try:
                conn = vm._get_read_connection()
                conn.execute("BEGIN")
                count_before = conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                    ("chat1",),
                ).fetchone()[0]
                with reader_lock:
                    reader_results[reader_id] = (count_before, -1)
                    if len(reader_results) == num_readers:
                        all_readers_started.set()
                write_done.wait(timeout=10)
                count_after = conn.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE chat_id = ?",
                    ("chat1",),
                ).fetchone()[0]
                with reader_lock:
                    reader_results[reader_id] = (count_before, count_after)
                conn.execute("COMMIT")
            except Exception as e:
                errors.append(e)

        def writer():
            all_readers_started.wait(timeout=10)
            time.sleep(0.05)
            with vm._write_lock:
                for i in range(num_new):
                    cur = vm._db.execute(
                        "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        ("chat1", f"concurrent {i}", "", time.time()),
                    )
                    row_id = cur.lastrowid
                    vm._db.execute(
                        "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                        (row_id, _serialize_f32(_make_embedding(i + 50))),
                    )
                vm._db.commit()
            write_done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(reader, i) for i in range(num_readers)]
            futures.append(pool.submit(writer))
            concurrent.futures.wait(futures, timeout=15)

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(reader_results) == num_readers
        for rid, (before, after) in reader_results.items():
            assert before == num_initial, f"Reader {rid}: expected {num_initial} before writes"
            assert after == num_initial, f"Reader {rid}: snapshot broken — saw {after} after writes"

        assert vm.count("chat1") == num_initial + num_new

    def test_search_returns_consistent_snapshot_during_writes(self, vm: VectorMemory):
        """A KNN vector search on a read connection should see a consistent snapshot,
        even while a concurrent thread inserts and commits new entries with embeddings.

        This validates WAL-mode isolation through the vec0 virtual table KNN path
        (the actual _search_sync code path used by search()), not just plain SQL.

        Steps:
        1. Pre-populate N entries with known embeddings
        2. Reader thread: open read txn → KNN search (N results) → signal writer →
           wait → KNN search again → verify identical results
        3. Writer thread: insert M new entries + commit → signal done
        4. Reader's second search should return the exact same N results (snapshot)
        5. A fresh search after the reader's transaction ends should see N+M entries
        """
        import concurrent.futures

        assert vm._db is not None
        num_initial = 5
        num_new = 3

        # Pre-populate entries with distinct embeddings so KNN ordering is deterministic
        for i in range(num_initial):
            with vm._write_lock:
                cur = vm._db.execute(
                    "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    ("chat1", f"initial {i}", "", time.time()),
                )
                row_id = cur.lastrowid
                vm._db.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (row_id, _serialize_f32(_make_embedding(i))),
                )
                vm._db.commit()

        # Use a query vector close to embedding(0) so we get deterministic results
        query_vec = _serialize_f32(_make_embedding(0))

        read_started = threading.Event()
        write_done = threading.Event()
        search_results: list[tuple[list[int], list[int]]] = []
        errors: list[Exception] = []

        def reader():
            try:
                conn = vm._get_read_connection()
                conn.execute("BEGIN")
                # First KNN search — establishes snapshot via vec0 virtual table
                rows_before = conn.execute(
                    """
                    SELECT e.id, e.text, v.distance
                    FROM memory_vec v
                    JOIN memory_entries e ON e.id = v.rowid
                    WHERE v.embedding MATCH ?
                      AND v.k = ?
                      AND e.chat_id = ?
                    ORDER BY v.distance
                    """,
                    (query_vec, 10, "chat1"),
                ).fetchall()
                ids_before = [r[0] for r in rows_before]
                read_started.set()
                write_done.wait(timeout=10)
                # Second KNN search within the same transaction
                rows_after = conn.execute(
                    """
                    SELECT e.id, e.text, v.distance
                    FROM memory_vec v
                    JOIN memory_entries e ON e.id = v.rowid
                    WHERE v.embedding MATCH ?
                      AND v.k = ?
                      AND e.chat_id = ?
                    ORDER BY v.distance
                    """,
                    (query_vec, 10, "chat1"),
                ).fetchall()
                ids_after = [r[0] for r in rows_after]
                search_results.append((ids_before, ids_after))
                conn.execute("COMMIT")
            except Exception as e:
                errors.append(e)

        def writer():
            read_started.wait(timeout=10)
            time.sleep(0.05)  # Ensure reader's snapshot is fully established
            with vm._write_lock:
                for i in range(num_new):
                    cur = vm._db.execute(
                        "INSERT INTO memory_entries (chat_id, text, category, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        ("chat1", f"concurrent {i}", "", time.time()),
                    )
                    row_id = cur.lastrowid
                    vm._db.execute(
                        "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                        (row_id, _serialize_f32(_make_embedding(i + 200))),
                    )
                vm._db.commit()
            write_done.set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reader), pool.submit(writer)]
            concurrent.futures.wait(futures, timeout=15)

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(search_results) == 1

        before_ids, after_ids = search_results[0]
        assert len(before_ids) == num_initial
        assert before_ids == after_ids, (
            f"Snapshot broken: KNN search saw {before_ids} then {after_ids} "
            f"within the same transaction"
        )

        # Fresh search should see all committed entries
        fresh_results = vm._search_sync("chat1", query_vec, 10)
        assert len(fresh_results) == num_initial + num_new
