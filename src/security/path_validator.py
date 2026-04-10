"""
src/security/path_validator.py — Workspace path validation and confinement.

Security module that ensures all file operations are confined to the
designated workspace directory. Prevents path traversal attacks and
absolute path escapes.

Usage:
    from src.security import validate_path, validate_command_paths

    # Validate a file path
    safe_path = validate_path(workspace_dir, user_path)

    # Validate paths in a shell command
    sanitized = validate_command_paths(workspace_dir, "cat /etc/passwd")
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shlex
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class PathSecurityError(Exception):
    """Raised when a path violates workspace confinement rules."""

    def __init__(self, message: str, path: Optional[str] = None, reason: str = ""):
        super().__init__(message)
        self.path = path
        self.reason = reason


def _normalize_path(path: Path) -> str:
    """Normalize path for comparison (case-insensitive on Windows)."""
    return (
        str(path.resolve()).lower()
        if platform.system() == "Windows"
        else str(path.resolve())
    )


def is_path_in_workspace(workspace_dir: Path, target_path: Path) -> bool:
    """
    Check if target_path is within workspace_dir.

    Args:
        workspace_dir: The workspace root directory.
        target_path: The path to check.

    Returns:
        True if target_path is within workspace_dir, False otherwise.
    """
    workspace_normalized = _normalize_path(workspace_dir)
    target_normalized = _normalize_path(target_path)
    return target_normalized == workspace_normalized or target_normalized.startswith(
        workspace_normalized + os.sep
    )


def validate_path(workspace_dir: Path, user_path: str) -> Path:
    """
    Validate and resolve a user-provided path within the workspace.

    Args:
        workspace_dir: The workspace root directory.
        user_path: User-provided path string (may be relative or absolute).

    Returns:
        Resolved Path object within the workspace.

    Raises:
        PathSecurityError: If the path attempts to escape the workspace.
    """
    if not user_path or not user_path.strip():
        return workspace_dir.resolve()

    user_path = user_path.strip()

    # Check for absolute paths
    user_path_obj = Path(user_path)
    if user_path_obj.is_absolute():
        # Check if absolute path is within workspace
        if not is_path_in_workspace(workspace_dir, user_path_obj):
            raise PathSecurityError(
                f"Absolute path not allowed outside workspace: {user_path!r}",
                path=user_path,
                reason="absolute_path_escape",
            )
        return user_path_obj.resolve()

    # Resolve relative path
    resolved = (workspace_dir / user_path).resolve()

    # Verify it stays within workspace
    if not is_path_in_workspace(workspace_dir, resolved):
        raise PathSecurityError(
            f"Path escape attempt blocked: {user_path!r}",
            path=user_path,
            reason="path_traversal",
        )

    return resolved


# Patterns for detecting paths in shell commands
# Matches: quoted paths, Windows paths (C:\), Unix paths (/path, ~/path), relative paths
_PATH_PATTERNS = [
    # Windows absolute paths: C:\, D:\, etc.
    r'["\']?([A-Za-z]:\\[^"\']*)["\']?',
    # Unix absolute paths
    r'["\']?(/[^"\']*)["\']?',
    # Home directory paths
    r'["\']?(~[/\\][^"\']*)["\']?',
]

# Commands that commonly take path arguments
_PATH_TAKING_COMMANDS = {
    # File operations
    "cat",
    "type",
    "more",
    "less",
    "head",
    "tail",
    "wc",
    "cp",
    "copy",
    "mv",
    "move",
    "rename",
    "rm",
    "del",
    "rmdir",
    "mkdir",
    "md",
    "touch",
    "echo",
    "tee",
    # Directory operations
    "ls",
    "dir",
    "cd",
    "chdir",
    "pwd",
    "find",
    "where",
    "which",
    # File permissions
    "chmod",
    "chown",
    "attrib",
    "icacls",
    # Archive operations
    "tar",
    "zip",
    "unzip",
    "gzip",
    "gunzip",
    # Text processing
    "grep",
    "findstr",
    "sed",
    "awk",
    "sort",
    # Diff
    "diff",
    "fc",
    "comp",
}

# Dangerous path patterns that should always be blocked
_BLOCKED_PATH_PATTERNS = [
    # System directories
    r"^/etc",
    r"^/proc",
    r"^/sys",
    r"^/dev",
    r"^/root",
    r"^/boot",
    r"^/var/log",
    r"^/var/run",
    r"^/home",  # User home directories
    r"^/usr",  # System programs
    r"^/bin",  # System binaries
    r"^/sbin",  # System binaries
    r"^/lib",  # System libraries
    r"^/opt",  # Optional software
    # Windows system
    r"^[Cc]:\\[Ww][Ii][Nn][Dd][Oo][Ww][Ss]",
    r"^[Cc]:\\[Pp][Rr][Oo][Gg][Rr][Aa][Mm]",
    r"^[Cc]:\\[Uu][Ss][Ee][Rr][Ss]\\[^\\]+\\",  # Other user directories
    # Home directory
    r"^~/",
    r"^~\\",
]


def _extract_paths_from_command(command: str) -> list[str]:
    """
    Extract potential file paths from a shell command.

    Uses shlex.split() for proper shell tokenization that handles
    quoted paths, escape sequences, and subshells.

    Args:
        command: The shell command string.

    Returns:
        List of detected path strings.
    """
    paths = []

    try:
        parts = shlex.split(command)
    except ValueError:
        # Fallback for unmatched quotes — use simple split
        parts = command.split()

    for part in parts:
        # Skip command flags (like -la, --help, /C on Windows cmd)
        # But NOT Unix absolute paths that start with /
        if part.startswith("-"):
            continue
        # Skip Windows cmd flags like /C, /Q but not paths like /etc/passwd
        # Windows flags are single letter after /, paths have more chars
        if part.startswith("/") and len(part) == 2:
            continue

        # Check if it looks like a path (contains separator or is absolute/home)
        if "/" in part or "\\" in part or part.startswith("~"):
            # Remove quotes
            clean = part.strip("\"'")
            paths.append(clean)

    return paths


def validate_command_paths(workspace_dir: Path, command: str) -> Tuple[str, bool]:
    """
    Validate paths in a shell command and sanitize if needed.

    This function analyzes a shell command for file paths and ensures
    they are confined to the workspace directory.

    Args:
        workspace_dir: The workspace root directory.
        command: The shell command to validate.

    Returns:
        Tuple of (sanitized_command, was_modified).
        - sanitized_command: The command with paths validated/blocked
        - was_modified: True if the command was modified or should be blocked

    Raises:
        PathSecurityError: If a path attempts to escape the workspace.
    """
    if not command or not command.strip():
        return command, False

    command = command.strip()
    detected_paths = _extract_paths_from_command(command)

    for path in detected_paths:
        # Check for blocked patterns first
        for pattern in _BLOCKED_PATH_PATTERNS:
            if re.match(pattern, path, re.IGNORECASE):
                raise PathSecurityError(
                    f"Access to system path blocked: {path!r}",
                    path=path,
                    reason="system_path_access",
                )

        # Check if path is absolute and outside workspace
        path_obj = Path(path)
        if path_obj.is_absolute():
            if not is_path_in_workspace(workspace_dir, path_obj):
                raise PathSecurityError(
                    f"Absolute path not allowed outside workspace: {path!r}",
                    path=path,
                    reason="absolute_path_escape",
                )

    return command, False


def sanitize_command(workspace_dir: Path, command: str) -> str:
    """
    Sanitize a shell command by blocking dangerous paths.

    This is a stricter version that returns an error message instead
    of raising an exception, suitable for use in skill execution.

    Args:
        workspace_dir: The workspace root directory.
        command: The shell command to sanitize.

    Returns:
        Either the original command (if safe) or an error message.
    """
    try:
        validate_command_paths(workspace_dir, command)
        return command
    except PathSecurityError as e:
        log.warning(
            "Blocked command with unsafe path: %s (reason: %s)", e.path, e.reason
        )
        return f"❌ Security: {e}"
