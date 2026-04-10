"""
async_file.py — Non-blocking file I/O utilities.

Uses asyncio.to_thread to run blocking file operations in a thread pool,
preventing event loop blocking.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


async def async_read_text(path: PathLike, encoding: str = "utf-8") -> str:
    """
    Read text file asynchronously.

    Args:
        path: File path (str or Path object)
        encoding: Text encoding (default: utf-8)

    Returns:
        File contents as string

    Raises:
        FileNotFoundError: If file doesn't exist
        PermissionError: If lacking read permissions
        UnicodeDecodeError: If encoding fails
    """
    path = Path(path)
    return await asyncio.to_thread(path.read_text, encoding=encoding)


async def async_write_text(
    path: PathLike, content: str, encoding: str = "utf-8"
) -> int:
    """
    Write text file asynchronously. Creates parent directories if needed.

    Args:
        path: File path (str or Path object)
        content: Text content to write
        encoding: Text encoding (default: utf-8)

    Returns:
        Number of characters written

    Raises:
        PermissionError: If lacking write permissions
        OSError: If filesystem errors occur
    """
    path = Path(path)

    def _write() -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)
        return len(content)

    return await asyncio.to_thread(_write)


async def async_append_text(
    path: PathLike, content: str, encoding: str = "utf-8"
) -> int:
    """
    Append text to file asynchronously. Creates file and parent dirs if needed.

    Args:
        path: File path (str or Path object)
        content: Text content to append
        encoding: Text encoding (default: utf-8)

    Returns:
        Number of characters appended

    Raises:
        PermissionError: If lacking write permissions
        OSError: If filesystem errors occur
    """
    path = Path(path)

    def _append() -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding=encoding) as f:
            f.write(content)
        return len(content)

    return await asyncio.to_thread(_append)


async def async_read_bytes(path: PathLike) -> bytes:
    """
    Read binary file asynchronously.

    Args:
        path: File path (str or Path object)

    Returns:
        File contents as bytes

    Raises:
        FileNotFoundError: If file doesn't exist
        PermissionError: If lacking read permissions
    """
    path = Path(path)
    return await asyncio.to_thread(path.read_bytes)


async def async_exists(path: PathLike) -> bool:
    """
    Check if path exists asynchronously.

    Args:
        path: File path (str or Path object)

    Returns:
        True if path exists, False otherwise
    """
    path = Path(path)
    return await asyncio.to_thread(path.exists)
