"""
utils/path.py — Filesystem path safety utilities.

Provides sanitization functions for converting chat IDs into
filesystem-safe directory/file names.

Security model (three-layer defense-in-depth):

    Layer 1 — Input validation (entry points):
        IncomingMessage.__post_init__() validates chat_id format at the
        message boundary.  Bot.process_scheduled() and
        TaskScheduler.add_task() validate before any processing begins.
        QueuedMessage.from_dict() validates at the deserialization
        boundary to catch tampered queue files.

    Layer 2 — Path sanitization (filesystem operations):
        sanitize_path_component() transforms chat_id into a safe
        filesystem component.  Every code path that constructs a path
        from chat_id calls this function (or a wrapper like
        MessageStore.message_file() or Memory._resolve_chat_path()).

    Layer 3 — Workspace confinement (escape prevention):
        is_path_in_workspace() / Path.is_relative_to() verify that
        the resolved path stays within the workspace root, preventing
        directory traversal even if earlier layers are bypassed.
"""

from __future__ import annotations

import functools

# Module-level constant to avoid re-allocation on every sanitize call.
SANITIZE_MAP: dict[str, str] = {
    "@": "_at_",
    ":": "_col_",
    "/": "_sl_",
    "\\": "_bs_",
    "|": "_pi_",
    "?": "_qm_",
    "*": "_as_",
    "<": "_lt_",
    ">": "_gt_",
    '"': "_dq_",
}


@functools.lru_cache(maxsize=1024)
def sanitize_path_component(chat_id: str) -> str:
    """Strip characters that are unsafe in filesystem paths.

    Uses a deterministic replacement map so that workspace directories
    and message files use consistent names across the application.

    Args:
        chat_id: Raw chat identifier (may contain @, :, /, etc.)

    Returns:
        Filesystem-safe string suitable for directory/file names.

    Raises:
        ValueError: If chat_id is empty or only whitespace.
    """
    if not chat_id:
        raise ValueError("chat_id must not be empty")

    result = chat_id
    for char, replacement in SANITIZE_MAP.items():
        result = result.replace(char, replacement)
    # Replace any remaining non-alphanumeric characters (except -_. and the replacements above)
    result = "".join(c if c.isalnum() or c in "-_." else "_" for c in result)
    # Strip leading/trailing dots and underscores to prevent path traversal
    # (e.g. ".." or "." as directory names) and ensure clean filenames.
    result = result.strip("._ ")
    # Truncate to 200 chars to stay within filesystem limits (255 bytes)
    result = result[:200]
    if not result:
        raise ValueError(
            "chat_id must not resolve to an empty path component "
            f"(input was {chat_id!r})"
        )
    return result
