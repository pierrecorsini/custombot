"""
mmap_reader.py — Memory-mapped file reader for large JSONL stores.

For files exceeding MMAP_THRESHOLD_BYTES, uses mmap to avoid loading the
entire file into memory.  Falls back to standard line-by-line reading for
small files or when mmap is unavailable (e.g. unsupported filesystem).

On Windows, mmap requires proper file-handle management — the context
manager pattern ensures handles are released promptly.
"""

from __future__ import annotations

import logging
import mmap
from pathlib import Path

log = logging.getLogger(__name__)

# Files larger than this use mmap for reading.
MMAP_THRESHOLD_BYTES: int = 1_048_576  # 1 MB


class MmapLineReader:
    """Read lines from a file using memory-mapped I/O for large files.

    For files > MMAP_THRESHOLD_BYTES, maps the file into virtual memory
    and iterates line-by-line without loading the entire contents into a
    Python string.  Falls back to standard open() for small files.
    """

    def __init__(self, threshold: int = MMAP_THRESHOLD_BYTES) -> None:
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        return self._threshold

    def read_lines(self, path: Path) -> list[str]:
        """Read all lines from *path*, choosing mmap or regular I/O.

        Returns lines without trailing newlines.  Empty lines are preserved.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return []

        if size < self._threshold:
            return self._read_regular(path)

        lines = list(self._iter_mmap_lines(path))
        if lines is not None:
            return lines

        # mmap failed — fall back to regular reading
        log.debug("mmap failed for %s, falling back to regular read", path)
        return self._read_regular(path)

    def _read_regular(self, path: Path) -> list[str]:
        """Standard line-by-line file read (small files)."""
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _iter_mmap_lines(self, path: Path) -> list[str] | None:
        """Memory-mapped line iteration.  Returns None on failure."""
        try:
            with path.open("rb") as f:
                # tag:ACCESS_READ is the default; explicit for clarity
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            return None

        with mm:
            data = mm.read()
            if isinstance(data, memoryview):
                data = bytes(data)
            return data.decode("utf-8", errors="replace").splitlines()


__all__ = ["MmapLineReader", "MMAP_THRESHOLD_BYTES"]
