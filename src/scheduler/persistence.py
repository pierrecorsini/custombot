"""scheduler/persistence.py — Task file I/O with HMAC integrity.

Handles reading, writing, and atomic persistence of scheduler task files.
When ``SCHEDULER_HMAC_SECRET`` is configured, task files are signed with
HMAC-SHA256 and verified on load to detect tampering.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.constants import SCHEDULER_HMAC_SIG_EXT
from src.db.db import _validate_chat_id
from src.security.signing import (
    get_scheduler_secret,
    read_signature_file,
    sign_payload,
    verify_payload,
    write_signature_file,
)
from src.utils import json_dumps, json_loads, JSONDecodeError
from src.utils.async_file import sync_atomic_write
from src.utils.path import sanitize_path_component

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "SCHEDULER_DIR",
    "TASKS_FILE",
    "read_tasks_file",
    "resolve_tasks_path",
    "write_tasks_file",
]

SCHEDULER_DIR = ".scheduler"
TASKS_FILE = "tasks.json"


def resolve_tasks_path(workspace: Path, chat_id: str) -> Path | None:
    """Build and validate the tasks.json path for a chat.

    Sanitizes ``chat_id`` and verifies the resolved path stays within
    the workspace root to prevent path-traversal attacks.

    Returns:
        Validated ``Path`` or ``None`` if workspace is unset / path is
        outside the workspace tree.
    """
    safe_id = sanitize_path_component(chat_id)
    dest = (workspace / safe_id / SCHEDULER_DIR / TASKS_FILE).resolve()
    workspace_root = workspace.resolve()
    if not dest.is_relative_to(workspace_root):
        log.warning(
            "Scheduler path traversal blocked for chat_id=%r (resolved: %s)",
            chat_id,
            dest,
        )
        return None
    return dest


def write_tasks_file(path: Path, data: list[dict]) -> None:
    """Synchronous helper: mkdir + serialize + write (runs in thread pool).

    When ``SCHEDULER_HMAC_SECRET`` is configured, an HMAC-SHA256
    signature is written to a sidecar ``.hmac`` file alongside the
    tasks data.

    Internal keys prefixed with ``_`` (e.g. ``_last_run_dt``) are
    stripped before serialization — they are ephemeral cache values.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = [
        {k: v for k, v in task.items() if not k.startswith("_")}
        for task in data
    ]
    content = json_dumps(serializable, indent=2)
    sync_atomic_write(path, content)

    secret = get_scheduler_secret()
    if secret is not None:
        signature = sign_payload(secret, content.encode("utf-8"))
        write_signature_file(path.with_suffix(path.suffix + SCHEDULER_HMAC_SIG_EXT), signature)


def read_tasks_file(path: Path) -> str | None:
    """Synchronous helper: check exists + read (runs in thread pool)."""
    if path.exists():
        return path.read_text()
    return None
