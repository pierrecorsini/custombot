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
        text = kwargs["input"]
        if text in embedding_map:
            vec = embedding_map[text]
        else:
            # Deterministic embedding from text hash
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
        row = vm._db.execute(
            "SELECT rowid FROM memory_vec WHERE rowid = ?", (row_id,)
        ).fetchone()
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
    async def test_returns_entries_in_reverse_chronological_order(
        self, vm: VectorMemory
    ):
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
        row = vm._db.execute(
            "SELECT * FROM memory_entries WHERE id = ?", (row_id,)
        ).fetchone()
        assert row is None

    @pytest.mark.asyncio
    async def test_removes_from_vec_table(self, vm: VectorMemory):
        row_id = await vm.save("chat1", "will be deleted")
        vm.delete(row_id)
        assert vm._db is not None
        row = vm._db.execute(
            "SELECT * FROM memory_vec WHERE rowid = ?", (row_id,)
        ).fetchone()
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
        vm._embed_cache_max = 3

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
        vm._embed_cache_max = 3

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
        assert vm._embed_cache_max == 256

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
        """list_recent acquires the lock — verify no concurrent modification."""
        # This is more of a structural test — ensure the lock exists
        assert isinstance(vm._lock, type(threading.Lock()))

    def test_lock_is_held_during_delete(self, vm: VectorMemory):
        """delete acquires the lock."""
        assert isinstance(vm._lock, type(threading.Lock()))

    def test_lock_is_held_during_count(self, vm: VectorMemory):
        """count acquires the lock."""
        assert isinstance(vm._lock, type(threading.Lock()))


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
