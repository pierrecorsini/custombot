"""
src/ui/image_handler.py — Image processing for vision-capable LLMs.

Accepts image bytes from incoming messages, converts to base64,
and sends to a vision model for description/analysis.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = logging.getLogger(__name__)

# Supported MIME types and their magic bytes signatures.
_SUPPORTED_TYPES: dict[str, bytes] = {
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG",
    "image/webp": b"RIFF",
}

# WhatsApp media size limit (20 MB).
MAX_IMAGE_SIZE: int = 20 * 1024 * 1024


def _detect_mime(data: bytes) -> str:
    """Return MIME type from image header bytes."""
    for mime, signature in _SUPPORTED_TYPES.items():
        if data[: len(signature)] == signature:
            return mime
    return "image/jpeg"  # fallback


def _encode_base64(data: bytes) -> str:
    """Encode *data* as a base64 data-URI string."""
    mime = _detect_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


@dataclass
class VisionConfig:
    """Configuration for image vision processing."""

    vision_model: str = ""
    vision_enabled: bool = False


class ImageHandler:
    """Process images via a vision-capable LLM.

    Usage::

        handler = ImageHandler(client=openai_client, config=config)
        description = await handler.process_image(image_bytes, "Describe this")
    """

    def __init__(self, client: AsyncOpenAI, config: VisionConfig) -> None:
        self._client = client
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.vision_enabled

    async def process_image(self, image_data: bytes, prompt: str = "Describe this image.") -> str:
        """Send *image_data* to the vision model with *prompt*.

        Args:
            image_data: Raw image bytes (JPEG, PNG, or WebP).
            prompt: Instruction for the vision model.

        Returns:
            Model's text response describing the image.

        Raises:
            ValueError: If the image exceeds the size limit or vision is disabled.
        """
        if not self._config.vision_enabled:
            raise ValueError("Image vision is disabled in configuration")

        if len(image_data) > MAX_IMAGE_SIZE:
            raise ValueError(
                f"Image size ({len(image_data)} bytes) exceeds limit "
                f"({MAX_IMAGE_SIZE} bytes)"
            )

        model = self._config.vision_model or "gpt-4o"
        data_uri = _encode_base64(image_data)

        response = await self._client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=1024,
        )

        content = response.choices[0].message.content
        log.info(
            "Image processed: model=%s, input=%d bytes, output=%d chars",
            model,
            len(image_data),
            len(content or ""),
        )
        return content or ""
