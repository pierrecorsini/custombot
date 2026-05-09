"""
src/ui/voice_handler.py — Voice message transcription via OpenAI Whisper.

Accepts audio bytes from incoming WhatsApp messages and transcribes
them using the Whisper API.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = logging.getLogger(__name__)

# Supported audio formats → MIME types.
_FORMAT_MIME: dict[str, str] = {
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
}

# Whisper API size limit (25 MB).
MAX_AUDIO_SIZE: int = 25 * 1024 * 1024


def _get_mime(fmt: str) -> str:
    """Return MIME type for an audio format extension."""
    return _FORMAT_MIME.get(fmt, "audio/ogg")


@dataclass
class TranscriptionConfig:
    """Configuration for voice transcription."""

    transcription_enabled: bool = True
    transcription_model: str = "whisper-1"


class VoiceHandler:
    """Transcribe voice messages via OpenAI Whisper.

    Usage::

        handler = VoiceHandler(client=openai_client, config=config)
        text = await handler.transcribe(audio_bytes, format="ogg")
    """

    def __init__(self, client: AsyncOpenAI, config: TranscriptionConfig) -> None:
        self._client = client
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.transcription_enabled

    async def transcribe(self, audio_data: bytes, format: str = "ogg") -> str:
        """Transcribe *audio_data* using Whisper.

        Args:
            audio_data: Raw audio bytes.
            format: Audio format extension (ogg, mp3, wav, m4a).

        Returns:
            Transcribed text.

        Raises:
            ValueError: If audio exceeds size limit or transcription is disabled.
        """
        if not self._config.transcription_enabled:
            raise ValueError("Voice transcription is disabled in configuration")

        if len(audio_data) > MAX_AUDIO_SIZE:
            raise ValueError(
                f"Audio size ({len(audio_data)} bytes) exceeds limit "
                f"({MAX_AUDIO_SIZE} bytes)"
            )

        filename = f"voice.{format}"
        mime = _get_mime(format)

        file_obj = io.BytesIO(audio_data)
        file_obj.name = filename

        response = await self._client.audio.transcriptions.create(
            model=self._config.transcription_model,
            file=(filename, file_obj, mime),
            response_format="text",
        )

        text = response.strip() if isinstance(response, str) else response.text.strip()
        log.info(
            "Audio transcribed: format=%s, input=%d bytes, output=%d chars",
            format,
            len(audio_data),
            len(text),
        )
        return text
