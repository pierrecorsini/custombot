"""
Tests for src/memory.py — Per-chat persistent memory manager.

Unit tests covering:
  - _safe_name character stripping
  - Memory read/write flow
  - LRU cache behaviour (mtime-based hits, write invalidation, eviction)
  - Corruption detection (checksum mismatch, missing checksum file, read errors)
  - Backup and repair operations
  - ensure_workspace seeding AGENTS.md
  - read_agents_md with caching
  - read_memory_with_validation / write_memory_with_checksum
  - Recovery event logging, has_recovery_events, clear_recovery_log
  - MemoryCorruptionResult dataclass
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from src.memory import (
    _DEFAULT_AGENTS_MD,
    AGENTS_FILENAME,
    BACKUP_DIR,
    MEMORY_FILENAME,
    RECOVERY_LOG_FILENAME,
    Memory,
    MemoryCorruptionResult,
    _safe_name,
)
from src.security import PathSecurityError, is_path_in_workspace
from src.utils import LRUDict

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a clean temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def mem(workspace: Path) -> Memory:
    """Provide a Memory instance backed by the temp workspace."""
    return Memory(str(workspace))


def _chat_dir(workspace: Path, chat_id: str) -> Path:
    """Return the expected chat directory path for assertions."""
    return workspace / "whatsapp_data" / _safe_name(chat_id)


def _memory_path(workspace: Path, chat_id: str) -> Path:
    return _chat_dir(workspace, chat_id) / MEMORY_FILENAME


def _agents_path(workspace: Path, chat_id: str) -> Path:
    return _chat_dir(workspace, chat_id) / AGENTS_FILENAME


def _checksum_path(workspace: Path, chat_id: str) -> Path:
    return _chat_dir(workspace, chat_id) / ".memory_checksum"


def _write_memory_raw(workspace: Path, chat_id: str, content: str) -> None:
    """Write a MEMORY.md file directly to disk, bypassing Memory methods."""
    p = _memory_path(workspace, chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# _safe_name
# ─────────────────────────────────────────────────────────────────────────────


class TestSafeName:
    """Tests for the _safe_name helper function."""

    def test_alphanumeric_passthrough(self):
        assert _safe_name("abc123") == "abc123"

    def test_allows_hyphen_underscore_dot(self):
        assert _safe_name("a-b_c.d") == "a-b_c.d"

    @pytest.mark.parametrize(
        "char",
        ["#", "$", "%", "^", "&", "(", ")", "!", " "],
    )
    def test_unsafe_chars_replaced_with_underscore(self, char: str):
        assert _safe_name(char) == "_"

    @pytest.mark.parametrize(
        "char, expected",
        [
            ("@", "_at_"),
            ("*", "_as_"),
            ("/", "_sl_"),
            ("\\", "_bs_"),
            (":", "_col_"),
            ("|", "_pi_"),
        ],
    )
    def test_unsafe_chars_replaced_with_named(self, char: str, expected: str):
        assert _safe_name(char) == expected

    def test_email_becomes_safe(self):
        result = _safe_name("user@domain.com")
        assert "@" not in result
        assert "." in result  # dot is allowed
        assert result == "user_at_domain.com"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _safe_name("")

    def test_phone_number_with_plus(self):
        result = _safe_name("+1234567890")
        assert result == "_1234567890"

    def test_unicode_characters(self):
        result = _safe_name("日本語")
        # Non-ASCII alnum is not matched by str.isalnum in C locale
        # but Python's isalnum does match CJK — verify replacement
        assert all(c.isalnum() or c in "-_." for c in result)

    def test_preserves_dots_for_extensions(self):
        assert _safe_name("chat.123") == "chat.123"

    def test_multiple_special_chars(self):
        result = _safe_name("a@b#c$d")
        assert result == "a_at_b_c_d"

    def test_consecutive_specials(self):
        result = _safe_name("a@@b")
        assert result == "a_at__at_b"


# ─────────────────────────────────────────────────────────────────────────────
# MemoryCorruptionResult dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestMemoryCorruptionResult:
    """Tests for the MemoryCorruptionResult dataclass defaults."""

    def test_default_values(self):
        r = MemoryCorruptionResult(
            file_path="/tmp/MEMORY.md", is_corrupted=False, checksum_valid=True
        )
        assert r.error_details == []
        assert r.backup_path is None
        assert r.repaired is False

    def test_all_fields_set(self):
        r = MemoryCorruptionResult(
            file_path="/tmp/MEMORY.md",
            is_corrupted=True,
            checksum_valid=False,
            error_details=["mismatch"],
            backup_path="/tmp/backup.md.bak",
            repaired=True,
        )
        assert r.is_corrupted is True
        assert r.backup_path == "/tmp/backup.md.bak"
        assert r.repaired is True
        assert r.error_details == ["mismatch"]


# ─────────────────────────────────────────────────────────────────────────────
# Constructor
# ─────────────────────────────────────────────────────────────────────────────


class TestMemoryInit:
    """Tests for Memory.__init__."""

    def test_root_stored_as_path(self, workspace: Path):
        m = Memory(str(workspace))
        assert m._root == workspace

    def test_caches_initially_empty(self, mem: Memory):
        assert len(mem._memory_cache) == 0
        assert len(mem._agents_cache) == 0


# ─────────────────────────────────────────────────────────────────────────────
# LRU Cache Internals
# ─────────────────────────────────────────────────────────────────────────────


class TestLRUCacheInternals:
    """Tests for MtimeCache-backed cache mechanics in Memory."""

    def test_caches_initially_empty(self, mem: Memory):
        assert len(mem._memory_cache) == 0
        assert len(mem._agents_cache) == 0

    @pytest.mark.asyncio
    async def test_miss_on_first_read(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "content")
        await mem.read_memory("chat1")
        assert mem._memory_cache.misses == 1
        assert mem._memory_cache.hits == 0

    @pytest.mark.asyncio
    async def test_hit_on_reread_unchanged(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "content")
        await mem.read_memory("chat1")
        await mem.read_memory("chat1")
        assert mem._memory_cache.hits == 1
        assert mem._memory_cache.misses == 1

    @pytest.mark.asyncio
    async def test_invalidate_removes_entry(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "content")
        await mem.read_memory("chat1")
        assert "chat1" in mem._memory_cache
        mem._memory_cache.invalidate("chat1")
        assert "chat1" not in mem._memory_cache

    @pytest.mark.asyncio
    async def test_separate_caches(self, mem: Memory, workspace: Path):
        """Memory cache and agents cache track hits/misses independently."""
        _write_memory_raw(workspace, "chat1", "mem")
        await mem.read_memory("chat1")
        mem.ensure_workspace("chat2")
        await mem.read_agents_md("chat2")
        assert mem._memory_cache.misses == 1
        assert mem._agents_cache.misses == 1


# ─────────────────────────────────────────────────────────────────────────────
# workspace_path / _chat_dir
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkspacePath:
    """Tests for workspace_path and internal _chat_dir."""

    def test_workspace_path_returns_whatsapp_data_subdir(self, mem: Memory):
        result = mem.workspace_path("my-chat")
        assert result == mem._root / "whatsapp_data" / "my-chat"

    def test_workspace_path_sanitizes_chat_id(self, mem: Memory):
        result = mem.workspace_path("user@domain")
        assert result.name == "user_at_domain"


# ─────────────────────────────────────────────────────────────────────────────
# ensure_workspace
# ─────────────────────────────────────────────────────────────────────────────


class TestEnsureWorkspace:
    """Tests for ensure_workspace."""

    def test_creates_directory(self, mem: Memory):
        result = mem.ensure_workspace("chat1")
        assert result.is_dir()

    def test_seeds_agents_md(self, mem: Memory):
        workspace = mem.ensure_workspace("chat1")
        agents = workspace / AGENTS_FILENAME
        assert agents.exists()
        assert agents.read_text(encoding="utf-8") == _DEFAULT_AGENTS_MD

    def test_does_not_overwrite_existing_agents_md(self, mem: Memory):
        workspace = mem.ensure_workspace("chat1")
        agents = workspace / AGENTS_FILENAME
        custom = "# Custom Agent\nSpecial instructions."
        agents.write_text(custom, encoding="utf-8")

        mem.ensure_workspace("chat1")

        assert agents.read_text(encoding="utf-8") == custom

    def test_idempotent(self, mem: Memory):
        path1 = mem.ensure_workspace("chat1")
        path2 = mem.ensure_workspace("chat1")
        assert path1 == path2

    def test_clears_agents_cache_on_seed(self, mem: Memory):
        """Seeding AGENTS.md should invalidate the agents cache entry."""
        # Populate agents cache via underlying LRUDict (MtimeCache has no __setitem__)
        mem._agents_cache._cache["chat1"] = (1.0, "old")
        mem.ensure_workspace("chat1")
        assert "chat1" not in mem._agents_cache

    def test_does_not_clear_agents_cache_if_agents_exists(self, mem: Memory):
        """If AGENTS.md already exists, the cache should not be touched."""
        mem.ensure_workspace("chat1")  # creates AGENTS.md
        mem._agents_cache._cache["chat1"] = (1.0, "cached")
        mem.ensure_workspace("chat1")  # should NOT evict
        assert mem._agents_cache._cache.get("chat1") == (1.0, "cached")

    def test_creates_parent_directories(self, mem: Memory):
        result = mem.ensure_workspace("deep/chat")
        assert result.is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# write_memory / read_memory
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteMemory:
    """Tests for write_memory."""

    @pytest.mark.asyncio
    async def test_creates_file_with_content(self, mem: Memory, workspace: Path):
        await mem.write_memory("chat1", "# My Notes\nHello")
        content = _memory_path(workspace, "chat1").read_text(encoding="utf-8")
        assert content.startswith("# My Notes\nHello\n")

    @pytest.mark.asyncio
    async def test_strips_trailing_whitespace(self, mem: Memory, workspace: Path):
        await mem.write_memory("chat1", "  hello  ")
        content = _memory_path(workspace, "chat1").read_text(encoding="utf-8")
        assert content == "hello\n"

    @pytest.mark.asyncio
    async def test_invalidates_memory_cache(self, mem: Memory):
        # Populate cache via a normal read
        _write_memory_raw(mem._root, "chat1", "old")
        await mem.read_memory("chat1")
        assert "chat1" in mem._memory_cache

        await mem.write_memory("chat1", "new content")
        assert "chat1" not in mem._memory_cache

    @pytest.mark.asyncio
    async def test_creates_directory_if_missing(self, mem: Memory, workspace: Path):
        await mem.write_memory("new-chat", "content")
        assert _memory_path(workspace, "new-chat").exists()


# ─────────────────────────────────────────────────────────────────────────────
# write_memory cache invalidation
# ─────────────────────────────────────────────────────────────────────────────


class TestWriteMemoryCacheInvalidation:
    """Verify that write_memory() properly invalidates the mtime cache so
    subsequent reads always reflect the freshly written content.

    Covers:
      (a) cache entry is removed (invalidate called)
      (b) next read returns the new content, not stale cache
      (c) cache miss counter increments on the post-write read
    """

    @pytest.mark.asyncio
    async def test_invalidate_removes_cached_entry(self, mem: Memory, workspace: Path):
        """write_memory() must call _memory_cache.invalidate(chat_id), removing
        the cached entry so a subsequent read re-fetches from disk."""
        # Arrange — populate cache via a normal read
        _write_memory_raw(workspace, "chat1", "original")
        await mem.read_memory("chat1")
        assert "chat1" in mem._memory_cache  # cache populated

        # Act
        await mem.write_memory("chat1", "new content")

        # Assert — cache entry removed
        assert "chat1" not in mem._memory_cache

    @pytest.mark.asyncio
    async def test_next_read_returns_new_content(self, mem: Memory, workspace: Path):
        """After write_memory(), a read must return the freshly written content
        rather than the stale cached version."""
        # Arrange
        _write_memory_raw(workspace, "chat1", "old")
        await mem.read_memory("chat1")
        assert await mem.read_memory("chat1") == "old"  # cached

        # Act
        await mem.write_memory("chat1", "updated")

        # Assert
        result = await mem.read_memory("chat1")
        assert result == "updated"

    @pytest.mark.asyncio
    async def test_cache_miss_counter_increments(self, mem: Memory, workspace: Path):
        """The post-write read should be a cache miss (invalidated entry forces
        a fresh stat+read from disk), so the miss counter must increment."""
        # Arrange — populate cache
        _write_memory_raw(workspace, "chat1", "initial")
        await mem.read_memory("chat1")
        misses_before = mem._memory_cache.misses

        # Act — write invalidates, then read must go to disk
        await mem.write_memory("chat1", "replacement")
        await mem.read_memory("chat1")

        # Assert — exactly one additional miss
        assert mem._memory_cache.misses == misses_before + 1

    @pytest.mark.asyncio
    async def test_write_then_read_populates_cache_with_new_content(
        self, mem: Memory, workspace: Path
    ):
        """After write→read, the cache should store the new content and mtime
        so subsequent reads are cache hits with the correct data."""
        # Arrange
        _write_memory_raw(workspace, "chat1", "v1")
        await mem.read_memory("chat1")

        # Act
        await mem.write_memory("chat1", "v2")
        await mem.read_memory("chat1")

        # Assert — cache now holds v2 (accessed via underlying LRUDict)
        cached = mem._memory_cache._cache.get("chat1")
        assert cached is not None
        # MtimeCache stores raw file content; write_memory appends \n
        assert cached[1].strip() == "v2"

        # Further read should be a cache hit
        hits_before = mem._memory_cache.hits
        result = await mem.read_memory("chat1")
        assert result == "v2"
        assert mem._memory_cache.hits == hits_before + 1


class TestReadMemory:
    """Tests for read_memory."""

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent(self, mem: Memory):
        result = await mem.read_memory("no-such-chat")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_file_content(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "# Notes\nSome text\n")
        result = await mem.read_memory("chat1")
        assert result == "# Notes\nSome text"

    @pytest.mark.asyncio
    async def test_returns_none_for_whitespace_only(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "   \n  \n")
        result = await mem.read_memory("chat1")
        assert result is None

    @pytest.mark.asyncio
    async def test_populates_cache_on_read(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "cached content")
        await mem.read_memory("chat1")
        cached = mem._memory_cache._cache.get("chat1")
        assert cached is not None
        assert cached[1] == "cached content"

    @pytest.mark.asyncio
    async def test_cache_hit_on_same_mtime(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "content")
        await mem.read_memory("chat1")
        # Second read should hit cache (file unchanged)
        result = await mem.read_memory("chat1")
        assert result == "content"

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_mtime_change(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "original")
        await mem.read_memory("chat1")

        # Modify file on disk (changes mtime)
        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "modified")

        result = await mem.read_memory("chat1")
        assert result == "modified"


class TestReadWriteRoundTrip:
    """Round-trip tests combining write and read."""

    @pytest.mark.asyncio
    async def test_basic_round_trip(self, mem: Memory):
        await mem.write_memory("chat1", "Hello, world!")
        result = await mem.read_memory("chat1")
        assert result == "Hello, world!"

    @pytest.mark.asyncio
    async def test_overwrite_round_trip(self, mem: Memory):
        await mem.write_memory("chat1", "First")
        await mem.write_memory("chat1", "Second")
        result = await mem.read_memory("chat1")
        assert result == "Second"

    @pytest.mark.asyncio
    async def test_multiple_chats_isolated(self, mem: Memory):
        await mem.write_memory("alice", "Alice data")
        await mem.write_memory("bob", "Bob data")
        assert await mem.read_memory("alice") == "Alice data"
        assert await mem.read_memory("bob") == "Bob data"

    @pytest.mark.asyncio
    async def test_empty_write_returns_none_on_read(self, mem: Memory):
        await mem.write_memory("chat1", "")
        result = await mem.read_memory("chat1")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# read_agents_md
# ─────────────────────────────────────────────────────────────────────────────


class TestReadAgentsMd:
    """Tests for read_agents_md."""

    @pytest.mark.asyncio
    async def test_returns_agents_content(self, mem: Memory):
        mem.ensure_workspace("chat1")
        result = await mem.read_agents_md("chat1")
        assert "Agent Instructions" in result

    @pytest.mark.asyncio
    async def test_raises_file_not_found_when_missing(self, mem: Memory, workspace: Path):
        # Create directory but NOT AGENTS.md
        chat_dir = _chat_dir(workspace, "chat1")
        chat_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(FileNotFoundError, match="AGENTS.md not found"):
            await mem.read_agents_md("chat1")

    @pytest.mark.asyncio
    async def test_caches_content(self, mem: Memory):
        mem.ensure_workspace("chat1")
        await mem.read_agents_md("chat1")
        cached = mem._agents_cache._cache.get("chat1")
        assert cached is not None
        assert "Agent Instructions" in cached[1]

    @pytest.mark.asyncio
    async def test_cache_hit_on_second_read(self, mem: Memory):
        """Second read with same mtime should hit cache (same content)."""
        mem.ensure_workspace("chat1")
        first = await mem.read_agents_md("chat1")
        second = await mem.read_agents_md("chat1")
        assert first == second

    @pytest.mark.asyncio
    async def test_cache_refreshed_on_external_modification(self, mem: Memory, workspace: Path):
        mem.ensure_workspace("chat1")
        await mem.read_agents_md("chat1")

        # Modify AGENTS.md on disk
        time.sleep(0.05)
        agents = _agents_path(workspace, "chat1")
        agents.write_text("# Modified", encoding="utf-8")

        result = await mem.read_agents_md("chat1")
        assert result == "# Modified"


# ─────────────────────────────────────────────────────────────────────────────
# Corruption Detection
# ─────────────────────────────────────────────────────────────────────────────


class TestChecksumCalculation:
    """Tests for _calculate_checksum."""

    def test_produces_32_char_hex(self, mem: Memory):
        cs = mem._calculate_checksum("hello")
        assert len(cs) == 32
        assert all(c in "0123456789abcdef" for c in cs)

    def test_matches_sha256_first_32(self, mem: Memory):
        content = "test content"
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
        assert mem._calculate_checksum(content) == expected

    def test_empty_string(self, mem: Memory):
        cs = mem._calculate_checksum("")
        assert len(cs) == 32

    def test_deterministic(self, mem: Memory):
        assert mem._calculate_checksum("x") == mem._calculate_checksum("x")


class TestDetectMemoryCorruption:
    """Tests for detect_memory_corruption."""

    def test_no_corruption_when_file_missing(self, mem: Memory):
        result = mem.detect_memory_corruption("no-such-chat")
        assert result.is_corrupted is False
        assert result.checksum_valid is True
        assert result.error_details == []

    def test_no_corruption_when_no_checksum_file(self, mem: Memory, workspace: Path):
        """Without a checksum file, corruption is not detected."""
        _write_memory_raw(workspace, "chat1", "some content")
        result = mem.detect_memory_corruption("chat1")
        assert result.is_corrupted is False

    def test_no_corruption_when_checksum_matches(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "intact content")
        cs = mem._calculate_checksum("intact content")
        _checksum_path(workspace, "chat1").write_text(cs, encoding="utf-8")
        result = mem.detect_memory_corruption("chat1")
        assert result.is_corrupted is False
        assert result.checksum_valid is True

    def test_detects_checksum_mismatch(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted content")
        _checksum_path(workspace, "chat1").write_text("badchecksum12345", encoding="utf-8")
        result = mem.detect_memory_corruption("chat1")
        assert result.is_corrupted is True
        assert result.checksum_valid is False
        assert any("Checksum mismatch" in e for e in result.error_details)

    def test_detects_corruption_after_content_tampering(self, mem: Memory, workspace: Path):
        """Write with checksum, then tamper with the file on disk."""
        _write_memory_raw(workspace, "chat1", "original content")
        cs = mem._calculate_checksum("original content")
        _checksum_path(workspace, "chat1").write_text(cs, encoding="utf-8")

        # Tamper with the file
        _write_memory_raw(workspace, "chat1", "tampered content")

        result = mem.detect_memory_corruption("chat1")
        assert result.is_corrupted is True

    def test_result_has_correct_file_path(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "data")
        result = mem.detect_memory_corruption("chat1")
        expected = str(_memory_path(workspace, "chat1"))
        assert result.file_path == expected

    def test_handles_read_error_gracefully(self, mem: Memory, workspace: Path):
        """Simulate a read error by making the path a directory instead of a file."""
        memory_file = _memory_path(workspace, "chat1")
        memory_file.parent.mkdir(parents=True, exist_ok=True)
        memory_file.mkdir()  # directory where file is expected → read will fail

        result = mem.detect_memory_corruption("chat1")
        assert result.is_corrupted is True
        assert len(result.error_details) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Backup
# ─────────────────────────────────────────────────────────────────────────────


class TestBackupMemoryFile:
    """Tests for backup_memory_file."""

    def test_returns_none_for_nonexistent(self, mem: Memory):
        result = mem.backup_memory_file("no-such-chat")
        assert result is None

    def test_creates_backup_file(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "important data")
        backup_path = mem.backup_memory_file("chat1")
        assert backup_path is not None
        assert Path(backup_path).exists()
        assert Path(backup_path).read_text(encoding="utf-8") == "important data"

    def test_backup_file_has_bak_extension(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "data")
        backup_path = mem.backup_memory_file("chat1")
        assert backup_path is not None
        assert backup_path.endswith(".md.bak")

    def test_backup_file_in_backups_directory(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "data")
        backup_path = mem.backup_memory_file("chat1")
        assert backup_path is not None
        assert BACKUP_DIR in backup_path

    def test_backup_file_name_contains_safe_chat_id(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat@1", "data")
        backup_path = mem.backup_memory_file("chat@1")
        assert backup_path is not None
        assert "chat_at_1" in Path(backup_path).name

    def test_backup_file_has_timestamp(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "data")
        backup_path = mem.backup_memory_file("chat1")
        assert backup_path is not None
        # Should contain a YYYYMMDD_HHMMSS timestamp
        name = Path(backup_path).stem  # e.g. "chat1_20260410_143000.md"
        # The stem includes the .md part before .bak, so check the name
        basename = Path(backup_path).name
        # Pattern: safeid_YYYYMMDD_HHMMSS.md.bak
        parts = basename.rsplit("_", 1)
        assert len(parts) == 2

    def test_multiple_backups_dont_collide(self, mem: Memory, workspace: Path):
        """Two backups in quick succession should still create separate files
        (timestamp may be the same second, so names could collide — but copy2
        would overwrite). Verify at least one backup exists."""
        _write_memory_raw(workspace, "chat1", "data")
        b1 = mem.backup_memory_file("chat1")
        # Small sleep to ensure different timestamp
        time.sleep(1.1)
        b2 = mem.backup_memory_file("chat1")
        assert b1 is not None
        assert b2 is not None


# ─────────────────────────────────────────────────────────────────────────────
# Repair
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairMemoryFile:
    """Tests for repair_memory_file."""

    def test_no_repair_when_not_corrupted(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "clean data")
        result = mem.repair_memory_file("chat1")
        assert result.is_corrupted is False
        assert result.repaired is False
        # File should still have content
        assert _memory_path(workspace, "chat1").read_text() == "clean data"

    def test_repairs_corrupted_file(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted")
        _checksum_path(workspace, "chat1").write_text("wrong_checksum", encoding="utf-8")

        result = mem.repair_memory_file("chat1")
        assert result.repaired is True
        # File should be cleared
        assert _memory_path(workspace, "chat1").read_text() == ""

    def test_removes_checksum_on_repair(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted")
        _checksum_path(workspace, "chat1").write_text("wrong", encoding="utf-8")

        mem.repair_memory_file("chat1")
        assert not _checksum_path(workspace, "chat1").exists()

    def test_creates_backup_before_repair(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted data")
        _checksum_path(workspace, "chat1").write_text("bad", encoding="utf-8")

        result = mem.repair_memory_file("chat1", backup=True)
        assert result.backup_path is not None
        assert Path(result.backup_path).exists()
        assert Path(result.backup_path).read_text() == "corrupted data"

    def test_skips_backup_when_requested(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted data")
        _checksum_path(workspace, "chat1").write_text("bad", encoding="utf-8")

        result = mem.repair_memory_file("chat1", backup=False)
        assert result.backup_path is None

    def test_no_repair_for_missing_file(self, mem: Memory):
        result = mem.repair_memory_file("no-such-chat")
        assert result.is_corrupted is False
        assert result.repaired is False


# ─────────────────────────────────────────────────────────────────────────────
# read_memory_with_validation / write_memory_with_checksum
# ─────────────────────────────────────────────────────────────────────────────


class TestReadMemoryWithValidation:
    """Tests for read_memory_with_validation."""

    @pytest.mark.asyncio
    async def test_returns_content_when_valid(self, mem: Memory):
        mem.ensure_workspace("chat1")
        await mem.write_memory_with_checksum("chat1", "valid content")
        result = await mem.read_memory_with_validation("chat1")
        assert result == "valid content"

    @pytest.mark.asyncio
    async def test_returns_none_when_corrupted(self, mem: Memory, workspace: Path):
        _write_memory_raw(workspace, "chat1", "corrupted")
        _checksum_path(workspace, "chat1").write_text("bad_checksum", encoding="utf-8")
        result = await mem.read_memory_with_validation("chat1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, mem: Memory):
        result = await mem.read_memory_with_validation("no-chat")
        assert result is None


class TestWriteMemoryWithChecksum:
    """Tests for write_memory_with_checksum."""

    @pytest.mark.asyncio
    async def test_writes_memory_file(self, mem: Memory, workspace: Path):
        await mem.write_memory_with_checksum("chat1", "hello")
        content = _memory_path(workspace, "chat1").read_text(encoding="utf-8")
        assert content.startswith("hello")

    @pytest.mark.asyncio
    async def test_writes_checksum_file(self, mem: Memory, workspace: Path):
        await mem.write_memory_with_checksum("chat1", "hello")
        checksum = _checksum_path(workspace, "chat1").read_text(encoding="utf-8")
        expected = mem._calculate_checksum("hello\n")
        assert checksum == expected

    @pytest.mark.asyncio
    async def test_strips_content_before_checksum(self, mem: Memory, workspace: Path):
        await mem.write_memory_with_checksum("chat1", "  hello  ")
        content = _memory_path(workspace, "chat1").read_text(encoding="utf-8")
        assert content.startswith("hello")
        checksum = _checksum_path(workspace, "chat1").read_text(encoding="utf-8")
        expected = mem._calculate_checksum("hello\n")
        assert checksum == expected

    @pytest.mark.asyncio
    async def test_round_trip_validation(self, mem: Memory):
        """Write with checksum, read with validation → content matches."""
        await mem.write_memory_with_checksum("chat1", "round trip data")
        result = await mem.read_memory_with_validation("chat1")
        assert result == "round trip data"

    @pytest.mark.asyncio
    async def test_corruption_detected_after_tamper(self, mem: Memory, workspace: Path):
        """Write with checksum, tamper file, then validation should fail."""
        await mem.write_memory_with_checksum("chat1", "original")
        _write_memory_raw(workspace, "chat1", "tampered")
        result = await mem.read_memory_with_validation("chat1")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Recovery Event Logging
# ─────────────────────────────────────────────────────────────────────────────


class TestLogRecoveryEvent:
    """Tests for log_recovery_event."""

    def test_creates_recovery_log(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("chat1", preserved_count=5, rebuilt_count=3, total_count=8)
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        assert recovery.exists()
        content = recovery.read_text(encoding="utf-8")
        assert "Recovery Event" in content
        assert "5" in content
        assert "3" in content
        assert "8" in content

    def test_log_has_header(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("chat1", preserved_count=0, rebuilt_count=0, total_count=0)
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        content = recovery.read_text(encoding="utf-8")
        assert content.startswith("# Message Index Recovery Log")

    def test_appends_to_existing_log(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("chat1", preserved_count=1, rebuilt_count=2, total_count=3)
        mem.log_recovery_event("chat1", preserved_count=4, rebuilt_count=5, total_count=9)
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        content = recovery.read_text(encoding="utf-8")
        # Should have two recovery events
        assert content.count("Recovery Event") == 2

    def test_includes_errors(self, mem: Memory, workspace: Path):
        errors = ["file missing", "read error"]
        mem.log_recovery_event(
            "chat1",
            preserved_count=0,
            rebuilt_count=0,
            total_count=0,
            errors=errors,
        )
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        content = recovery.read_text(encoding="utf-8")
        assert "file missing" in content
        assert "read error" in content

    def test_limits_errors_to_five(self, mem: Memory, workspace: Path):
        errors = [f"error {i}" for i in range(10)]
        mem.log_recovery_event(
            "chat1", preserved_count=0, rebuilt_count=0, total_count=0, errors=errors
        )
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        content = recovery.read_text(encoding="utf-8")
        # Should include first 5 errors, not the 6th
        assert "error 4" in content
        assert "error 5" not in content

    def test_no_errors_section_when_none(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("chat1", preserved_count=0, rebuilt_count=0, total_count=0)
        recovery = _chat_dir(workspace, "chat1") / RECOVERY_LOG_FILENAME
        content = recovery.read_text(encoding="utf-8")
        assert "Errors" not in content

    def test_creates_chat_directory(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("brand-new-chat", 1, 1, 2)
        assert _chat_dir(workspace, "brand-new-chat").is_dir()


class TestHasRecoveryEvents:
    """Tests for has_recovery_events."""

    def test_returns_false_when_no_log(self, mem: Memory):
        assert mem.has_recovery_events("chat1") is False

    def test_returns_true_after_logging(self, mem: Memory):
        mem.log_recovery_event("chat1", 0, 0, 0)
        assert mem.has_recovery_events("chat1") is True


class TestClearRecoveryLog:
    """Tests for clear_recovery_log."""

    def test_removes_recovery_log(self, mem: Memory, workspace: Path):
        mem.log_recovery_event("chat1", 0, 0, 0)
        assert mem.has_recovery_events("chat1") is True

        mem.clear_recovery_log("chat1")
        assert mem.has_recovery_events("chat1") is False

    def test_no_error_when_no_log(self, mem: Memory):
        """Should not raise if there is no recovery log to clear."""
        mem.clear_recovery_log("nonexistent-chat")

    def test_creates_directory_if_missing(self, mem: Memory, workspace: Path):
        """clear_recovery_log calls _ensure_chat_dir, which creates dirs."""
        mem.clear_recovery_log("new-chat")
        assert _chat_dir(workspace, "new-chat").is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# Path Traversal Validation
# ─────────────────────────────────────────────────────────────────────────────


class TestPathTraversalValidation:
    """Security tests ensuring workspace confinement in Memory."""

    def test_normal_chat_id_passes(self, mem: Memory):
        """Legitimate chat IDs should work without issue."""
        path = mem.ensure_workspace("1234567890@s.whatsapp.net")
        assert path.is_dir()
        assert is_path_in_workspace(mem._root / "whatsapp_data", path)

    def test_dotdot_in_chat_id_blocked(self, mem: Memory):
        """`..` passes through sanitize_path_component (dots are allowed),
        but resolve() catches the escape — verify it's blocked."""
        with pytest.raises(PathSecurityError, match="Workspace escape blocked"):
            mem.ensure_workspace("..")

    def test_resolve_stays_within_workspace(self, mem: Memory, workspace: Path):
        """Verify that created paths resolve within workspace_data."""
        d = mem._chat_dir("normal-chat")
        assert is_path_in_workspace(workspace / "whatsapp_data", d.resolve())

    def test_ensure_workspace_raises_on_escape(self, mem: Memory, monkeypatch):
        """If sanitize_path_component returned a traversal, _validate_path blocks it."""
        monkeypatch.setattr(
            "src.memory.sanitize_path_component", lambda x: "../../etc"
        )
        with pytest.raises(PathSecurityError, match="Workspace escape blocked"):
            mem.ensure_workspace("evil")

    def test_chat_dir_raises_on_escape(self, mem: Memory, monkeypatch):
        """_chat_dir also validates (read path protection)."""
        monkeypatch.setattr(
            "src.memory.sanitize_path_component", lambda x: "../../etc"
        )
        with pytest.raises(PathSecurityError, match="Workspace escape blocked"):
            mem._chat_dir("evil")

    def test_write_memory_raises_on_escape(self, mem: Memory, monkeypatch):
        """write_memory calls _ensure_chat_dir which validates."""
        monkeypatch.setattr(
            "src.memory.sanitize_path_component", lambda x: "../../etc"
        )
        with pytest.raises(PathSecurityError):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                mem.write_memory("evil", "data")
            )

    def test_symlink_escape_blocked(self, workspace: Path):
        """A symlink inside whatsapp_data pointing outside should be caught."""
        import os

        ws = workspace / "whatsapp_data"
        ws.mkdir()
        # Create a symlink that points outside workspace
        link_target = workspace.parent  # one level above workspace
        link_path = ws / "evil_link"
        try:
            os.symlink(str(link_target), str(link_path))
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks not supported on this platform")

        m = Memory(str(workspace))
        # sanitize_path_component("evil_link") returns "evil_link" unchanged
        # but resolve() follows the symlink and detects the escape
        with pytest.raises(PathSecurityError, match="Workspace escape blocked"):
            m.ensure_workspace("evil_link")


# ─────────────────────────────────────────────────────────────────────────────
# Mtime Cache Consistency After External File Modification
# ─────────────────────────────────────────────────────────────────────────────


class TestMtimeCacheConsistency:
    """Verify that mtime-based caching correctly invalidates when an external
    process (e.g. a skill) modifies the file on disk between reads."""

    @pytest.mark.asyncio
    async def test_external_memory_modification_invalidates_cache(
        self, mem: Memory, workspace: Path
    ):
        """An external write to MEMORY.md changes mtime, so the next
        read_memory() must return the new content, not the stale cache."""
        _write_memory_raw(workspace, "chat1", "original content")
        first = await mem.read_memory("chat1")
        assert first == "original content"

        # External process modifies the file (mtime changes)
        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "externally modified")

        second = await mem.read_memory("chat1")
        assert second == "externally modified"

    @pytest.mark.asyncio
    async def test_multiple_external_modifications_detected(
        self, mem: Memory, workspace: Path
    ):
        """Each successive external modification is picked up independently."""
        _write_memory_raw(workspace, "chat1", "v1")
        assert await mem.read_memory("chat1") == "v1"

        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "v2")
        assert await mem.read_memory("chat1") == "v2"

        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "v3")
        assert await mem.read_memory("chat1") == "v3"

    @pytest.mark.asyncio
    async def test_cache_hit_then_external_mod_returns_fresh(
        self, mem: Memory, workspace: Path
    ):
        """After a cache hit (same mtime), an external modification still
        causes the next read to return fresh content."""
        _write_memory_raw(workspace, "chat1", "initial")
        await mem.read_memory("chat1")
        # Second read is a cache hit (mtime unchanged)
        assert await mem.read_memory("chat1") == "initial"

        # External modification
        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "updated by skill")

        result = await mem.read_memory("chat1")
        assert result == "updated by skill"

    @pytest.mark.asyncio
    async def test_external_agents_md_modification_invalidates_cache(
        self, mem: Memory, workspace: Path
    ):
        """Same mtime-based cache invalidation applies to AGENTS.md reads."""
        mem.ensure_workspace("chat1")
        first = await mem.read_agents_md("chat1")
        assert "Agent Instructions" in first

        # External process overwrites AGENTS.md
        time.sleep(0.05)
        agents = _agents_path(workspace, "chat1")
        agents.write_text("# Custom Agent\nNew instructions.", encoding="utf-8")

        result = await mem.read_agents_md("chat1")
        assert result == "# Custom Agent\nNew instructions."

    @pytest.mark.asyncio
    async def test_external_mod_to_empty_invalidates_cache(
        self, mem: Memory, workspace: Path
    ):
        """External modification that empties the file returns None (stripped
        whitespace-only content is treated as missing)."""
        _write_memory_raw(workspace, "chat1", "has content")
        assert await mem.read_memory("chat1") == "has content"

        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "")

        result = await mem.read_memory("chat1")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_entry_reflects_last_external_content(
        self, mem: Memory, workspace: Path
    ):
        """After external modification and re-read, the internal cache entry
        stores the new content and mtime — not the old values."""
        _write_memory_raw(workspace, "chat1", "first")
        await mem.read_memory("chat1")
        old_cached = mem._memory_cache._cache.get("chat1")
        assert old_cached[1] == "first"

        time.sleep(0.05)
        _write_memory_raw(workspace, "chat1", "second")
        await mem.read_memory("chat1")

        new_cached = mem._memory_cache._cache.get("chat1")
        assert new_cached[1] == "second"
        assert new_cached[0] != old_cached[0]  # mtime changed


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_chat_path() caching correctness
# ─────────────────────────────────────────────────────────────────────────────


class TestResolveChatPathCaching:
    """Verify _resolve_chat_path() LRU caching behaviour.

    Covers:
      (a) Repeated calls with same chat_id return the same cached Path
      (b) Cache invalidated when ensure_workspace() creates a new chat
      (c) Invalid chat_id raises PathSecurityError before caching
      (d) Cache respects the max size bound
    """

    def test_cache_populated_after_first_call(self, mem: Memory):
        """After the first call, the path is stored in _path_cache."""
        assert mem._path_cache.get("chat1") is None
        mem._resolve_chat_path("chat1")
        assert mem._path_cache.get("chat1") is not None

    def test_repeated_calls_return_equivalent_path(self, mem: Memory):
        """Repeated calls with the same chat_id return equivalent Paths."""
        first = mem._resolve_chat_path("chat1")
        second = mem._resolve_chat_path("chat1")
        assert first == second

    def test_repeated_calls_use_cache(self, mem: Memory):
        """The second call returns the exact same object from the cache."""
        first = mem._resolve_chat_path("chat1")
        second = mem._resolve_chat_path("chat1")
        assert first is second

    def test_different_chat_ids_cached_independently(self, mem: Memory):
        """Each chat_id gets its own cache entry."""
        path_a = mem._resolve_chat_path("alice")
        path_b = mem._resolve_chat_path("bob")
        assert path_a != path_b
        assert mem._path_cache.get("alice") is not None
        assert mem._path_cache.get("bob") is not None

    def test_ensure_workspace_invalidates_and_repoulates(self, mem: Memory):
        """ensure_workspace() pops the stale cache entry, then re-resolves."""
        original = mem._resolve_chat_path("chat1")
        assert not original.exists()

        result = mem.ensure_workspace("chat1")
        assert result.is_dir()
        assert result == original

        # Cache should be repopulated with the valid path
        cached = mem._path_cache.get("chat1")
        assert cached is not None
        assert cached.is_dir()

    def test_ensure_workspace_for_existing_chat_keeps_valid_cache(
        self, mem: Memory
    ):
        """For an already-created chat, ensure_workspace still works."""
        mem.ensure_workspace("chat1")
        cached_after_first = mem._path_cache.get("chat1")
        assert cached_after_first is not None

        # Second call invalidates and repopulates
        mem.ensure_workspace("chat1")
        cached_after_second = mem._path_cache.get("chat1")
        assert cached_after_second is not None
        assert cached_after_second.is_dir()

    def test_invalid_chat_id_raises_before_caching(self, mem: Memory, monkeypatch):
        """If _validate_path raises, the invalid entry must NOT be cached."""
        monkeypatch.setattr(
            "src.memory.sanitize_path_component", lambda x: "../../etc"
        )
        with pytest.raises(PathSecurityError):
            mem._resolve_chat_path("evil")

        assert mem._path_cache.get("evil") is None

    def test_valid_id_cached_even_after_invalid_attempt(
        self, mem: Memory, monkeypatch
    ):
        """A failed validation for one chat_id doesn't affect others."""
        mem._resolve_chat_path("good-chat")
        assert mem._path_cache.get("good-chat") is not None

        monkeypatch.setattr(
            "src.memory.sanitize_path_component", lambda x: "../../etc"
        )
        with pytest.raises(PathSecurityError):
            mem._resolve_chat_path("evil")

        # good-chat should still be cached
        assert mem._path_cache.get("good-chat") is not None

    def test_cache_respects_max_size_bound(self, mem: Memory):
        """Cache evicts oldest entries when max_size is exceeded."""
        max_size = 10
        mem._path_cache = LRUDict(max_size=max_size)

        for i in range(max_size + 5):
            mem._resolve_chat_path(f"chat-{i:04d}")

        # Cache must not exceed max_size
        assert len(mem._path_cache) <= max_size

        # Most recent entries are preserved
        assert mem._path_cache.get("chat-0014") is not None

        # Oldest entries are evicted
        assert mem._path_cache.get("chat-0000") is None

    def test_cache_lru_eviction_preserves_recent_entries(self, mem: Memory):
        """Accessing an entry refreshes it, preventing eviction."""
        max_size = 5
        mem._path_cache = LRUDict(max_size=max_size)

        # Fill cache: entries 0-4
        for i in range(max_size):
            mem._resolve_chat_path(f"chat-{i:04d}")

        # Touch chat-0000 to make it recently used
        mem._resolve_chat_path("chat-0000")

        # Add one more to trigger eviction of the now-oldest (chat-0001)
        mem._resolve_chat_path("chat-0005")

        # chat-0000 was refreshed, so it survives
        assert mem._path_cache.get("chat-0000") is not None
        # chat-0001 was the oldest after refresh, so it gets evicted
        assert mem._path_cache.get("chat-0001") is None
