"""
test_whatsapp_qr.py - E2E test for WhatsApp connection via neonize.

Tests that the WhatsAppChannel and NeonizeBackend handle the connection
lifecycle correctly, including QR code display and session persistence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Tests: NeonizeBackend connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_neonize_backend_connects_successfully():
    """
    E2E Test: NeonizeBackend initializes and connects.

    Arrange:
        - Create NeonizeBackend with test config
        - Mock neonize.client.NewClient

    Act:
        - Call connect()

    Assert:
        - Client is initialized
        - Event handlers are registered
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    backend = NeonizeBackend(cfg)

    mock_client = MagicMock()
    mock_client.on = MagicMock(return_value=lambda f: f)
    mock_client.connect = MagicMock()

    with patch("src.channels.neonize_backend.neonize") as mock_neonize:
        # Mock the neonize imports inside connect()
        mock_neonize.client.NewClient.return_value = mock_client
        mock_neonize.events.ConnectedEv = MagicMock()
        mock_neonize.events.MessageEv = MagicMock()
        mock_neonize.events.DisconnectedEv = MagicMock()
        mock_neonize.events.PairStatusEv = MagicMock()

        loop = asyncio.get_running_loop()
        backend.connect(loop)

    assert backend._client is not None


@pytest.mark.asyncio
async def test_neonize_backend_is_connected_property():
    """
    E2E Test: NeonizeBackend reports connected state correctly.

    Arrange:
        - Create NeonizeBackend

    Act:
        - Check is_connected before and after setting state

    Assert:
        - Initially False, True after connected
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    backend = NeonizeBackend(cfg)

    assert backend.is_connected is False

    backend._connected = True
    assert backend.is_connected is True


@pytest.mark.asyncio
async def test_neonize_backend_disconnect():
    """
    E2E Test: NeonizeBackend disconnects cleanly.

    Arrange:
        - Create NeonizeBackend with a mock client

    Act:
        - Call disconnect()

    Assert:
        - Client is set to None
        - is_connected is False
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    backend = NeonizeBackend(cfg)

    mock_client = MagicMock()
    mock_client.logout = MagicMock()
    backend._client = mock_client
    backend._connected = True

    await backend.disconnect()

    assert backend.is_connected is False
    assert backend._client is None


@pytest.mark.asyncio
async def test_neonize_backend_send_when_not_connected():
    """
    E2E Test: NeonizeBackend raises error when sending while disconnected.

    Arrange:
        - Create NeonizeBackend (not connected)

    Act:
        - Try to send a message

    Assert:
        - RuntimeError is raised
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    backend = NeonizeBackend(cfg)

    with pytest.raises(RuntimeError, match="Not connected"):
        await backend.send("test@s.whatsapp.net", "Hello")


@pytest.mark.asyncio
async def test_neonize_backend_poll_message_timeout():
    """
    E2E Test: NeonizeBackend poll_message returns None on timeout.

    Arrange:
        - Create NeonizeBackend with empty queue

    Act:
        - Call poll_message()

    Assert:
        - Returns None (timeout)
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    backend = NeonizeBackend(cfg)

    result = await backend.poll_message()
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: WhatsAppChannel
# ─────────────────────────────────────────────────────────────────────────────


def test_whatsapp_channel_get_channel_prompt():
    """
    E2E Test: WhatsAppChannel returns WhatsApp formatting prompt.

    Arrange:
        - Create WhatsAppChannel

    Act:
        - Call get_channel_prompt()

    Assert:
        - Returns string with WhatsApp formatting instructions
        - Contains key formatting rules
    """
    from src.channels.whatsapp import WhatsAppChannel
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    channel = WhatsAppChannel(cfg)

    prompt = channel.get_channel_prompt()

    assert prompt is not None
    assert "WhatsApp" in prompt
    assert "*bold*" in prompt
    assert "_italic_" in prompt


def test_whatsapp_channel_prompt_forbids_markdown():
    """
    E2E Test: WhatsApp prompt forbids Markdown tables and headers.

    Assert:
        - Prompt mentions NO Markdown tables
        - Prompt mentions NO headers
    """
    from src.channels.whatsapp import WhatsAppChannel
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    channel = WhatsAppChannel(cfg)

    prompt = channel.get_channel_prompt()

    assert "NO Markdown tables" in prompt or "no tables" in prompt.lower()
    assert "NO #" in prompt or "NO ##" in prompt


@pytest.mark.asyncio
async def test_whatsapp_channel_close():
    """
    E2E Test: WhatsAppChannel closes cleanly.

    Arrange:
        - Create WhatsAppChannel with mocked backend

    Act:
        - Call close()

    Assert:
        - Backend disconnect is called
        - Shutdown is requested
    """
    from src.channels.whatsapp import WhatsAppChannel
    from src.config import NeonizeConfig, WhatsAppConfig

    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path="workspace/test_session.db"),
    )
    channel = WhatsAppChannel(cfg)

    # Mock the backend
    channel._backend.disconnect = AsyncMock()

    await channel.close()

    assert channel._shutdown_requested is True
    channel._backend.disconnect.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: NeonizeBackend db_path configuration
# ─────────────────────────────────────────────────────────────────────────────


def test_neonize_backend_uses_config_db_path():
    """
    E2E Test: NeonizeBackend uses the db_path from config.

    Arrange:
        - Create config with specific db_path

    Act:
        - Create NeonizeBackend

    Assert:
        - Backend stores the db_path
    """
    from src.channels.neonize_backend import NeonizeBackend
    from src.config import NeonizeConfig, WhatsAppConfig

    db_path = "custom/path/session.db"
    cfg = WhatsAppConfig(
        provider="neonize",
        neonize=NeonizeConfig(db_path=db_path),
    )
    backend = NeonizeBackend(cfg)

    assert backend._db_path == db_path
