"""
src/skills/builtin/files.py — File I/O skills operating inside the workspace.

All paths are resolved relative to workspace_dir (the per-chat sandbox).
Attempts to escape via ".." or absolute paths are blocked.

Security measures:
- Path traversal prevention (delegated to src.security.path_validator)
- File size limits (read: 64KB, write: 1MB)
- Audit logging for file operations
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from src.skills.base import BaseSkill, validate_input
from src.security.path_validator import validate_path, PathSecurityError

log = logging.getLogger(__name__)

# Maximum file sizes for safety
_MAX_READ_BYTES = 64 * 1024  # 64 KB
_MAX_WRITE_BYTES = 1 * 1024 * 1024  # 1 MB


def _audit_log(event: str, details: Dict[str, Any]) -> None:
    """
    Log file operation events for audit purposes.

    Args:
        event: Event type (e.g., "file_read", "file_write", "path_blocked")
        details: Additional context about the event
    """
    log.info(
        "FILE_AUDIT: %s | %s",
        event,
        " | ".join(f"{k}={v}" for k, v in details.items()),
        extra={"file_event": event, **details},
    )


def _safe_path(workspace_dir: Path, filename: str) -> Path:
    """Validate path stays within workspace (delegates to security module)."""
    try:
        return validate_path(workspace_dir, filename)
    except PathSecurityError as exc:
        raise ValueError(str(exc)) from exc


# ─────────────────────────────────────────────────────────────────────────────


class ReadFileSkill(BaseSkill):
    name = "read_file"
    description = (
        "Read the contents of a file in the conversation workspace. "
        "Returns up to 64 KB of text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Relative path to the file inside the workspace.",
            }
        },
        "required": ["filename"],
    }

    @validate_input
    async def execute(
        self, workspace_dir: Path, filename: str = "", **kwargs: Any
    ) -> str:
        try:
            path = _safe_path(workspace_dir, filename)
        except ValueError as exc:
            return f"Error: {exc}"

        if not path.exists():
            return f"Error: file not found: {filename!r}"
        if path.is_dir():
            return f"Error: {filename!r} is a directory, use list_files."

        raw = path.read_bytes()
        truncated = len(raw) > _MAX_READ_BYTES
        content = raw[:_MAX_READ_BYTES].decode(errors="replace")
        if truncated:
            content += "\n\n[... truncated at 64 KB ...]"
        return content


class WriteFileSkill(BaseSkill):
    name = "write_file"
    description = (
        "Write (or overwrite) a file in the conversation workspace. "
        "Creates parent directories automatically. "
        f"Maximum file size: {_MAX_WRITE_BYTES // 1024 // 1024}MB."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Relative path for the file inside the workspace.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write to the file.",
            },
        },
        "required": ["filename", "content"],
    }

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        filename: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> str:
        # Security check: Validate path
        try:
            path = _safe_path(workspace_dir, filename)
        except ValueError as exc:
            _audit_log("path_blocked", {"filename": filename, "reason": str(exc)})
            return f"Error: {exc}"

        # Security check: File size limit
        content_size = len(content.encode("utf-8"))
        if content_size > _MAX_WRITE_BYTES:
            _audit_log(
                "write_blocked",
                {
                    "filename": filename,
                    "size_bytes": content_size,
                    "max_bytes": _MAX_WRITE_BYTES,
                    "reason": "size_limit_exceeded",
                },
            )
            return (
                f"Error: Content size ({content_size // 1024}KB) exceeds "
                f"maximum allowed size ({_MAX_WRITE_BYTES // 1024 // 1024}MB)."
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        _audit_log(
            "file_written",
            {"filename": filename, "size_bytes": content_size},
        )
        return f"✅ Written {len(content)} chars to {filename!r}."


class ListFilesSkill(BaseSkill):
    name = "list_files"
    description = (
        "List files and directories in the conversation workspace (or a sub-path). "
        "Returns a tree-like listing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Sub-path inside the workspace to list. "
                    "Leave empty or use '.' for the root."
                ),
                "default": ".",
            }
        },
        "required": [],
    }

    async def execute(self, workspace_dir: Path, path: str = ".", **kwargs: Any) -> str:
        try:
            target = _safe_path(workspace_dir, path)
        except ValueError as exc:
            return f"Error: {exc}"

        if not target.exists():
            return f"Error: path not found: {path!r}"
        if not target.is_dir():
            return f"{path} is a file (not a directory)."

        lines: list[str] = []
        _tree(target, workspace_dir, lines, prefix="")
        if not lines:
            return "(workspace is empty)"
        return "\n".join(lines)


def _tree(
    directory: Path,
    workspace_root: Path,
    lines: list,
    prefix: str,
    depth: int = 0,
) -> None:
    if depth > 5:
        lines.append(prefix + "  ...")
        return
    try:
        children = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return
    for i, child in enumerate(children):
        connector = "└── " if i == len(children) - 1 else "├── "
        rel = str(child.relative_to(workspace_root))
        suffix = "/" if child.is_dir() else ""
        lines.append(prefix + connector + child.name + suffix)
        if child.is_dir():
            extension = "    " if i == len(children) - 1 else "│   "
            _tree(child, workspace_root, lines, prefix + extension, depth + 1)
