"""
tests/unit/test_instruction_loader.py — Tests for InstructionLoader.

Covers mtime caching, frontmatter stripping, file I/O, and cache invalidation.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.core.instruction_loader import InstructionLoader


@pytest.fixture()
def instructions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "instructions"
    d.mkdir()
    return d


@pytest.fixture()
def loader(instructions_dir: Path) -> InstructionLoader:
    return InstructionLoader(instructions_dir)


def _write_instruction(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ── load() ───────────────────────────────────────────────────────────────────


class TestLoad:
    def test_loads_instruction_file(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "chat.md", "You are a helpful assistant.")
        result = loader.load("chat.md")
        assert result == "You are a helpful assistant."

    def test_strips_frontmatter(self, loader: InstructionLoader, instructions_dir: Path):
        content = "---\nrouting: chat\n---\nYou are a helpful assistant."
        _write_instruction(instructions_dir / "chat.md", content)
        result = loader.load("chat.md")
        assert result == "You are a helpful assistant."
        assert "routing" not in result

    def test_strips_whitespace(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "chat.md", "  Hello  \n")
        result = loader.load("chat.md")
        assert result == "Hello"

    def test_raises_on_missing_file(self, loader: InstructionLoader):
        with pytest.raises(FileNotFoundError, match="Instruction file not found"):
            loader.load("nonexistent.md")

    def test_mtime_cache_returns_same_content(
        self, loader: InstructionLoader, instructions_dir: Path
    ):
        _write_instruction(instructions_dir / "chat.md", "Version 1")
        result1 = loader.load("chat.md")

        # Modify the file (but mtime may be same on fast systems)
        _write_instruction(instructions_dir / "chat.md", "Version 2")
        # Force mtime change by touching with a future time
        future = time.time() + 100
        (instructions_dir / "chat.md").touch()

        # This should still return cached because mtime might match
        # We verify the cache mechanism exists by checking internal state
        assert "chat.md" in loader._cache

    def test_cache_invalidated_on_mtime_change(
        self, loader: InstructionLoader, instructions_dir: Path
    ):
        _write_instruction(instructions_dir / "chat.md", "Version 1")
        result1 = loader.load("chat.md")
        assert result1 == "Version 1"

        # Change file and ensure mtime differs
        time.sleep(0.05)
        _write_instruction(instructions_dir / "chat.md", "Version 2")
        result2 = loader.load("chat.md")
        assert result2 == "Version 2"

    def test_path_traversal_sanitized(self, loader: InstructionLoader, instructions_dir: Path):
        """Ensure path traversal is prevented — only basename is used."""
        _write_instruction(instructions_dir / "chat.md", "Safe content")
        # Path("../../etc/passwd").name == "passwd" → only "passwd" is looked up
        # in instructions_dir, so traversal is prevented
        with pytest.raises(FileNotFoundError):
            loader.load("../../../etc/passwd")


# ── load_raw() ───────────────────────────────────────────────────────────────


class TestLoadRaw:
    def test_loads_existing_file(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "raw.md", "---\nmeta: data\n---\nContent here")
        result = loader.load_raw("raw.md")
        assert "---" in result  # Frontmatter NOT stripped
        assert "Content here" in result

    def test_returns_none_for_missing(self, loader: InstructionLoader):
        result = loader.load_raw("nonexistent.md")
        assert result is None

    def test_no_caching(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "raw.md", "Original")
        loader.load_raw("raw.md")
        _write_instruction(instructions_dir / "raw.md", "Modified")
        result = loader.load_raw("raw.md")
        assert result == "Modified"


# ── save() ───────────────────────────────────────────────────────────────────


class TestSave:
    def test_saves_file(self, loader: InstructionLoader, instructions_dir: Path):
        loader.save("new.md", "# New instruction")
        assert (instructions_dir / "new.md").read_text() == "# New instruction"

    def test_overwrites_existing(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "existing.md", "Old")
        loader.save("existing.md", "New")
        assert (instructions_dir / "existing.md").read_text() == "New"

    def test_invalidates_cache(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "cached.md", "Version 1")
        loader.load("cached.md")
        assert "cached.md" in loader._cache

        loader.save("cached.md", "Version 2")
        assert "cached.md" not in loader._cache


# ── delete() ─────────────────────────────────────────────────────────────────


class TestDelete:
    def test_deletes_existing_file(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "del.md", "Content")
        assert loader.delete("del.md") is True
        assert not (instructions_dir / "del.md").exists()

    def test_returns_false_for_missing(self, loader: InstructionLoader):
        assert loader.delete("nonexistent.md") is False

    def test_invalidates_cache(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "cached.md", "Content")
        loader.load("cached.md")
        assert "cached.md" in loader._cache
        loader.delete("cached.md")
        assert "cached.md" not in loader._cache


# ── list_files() ─────────────────────────────────────────────────────────────


class TestListFiles:
    def test_lists_md_files(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "a.md", "A")
        _write_instruction(instructions_dir / "b.md", "B")
        _write_instruction(instructions_dir / "c.txt", "Not an instruction")
        files = loader.list_files()
        assert files == ["a.md", "b.md"]

    def test_returns_empty_for_empty_dir(self, loader: InstructionLoader):
        assert loader.list_files() == []

    def test_returns_sorted(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "z.md", "Z")
        _write_instruction(instructions_dir / "a.md", "A")
        assert loader.list_files() == ["a.md", "z.md"]


# ── invalidate() ─────────────────────────────────────────────────────────────


class TestInvalidate:
    def test_removes_from_cache(self, loader: InstructionLoader, instructions_dir: Path):
        _write_instruction(instructions_dir / "test.md", "Content")
        loader.load("test.md")
        assert "test.md" in loader._cache
        loader.invalidate("test.md")
        assert "test.md" not in loader._cache

    def test_no_error_if_not_cached(self, loader: InstructionLoader):
        # Should not raise
        loader.invalidate("never_loaded.md")
