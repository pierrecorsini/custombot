"""
utils/path.py — Filesystem path safety utilities.

Provides sanitization functions for converting chat IDs into
filesystem-safe directory/file names.
"""

from __future__ import annotations

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
    # Truncate to 200 chars to stay within filesystem limits (255 bytes)
    return result[:200]
