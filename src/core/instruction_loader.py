"""
src/core/instruction_loader.py — Instruction file loading with mtime cache.

Loads instruction files from the instructions directory with mtime-based
caching to avoid repeated disk reads. Automatically strips YAML frontmatter
from the returned content (frontmatter is consumed by the routing engine,
not by the LLM).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from src.security import PathSecurityError, is_path_in_workspace
from src.utils.frontmatter import parse_frontmatter

log = logging.getLogger(__name__)


class InstructionLoader:
    """Loads instruction files with mtime-based caching and frontmatter stripping."""

    def __init__(self, instructions_dir: Path) -> None:
        self._dir = instructions_dir
        self._cache: dict[str, tuple[float, str]] = {}

    def _validate_path(self, target: Path, filename: str) -> None:
        """Validate that a file path stays within the instructions directory."""
        # Reject filenames with directory components (path traversal / absolute paths)
        if filename != Path(filename).name:
            log.warning("Path traversal attempt in instruction loader: %r", filename)
            raise PathSecurityError(
                f"Path traversal blocked: {filename!r}",
                path=filename,
                reason="path_traversal",
            )
        # Verify resolved path stays within instructions directory
        if not is_path_in_workspace(self._dir, target.resolve()):
            log.warning("Instruction path escapes directory: %r", filename)
            raise PathSecurityError(
                f"Instruction path escape blocked: {filename!r}",
                path=filename,
                reason="path_traversal",
            )

    def load(self, filename: str) -> str:
        """
        Load instruction content with mtime-based cache.

        Strips YAML frontmatter from the returned content so only the
        instruction body is passed to the LLM.

        Exits the program if the instruction file is not found.

        Args:
            filename: Instruction filename (e.g., 'chat.agent.md').
        """
        safe_filename = Path(filename).name
        path = self._dir / safe_filename
        self._validate_path(path, filename)

        if not (path.exists() and path.is_file()):
            log.critical(
                "Instruction file not found: %s (referenced by routing rule). Aborting.",
                path,
            )
            raise FileNotFoundError(f"Instruction file not found: {path}")

        mtime = path.stat().st_mtime
        cached = self._cache.get(safe_filename)
        if cached and cached[0] == mtime:
            return cached[1]

        raw = path.read_text(encoding="utf-8")
        parsed = parse_frontmatter(raw)
        content = parsed.content.strip()

        self._cache[safe_filename] = (mtime, content)
        log.debug("Loaded instruction from %s", path)
        return content

    def load_raw(self, filename: str) -> Optional[str]:
        """
        Load raw file content without caching or frontmatter stripping.

        Returns None if the file does not exist (no exit on missing).
        Useful for the routing CRUD skills that need to read/write full files.

        Args:
            filename: Instruction filename (e.g., 'chat.agent.md').

        Returns:
            Raw file content, or None if file not found.
        """
        safe_filename = Path(filename).name
        path = self._dir / safe_filename
        self._validate_path(path, filename)

        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def save(self, filename: str, content: str) -> None:
        """
        Write content to an instruction file.

        Args:
            filename: Instruction filename (e.g., 'chat.agent.md').
            content: Full file content (including frontmatter).
        """
        safe_filename = Path(filename).name
        path = self._dir / safe_filename
        self._validate_path(path, filename)
        path.write_text(content, encoding="utf-8")
        # Invalidate cache
        self._cache.pop(safe_filename, None)
        log.debug("Saved instruction file: %s", path)

    def delete(self, filename: str) -> bool:
        """
        Delete an instruction file.

        Args:
            filename: Instruction filename (e.g., 'chat.agent.md').

        Returns:
            True if the file was deleted, False if it didn't exist.
        """
        safe_filename = Path(filename).name
        path = self._dir / safe_filename
        self._validate_path(path, filename)

        if not path.exists():
            return False

        path.unlink()
        self._cache.pop(safe_filename, None)
        log.debug("Deleted instruction file: %s", path)
        return True

    def list_files(self) -> list[str]:
        """
        List all .md instruction files in the directory.

        Returns:
            Sorted list of filenames.
        """
        if not self._dir.is_dir():
            return []
        return sorted(f.name for f in self._dir.glob("*.md"))

    def invalidate(self, filename: str) -> None:
        """Remove a file from the cache (use after external modification)."""
        safe_filename = Path(filename).name
        self._cache.pop(safe_filename, None)
