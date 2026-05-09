"""
memory.versioning — Track memory mutations with version history for rollback.

Stores version snapshots with diffs in per-chat JSON files under
workspace/.data/memory_versions/.  Max 50 versions per chat (FIFO eviction).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MAX_VERSIONS_PER_CHAT = 50


@dataclass(slots=True)
class VersionInfo:
    """Metadata for a single memory version."""

    version: int
    timestamp: str
    diff: dict[str, list[str]]


def _compute_diff(old_lines: list[str], new_lines: list[str]) -> dict[str, list[str]]:
    """Compute a simple line-level diff between two versions."""
    old_set = set(old_lines)
    new_set = set(new_lines)
    return {
        "added": [line for line in new_lines if line not in old_set],
        "removed": [line for line in old_lines if line not in new_set],
        "modified": [],
    }


class MemoryVersionManager:
    """Version-tracked memory store with rollback support."""

    def __init__(self, data_dir: str) -> None:
        self._versions_dir = Path(data_dir) / "memory_versions"
        self._versions_dir.mkdir(parents=True, exist_ok=True)
        # In-memory cache: {chat_id: [VersionInfo, ...]}
        self._cache: dict[str, list[VersionInfo]] = {}

    def _version_path(self, chat_id: str) -> Path:
        safe_id = chat_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._versions_dir / f"{safe_id}.json"

    def _load_versions(self, chat_id: str) -> list[VersionInfo]:
        """Load version history for a chat from disk."""
        if chat_id in self._cache:
            return self._cache[chat_id]

        path = self._version_path(chat_id)
        if not path.exists():
            self._cache[chat_id] = []
            return []

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load versions for %s: %s", chat_id, exc)
            self._cache[chat_id] = []
            return []

        versions = [
            VersionInfo(
                version=v["version"],
                timestamp=v["timestamp"],
                diff=v.get("diff", {}),
            )
            for v in raw
        ]
        self._cache[chat_id] = versions
        return versions

    def _save_versions(self, chat_id: str, versions: list[VersionInfo]) -> None:
        """Persist version history to disk."""
        data = [
            {
                "version": v.version,
                "timestamp": v.timestamp,
                "diff": v.diff,
            }
            for v in versions
        ]
        path = self._version_path(chat_id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._cache[chat_id] = versions

    def checkpoint(self, chat_id: str, old_content: str, new_content: str) -> int:
        """Create a version snapshot. Returns the new version number."""
        versions = self._load_versions(chat_id)
        next_version = (versions[-1].version + 1) if versions else 1

        old_lines = [line for line in old_content.splitlines() if line.strip()]
        new_lines = [line for line in new_content.splitlines() if line.strip()]
        diff = _compute_diff(old_lines, new_lines)

        info = VersionInfo(
            version=next_version,
            timestamp=datetime.now(timezone.utc).isoformat(),
            diff=diff,
        )
        versions.append(info)

        # FIFO eviction
        if len(versions) > MAX_VERSIONS_PER_CHAT:
            versions = versions[-MAX_VERSIONS_PER_CHAT:]

        self._save_versions(chat_id, versions)
        log.debug("Checkpoint v%d for chat %s", next_version, chat_id)
        return next_version

    def get_history(self, chat_id: str, limit: int = 10) -> list[VersionInfo]:
        """Return recent version history (newest first)."""
        versions = self._load_versions(chat_id)
        return list(reversed(versions[-limit:]))

    def rollback(self, chat_id: str, version: int, current_content: str) -> str | None:
        """Roll back to a specific version.

        Applies the inverse diff to reconstruct the target version.
        Returns the restored content or None on failure.
        """
        versions = self._load_versions(chat_id)
        target_idx = None
        for i, v in enumerate(versions):
            if v.version == version:
                target_idx = i
                break

        if target_idx is None:
            log.warning("Version %d not found for chat %s", version, chat_id)
            return None

        # Replay diffs: start from target and undo each subsequent version
        content_lines = set()
        # Collect all lines that existed up to the target version
        for v in versions[: target_idx + 1]:
            content_lines.update(v.diff.get("added", []))

        # Remove lines added after the target
        for v in versions[target_idx + 1 :]:
            content_lines.difference_update(v.diff.get("added", []))
            # Restore removed lines — only if they were removed after target
            content_lines.update(v.diff.get("removed", []))

        result = "\n".join(sorted(content_lines))
        log.info("Rolled back chat %s to version %d", chat_id, version)
        return result
