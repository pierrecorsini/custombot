"""
src/ui/branching.py — Conversation branching from a previous point.

Allows users to fork a conversation from an earlier message, creating
an alternate timeline stored under a branched chat_id.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.constants import WORKSPACE_DIR

log = logging.getLogger(__name__)

BRANCHES_FILENAME = "branches.json"


@dataclass(slots=True, frozen=True)
class BranchInfo:
    """Metadata for a conversation branch."""

    branch_id: str
    from_message_id: str
    created_at: float
    label: str = ""


class ConversationBrancher:
    """Create and manage conversation branches."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is None:
            data_dir = Path(WORKSPACE_DIR) / ".data"
        self._data_dir = Path(data_dir)
        self._branches_file = self._data_dir / BRANCHES_FILENAME
        # {chat_id: [BranchInfo dicts]}
        self._branches: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        """Load branch metadata from disk."""
        if not self._branches_file.exists():
            return
        try:
            raw = self._branches_file.read_text(encoding="utf-8")
            self._branches = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load branches metadata: %s", exc)
            self._branches = {}

    def _save(self) -> None:
        """Persist branch metadata to disk."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._branches_file.write_text(
                json.dumps(self._branches, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Failed to save branches metadata: %s", exc)

    @staticmethod
    def _next_counter(branches: list[dict[str, Any]]) -> int:
        """Determine the next branch number."""
        if not branches:
            return 1
        return max(b.get("number", 0) for b in branches) + 1

    def create_branch(self, chat_id: str, from_message_id: str) -> str:
        """Create a new branch from a message in the conversation.

        Args:
            chat_id: Original chat identifier.
            from_message_id: Message ID to branch from.

        Returns:
            The new branched chat_id.
        """
        if chat_id not in self._branches:
            self._branches[chat_id] = []

        entries = self._branches[chat_id]
        num = self._next_counter(entries)
        branch_id = f"{chat_id}_branch_{num}"

        entry: dict[str, Any] = {
            "branch_id": branch_id,
            "number": num,
            "from_message_id": from_message_id,
            "created_at": time.time(),
        }
        entries.append(entry)
        self._save()

        log.info("Created branch %s from chat %s at message %s", branch_id, chat_id, from_message_id)
        return branch_id

    def list_branches(self, chat_id: str) -> list[BranchInfo]:
        """List all branches for a conversation.

        Args:
            chat_id: Original chat identifier.

        Returns:
            List of BranchInfo describing each branch.
        """
        entries = self._branches.get(chat_id, [])
        return [
            BranchInfo(
                branch_id=e["branch_id"],
                from_message_id=e["from_message_id"],
                created_at=e["created_at"],
                label=f"Branch {e['number']}",
            )
            for e in entries
        ]

    def switch_to_branch(
        self,
        chat_id: str,
        branch_id: str,
    ) -> list[dict[str, Any]]:
        """Return the branch identifier and metadata for switching.

        The caller (bot layer) is responsible for actually loading the
        branched conversation history from the database using the
        returned branch_id as the effective chat_id.

        Args:
            chat_id: Original chat identifier.
            branch_id: Target branch identifier.

        Returns:
            List of message dicts from the branch point.

        Raises:
            ValueError: If the branch does not exist.
        """
        entries = self._branches.get(chat_id, [])
        for entry in entries:
            if entry["branch_id"] == branch_id:
                log.info("Switching to branch %s in chat %s", branch_id, chat_id)
                return [
                    {
                        "branch_id": branch_id,
                        "from_message_id": entry["from_message_id"],
                        "created_at": entry["created_at"],
                    }
                ]
        raise ValueError(f"Branch {branch_id!r} not found for chat {chat_id!r}")
