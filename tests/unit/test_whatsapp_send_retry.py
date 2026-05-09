"""
test_whatsapp_send_retry.py - Tests for per-chunk retry in WhatsAppChannel._send_message().

Verifies retry behaviour:
- Successful send (no retry needed)
- First attempt fails, retry succeeds
- Both attempts fail → raises with partial delivery warning
- Single-chunk messages (no partial delivery warning on failure)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.config import NeonizeConfig, WhatsAppConfig


def _make_channel() -> "WhatsAppChannel":
    """Create a WhatsAppChannel with a mocked backend."""
    from src.channels.whatsapp import WhatsAppChannel

    cfg = WhatsAppConfig(neonize=NeonizeConfig(db_path="/tmp/test.db"))
    channel = WhatsAppChannel(cfg)
    # Replace the real backend with a mock
    channel._backend = AsyncMock()
    channel._backend.send = AsyncMock()
    return channel


@pytest.mark.asyncio
async def test_send_message_succeeds_without_retry():
    """When all chunks send successfully, no retry occurs."""
    channel = _make_channel()

    await channel._send_message("chat1", "Hello, world!")

    assert channel._backend.send.call_count == 1


@pytest.mark.asyncio
async def test_send_message_single_chunk_retry_succeeds():
    """When a single-chunk send fails on first attempt, retry succeeds."""
    channel = _make_channel()
    channel._backend.send.side_effect = [RuntimeError("transient"), None]

    with patch("src.channels.whatsapp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await channel._send_message("chat1", "Hello!")

    assert channel._backend.send.call_count == 2
    mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_single_chunk_both_attempts_fail():
    """When both attempts fail for a single chunk, the exception is raised.
    No partial delivery warning because nothing was delivered yet."""
    channel = _make_channel()
    channel._backend.send.side_effect = RuntimeError("broken")

    with (
        patch("src.channels.whatsapp.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="broken"),
    ):
        await channel._send_message("chat1", "Hello!")

    assert channel._backend.send.call_count == 2


@pytest.mark.asyncio
async def test_send_message_multi_chunk_second_chunk_retries():
    """With multiple chunks, if the second chunk fails then retries, both are sent."""
    channel = _make_channel()
    # First chunk succeeds, second chunk fails then succeeds on retry
    channel._backend.send.side_effect = [None, RuntimeError("transient"), None]

    long_text = "A" * 4001  # forces 2 chunks
    with patch("src.channels.whatsapp.asyncio.sleep", new_callable=AsyncMock):
        await channel._send_message("chat1", long_text)

    assert channel._backend.send.call_count == 3


@pytest.mark.asyncio
async def test_send_message_partial_delivery_logs_warning():
    """When chunk 2/3 fails both attempts, an error is raised and partial
    delivery is logged (1 of 3 chunks were sent)."""
    channel = _make_channel()
    # Chunk 1 succeeds, chunk 2 fails twice
    channel._backend.send.side_effect = [
        None,
        RuntimeError("connection lost"),
        RuntimeError("still broken"),
    ]

    # 3 chunks: 4000 "A"s, 4000 "B"s, 100 "C"s (newlines are stripped by lstrip)
    long_text = "A" * 4000 + "\n" + "B" * 4000 + "\n" + "C" * 100
    with (
        patch("src.channels.whatsapp.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="still broken"),
        patch("src.channels.whatsapp.log") as mock_log,
    ):
        await channel._send_message("chat1", long_text)

    # Verify partial delivery error was logged
    mock_log.error.assert_called_once()
    call_args = mock_log.error.call_args
    assert "Partial delivery" in call_args[0][0]
    assert "chat1" in call_args[0][1]
    # 1 of 3 chunks sent (chunk index 1 = second chunk failed)
    assert call_args[0][2] == 1  # chunks sent
    assert call_args[0][3] == 3  # total chunks


@pytest.mark.asyncio
async def test_send_message_incoming_len_only_on_first_chunk():
    """incoming_len is passed to the first chunk send and 0 to subsequent ones."""
    channel = _make_channel()
    channel._last_incoming_len["chat1"] = 42

    long_text = "A" * 4001 + "\n" + "B" * 100

    await channel._send_message("chat1", long_text)

    calls = channel._backend.send.call_args_list
    assert calls[0].kwargs["incoming_len"] == 42
    assert calls[1].kwargs["incoming_len"] == 0


@pytest.mark.asyncio
async def test_send_message_retry_uses_same_incoming_len():
    """When first attempt fails and retries, the retry uses the same incoming_len."""
    channel = _make_channel()
    channel._last_incoming_len["chat1"] = 99
    channel._backend.send.side_effect = [RuntimeError("fail"), None]

    with patch("src.channels.whatsapp.asyncio.sleep", new_callable=AsyncMock):
        await channel._send_message("chat1", "Hello!")

    calls = channel._backend.send.call_args_list
    assert calls[0].kwargs["incoming_len"] == 99
    assert calls[1].kwargs["incoming_len"] == 99
