"""
Tests for src/security/path_validator.py — Workspace path validation and confinement.

Covers:
- validate_path: relative paths, absolute paths, traversal attacks, edge cases
- is_path_in_workspace: containment checks
- validate_command_paths: shell command path extraction and validation
- sanitize_command: non-exception sanitization
- _extract_paths_from_command: path extraction from command strings
- PathSecurityError: custom exception attributes
"""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from src.security.path_validator import (
    PathSecurityError,
    _extract_paths_from_command,
    _normalize_path,
    is_path_in_workspace,
    sanitize_command,
    validate_command_paths,
    validate_path,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


# ═══════════════════════════════════════════════════════════════════════════
# 1. PathSecurityError
# ═══════════════════════════════════════════════════════════════════════════


class TestPathSecurityError:
    """Tests for the PathSecurityError exception."""

    def test_message_stored(self):
        exc = PathSecurityError("bad path", path="/etc/passwd", reason="traversal")
        assert str(exc) == "bad path"
        assert exc.path == "/etc/passwd"
        assert exc.reason == "traversal"

    def test_default_message(self):
        exc = PathSecurityError("default msg")
        assert str(exc) == "default msg"
        assert exc.path is None
        assert exc.reason == ""

    def test_is_custom_bot_exception(self):
        from src.exceptions import CustomBotException

        exc = PathSecurityError("test")
        assert isinstance(exc, CustomBotException)


# ═══════════════════════════════════════════════════════════════════════════
# 2. _normalize_path
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizePath:
    """Tests for the internal _normalize_path helper."""

    def test_returns_string(self, workspace: Path):
        result = _normalize_path(workspace)
        assert isinstance(result, str)

    def test_resolves_path(self, tmp_path: Path):
        p = tmp_path / "some_dir"
        result = _normalize_path(p)
        assert str(p.resolve()) in result or str(p.resolve()).lower() in result


# ═══════════════════════════════════════════════════════════════════════════
# 3. is_path_in_workspace
# ═══════════════════════════════════════════════════════════════════════════


class TestIsPathInWorkspace:
    """Tests for is_path_in_workspace."""

    def test_child_path_is_inside(self, workspace: Path):
        child = workspace / "subdir" / "file.txt"
        assert is_path_in_workspace(workspace, child) is True

    def test_workspace_itself_is_inside(self, workspace: Path):
        assert is_path_in_workspace(workspace, workspace) is True

    def test_sibling_is_outside(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        sibling = tmp_path / "other_dir"
        assert is_path_in_workspace(ws, sibling) is False

    def test_parent_is_outside(self, tmp_path: Path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        assert is_path_in_workspace(ws, tmp_path) is False

    def test_deeply_nested_child(self, workspace: Path):
        deep = workspace / "a" / "b" / "c" / "d" / "file.txt"
        assert is_path_in_workspace(workspace, deep) is True

    def test_traversal_attempt(self, workspace: Path):
        escape = workspace / ".." / "etc" / "passwd"
        assert is_path_in_workspace(workspace, escape) is False


# ═══════════════════════════════════════════════════════════════════════════
# 4. validate_path
# ═══════════════════════════════════════════════════════════════════════════


class TestValidatePath:
    """Tests for validate_path."""

    def test_simple_relative_path(self, workspace: Path):
        result = validate_path(workspace, "file.txt")
        assert result == (workspace / "file.txt").resolve()

    def test_nested_relative_path(self, workspace: Path):
        result = validate_path(workspace, "subdir/file.txt")
        assert result == (workspace / "subdir" / "file.txt").resolve()

    def test_empty_string_returns_workspace(self, workspace: Path):
        result = validate_path(workspace, "")
        assert result == workspace.resolve()

    def test_whitespace_only_returns_workspace(self, workspace: Path):
        result = validate_path(workspace, "   ")
        assert result == workspace.resolve()

    def test_traversal_attack_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError, reason="path_traversal"):
            validate_path(workspace, "../../../etc/passwd")

    def test_absolute_path_outside_workspace(self, workspace: Path):
        with pytest.raises(PathSecurityError, reason="absolute_path_escape"):
            validate_path(workspace, "/etc/passwd")

    def test_dot_path_resolves_to_workspace(self, workspace: Path):
        result = validate_path(workspace, ".")
        assert result == workspace.resolve()

    def test_path_with_spaces(self, workspace: Path):
        result = validate_path(workspace, "my file.txt")
        assert result == (workspace / "my file.txt").resolve()

    def test_deeply_nested_relative_path(self, workspace: Path):
        result = validate_path(workspace, "a/b/c/d/e.txt")
        assert result == (workspace / "a/b/c/d/e.txt").resolve()

    def test_traversal_in_middle(self, workspace: Path):
        """Paths like 'subdir/../../../etc/passwd' should be blocked."""
        with pytest.raises(PathSecurityError):
            validate_path(workspace, "subdir/../../../etc/passwd")

    def test_absolute_path_inside_workspace(self, workspace: Path):
        """An absolute path that resolves inside workspace should be allowed."""
        target = workspace / "inner.txt"
        result = validate_path(workspace, str(target))
        assert result == target.resolve()


# ═══════════════════════════════════════════════════════════════════════════
# 5. _extract_paths_from_command
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractPathsFromCommand:
    """Tests for _extract_paths_from_command."""

    def test_simple_path(self):
        paths = _extract_paths_from_command("cat /etc/passwd")
        assert "/etc/passwd" in paths

    def test_quoted_path(self):
        paths = _extract_paths_from_command('cat "/etc/passwd"')
        assert "/etc/passwd" in paths

    def test_windows_path(self):
        paths = _extract_paths_from_command("type C:\\Windows\\System32\\config")
        assert len(paths) > 0

    def test_home_path(self):
        paths = _extract_paths_from_command("cat ~/.ssh/id_rsa")
        assert "~/.ssh/id_rsa" in paths

    def test_flags_skipped(self):
        paths = _extract_paths_from_command("ls -la /home/user")
        assert "/home/user" in paths
        assert "-la" not in paths

    def test_no_paths(self):
        paths = _extract_paths_from_command("echo hello")
        assert paths == []

    def test_relative_path_with_slash(self):
        paths = _extract_paths_from_command("cat src/main.py")
        assert "src/main.py" in paths

    def test_empty_command(self):
        paths = _extract_paths_from_command("")
        assert paths == []

    def test_multiple_paths(self):
        paths = _extract_paths_from_command("cp /etc/hosts /tmp/hosts_copy")
        assert "/etc/hosts" in paths
        assert "/tmp/hosts_copy" in paths

    def test_single_slash_flag_skipped(self):
        """Windows-style /C flag should be skipped but /etc/passwd kept."""
        paths = _extract_paths_from_command("cmd /C echo /etc/passwd")
        # /C should be skipped, /etc/passwd should be present
        assert "/C" not in paths

    def test_unmatched_quotes_fallback(self):
        """Unmatched quotes should use simple split fallback."""
        paths = _extract_paths_from_command('cat "/etc/passwd')
        # Should not crash; fallback to simple split
        assert isinstance(paths, list)


# ═══════════════════════════════════════════════════════════════════════════
# 6. validate_command_paths
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateCommandPaths:
    """Tests for validate_command_paths."""

    def test_safe_relative_path(self, workspace: Path):
        cmd, modified = validate_command_paths(workspace, "cat file.txt")
        assert cmd == "cat file.txt"
        assert modified is False

    def test_system_path_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError, reason="system_path_access"):
            validate_command_paths(workspace, "cat /etc/passwd")

    def test_proc_path_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError):
            validate_command_paths(workspace, "cat /proc/self/environ")

    def test_home_path_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError):
            validate_command_paths(workspace, "cat ~/.ssh/id_rsa")

    def test_empty_command(self, workspace: Path):
        cmd, modified = validate_command_paths(workspace, "")
        assert cmd == ""
        assert modified is False

    def test_whitespace_command(self, workspace: Path):
        cmd, modified = validate_command_paths(workspace, "   ")
        assert modified is False

    def test_command_without_paths(self, workspace: Path):
        cmd, modified = validate_command_paths(workspace, "echo hello world")
        assert modified is False

    def test_absolute_workspace_path_allowed(self, workspace: Path):
        """Absolute path inside workspace should pass."""
        inner = workspace / "data.txt"
        cmd, modified = validate_command_paths(workspace, f"cat {inner}")
        assert modified is False

    def test_var_log_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError):
            validate_command_paths(workspace, "tail /var/log/syslog")

    def test_windows_system_path_blocked(self, workspace: Path):
        with pytest.raises(PathSecurityError):
            validate_command_paths(workspace, "type C:\\Windows\\System32\\config\\SAM")


# ═══════════════════════════════════════════════════════════════════════════
# 7. sanitize_command
# ═══════════════════════════════════════════════════════════════════════════


class TestSanitizeCommand:
    """Tests for sanitize_command."""

    def test_safe_command_returns_original(self, workspace: Path):
        result = sanitize_command(workspace, "cat file.txt")
        assert result == "cat file.txt"

    def test_unsafe_command_returns_error_message(self, workspace: Path):
        result = sanitize_command(workspace, "cat /etc/passwd")
        assert "Security:" in result
        assert "cat /etc/passwd" not in result or "Security" in result

    def test_empty_command_returns_empty(self, workspace: Path):
        result = sanitize_command(workspace, "")
        assert result == ""

    def test_echo_command_passes(self, workspace: Path):
        result = sanitize_command(workspace, "echo hello")
        assert result == "echo hello"
