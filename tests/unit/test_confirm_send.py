"""test_confirm_send.py — Unit tests for safe-mode _confirm_send() helper.

Covers:
- Timeout when stdin read blocks beyond SAFE_MODE_CONFIRM_TIMEOUT
- Normal Y/N confirmation flow
- Auto-reject on max invalid inputs
- Auto-reject when stdin is not a TTY
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.channels.base import _confirm_send


# ─────────────────────────────────────────────────────────────────────────────
# Tests: stdin read timeout
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_send_timeout_auto_rejects():
    """When input() blocks beyond the timeout, the send is auto-rejected."""
    async def _blocked_input(_fn, _prompt: str) -> str:
        await asyncio.sleep(300)
        return ""

    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.constants.SAFE_MODE_CONFIRM_TIMEOUT", 0.1),
        patch("src.constants.SAFE_MODE_MAX_CONFIRM_RETRIES", 1),
        patch("src.channels.base.asyncio.to_thread", side_effect=_blocked_input),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = await _confirm_send("chat-123", "Redacted")

    assert result is False


@pytest.mark.asyncio
async def test_confirm_send_timeout_logs_warning():
    """Timeout path emits a warning log with the timeout duration."""
    async def _blocked_input(_fn, _prompt: str) -> str:
        await asyncio.sleep(300)
        return ""

    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.constants.SAFE_MODE_CONFIRM_TIMEOUT", 0.05),
        patch("src.constants.SAFE_MODE_MAX_CONFIRM_RETRIES", 1),
        patch("src.channels.base.asyncio.to_thread", side_effect=_blocked_input),
        patch("src.channels.base.log") as mock_log,
    ):
        mock_sys.stdin.isatty.return_value = True
        await _confirm_send("chat-456", "Redacted")

    mock_log.warning.assert_called_once()
    args = mock_log.warning.call_args
    assert "timed out" in args[0][0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: normal confirmation flow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_send_yes():
    """User confirms with 'y' → returns True."""
    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.channels.base.asyncio.to_thread", return_value="y"),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = await _confirm_send("chat-1", "Redacted")

    assert result is True


@pytest.mark.asyncio
async def test_confirm_send_no():
    """User rejects with 'n' → returns False."""
    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.channels.base.asyncio.to_thread", return_value="n"),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = await _confirm_send("chat-1", "Redacted")

    assert result is False


@pytest.mark.asyncio
async def test_confirm_send_yes_full_word():
    """User confirms with 'yes' → returns True."""
    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.channels.base.asyncio.to_thread", return_value="yes"),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = await _confirm_send("chat-1", "Redacted")

    assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# Tests: non-TTY auto-reject
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_send_non_tty_auto_rejects():
    """When stdin is not a TTY, the send is auto-rejected without prompting."""
    with patch("src.channels.base.sys") as mock_sys:
        mock_sys.stdin.isatty.return_value = False
        result = await _confirm_send("chat-1", "Redacted")

    assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests: max invalid inputs
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_send_max_invalid_rejects():
    """After SAFE_MODE_MAX_CONFIRM_RETRIES invalid inputs, the send is rejected."""
    with (
        patch("src.channels.base.sys") as mock_sys,
        patch("src.channels.base.asyncio.to_thread", return_value="maybe"),
        patch("src.constants.SAFE_MODE_MAX_CONFIRM_RETRIES", 2),
    ):
        mock_sys.stdin.isatty.return_value = True
        result = await _confirm_send("chat-1", "Redacted")

    assert result is False
