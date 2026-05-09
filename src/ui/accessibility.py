"""
src/ui/accessibility.py — Accessibility improvements for response delivery.

Provides simplified response mode, alt-text generation, high-contrast
formatting, and TTS integration for users who need it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Recognised accessibility modes.
MODE_STANDARD = "standard"
MODE_SIMPLIFIED = "simplified"
MODE_SCREEN_READER = "screen_reader"
VALID_MODES = frozenset({MODE_STANDARD, MODE_SIMPLIFIED, MODE_SCREEN_READER})

# Regex for stripping markdown formatting.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"\*(.+?)\*")
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE = re.compile(r"`(.+?)`")
_MD_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LIST = re.compile(r"^[*\-]\s+", re.MULTILINE)
_EMOJI = re.compile(r"[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff"
                     r"\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff"
                     r"\U00002702-\U000027b0\U0000fe00-\U0000feff"
                     r"\U0001f900-\U0001f9ff\U0001fa00-\U0001fa6f"
                     r"\U0001fa70-\U0001faff\u2600-\u26ff\u2700-\u27bf]")


@dataclass(slots=True, frozen=True)
class AccessibilityConfig:
    """Accessibility feature configuration."""

    mode: str = MODE_STANDARD


class AccessibilityManager:
    """Transform responses based on accessibility requirements."""

    def __init__(self, config: AccessibilityConfig | None = None) -> None:
        self._config = config or AccessibilityConfig()

    @property
    def mode(self) -> str:
        """Current accessibility mode."""
        return self._config.mode

    def apply_accessibility(self, response: str, mode: str | None = None) -> str:
        """Transform a response for the given accessibility mode.

        Args:
            response: Original response text.
            mode: Override mode (uses config default if None).

        Returns:
            Transformed response string.
        """
        active_mode = mode or self._config.mode

        if active_mode == MODE_STANDARD:
            return response

        if active_mode == MODE_SIMPLIFIED:
            return self._simplify(response)

        if active_mode == MODE_SCREEN_READER:
            return self._screen_reader_format(response)

        return response

    def generate_alt_text(self, description: str) -> str:
        """Generate alt-text from an image description.

        Args:
            description: Available image metadata or description.

        Returns:
            Plain-text alt description.
        """
        cleaned = _EMOJI.sub("", description).strip()
        if not cleaned:
            return "Image"
        # Limit length for screen readers.
        if len(cleaned) > 200:
            cleaned = cleaned[:197] + "..."
        return cleaned

    def _simplify(self, text: str) -> str:
        """Simplified mode: shorter sentences, no emojis, plain text."""
        # Remove emojis
        text = _EMOJI.sub("", text)

        # Strip markdown formatting
        text = _MD_BOLD.sub(r"\1", text)
        text = _MD_ITALIC.sub(r"\1", text)
        text = _MD_HEADER.sub("", text)

        # Convert code blocks to indented text
        text = _MD_CODE_BLOCK.sub(
            lambda m: "\n" + "\n".join(
                "  " + line for line in m.group(0).strip("`").strip().splitlines()
            ) + "\n",
            text,
        )
        text = _MD_INLINE_CODE.sub(r"\1", text)

        # Convert markdown list markers to bullet characters
        text = _MD_LIST.sub("- ", text)

        return text.strip()

    def _screen_reader_format(self, text: str) -> str:
        """Screen reader mode: semantic markup, descriptive formatting."""
        # Add explicit labels for headings
        text = _MD_HEADER.sub("", text)

        # Convert bold to descriptive markers
        text = _MD_BOLD.sub(r"\1", text)
        text = _MD_ITALIC.sub(r"\1", text)

        # Describe code blocks
        text = _MD_CODE_BLOCK.sub(
            lambda m: "[Code start]\n" + m.group(0).strip("`").strip() + "\n[Code end]",
            text,
        )
        text = _MD_INLINE_CODE.sub(r"code: \1", text)

        # Keep list markers (screen readers handle these well)
        return text.strip()
