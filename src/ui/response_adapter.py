"""
src/ui/response_adapter.py — Automatic response length adaptation.

Analyzes user messages and conversation context to suggest an appropriate
max_tokens value, ensuring short queries get concise replies and complex
queries get detailed ones.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.context_builder import ChatMessage

log = logging.getLogger(__name__)

# Token thresholds
SHORT_MESSAGE_CHARS = 20
DETAILED_KEYWORDS = re.compile(
    r"\b(explain|detail|detailed|how|describe|elaborate|comprehensive|thorough|in\s+depth)\b",
    re.IGNORECASE,
)
CODE_KEYWORDS = re.compile(
    r"\b(code|function|implement|script|program|example|snippet)\b",
    re.IGNORECASE,
)

SHORT_MAX_TOKENS = 200
DETAILED_MAX_TOKENS = 1000
DEFAULT_MAX_TOKENS = 500
CODE_MAX_TOKENS = 1500


@dataclass(slots=True)
class AdaptationMetrics:
    """Tracks response length adaptation statistics."""

    short_count: int = 0
    detailed_count: int = 0
    code_count: int = 0
    default_count: int = 0


class ResponseLengthAdapter:
    """Suggests max_tokens based on message complexity and conversation history."""

    def __init__(self) -> None:
        self._metrics = AdaptationMetrics()

    def suggest_max_tokens(
        self,
        message: str,
        history: list[ChatMessage] | None = None,
    ) -> int:
        """Suggest response length in tokens based on message and history.

        Args:
            message: The user's incoming message text.
            history: Recent conversation history for context.

        Returns:
            Suggested max_tokens value.
        """
        stripped = message.strip()

        # Very short / simple messages → brief response
        if len(stripped) < SHORT_MESSAGE_CHARS:
            self._metrics.short_count += 1
            return SHORT_MAX_TOKENS

        # Code-related queries → longer response
        if CODE_KEYWORDS.search(stripped):
            self._metrics.code_count += 1
            return CODE_MAX_TOKENS

        # Detailed / explanatory queries → long response
        if DETAILED_KEYWORDS.search(stripped):
            self._metrics.detailed_count += 1
            return DETAILED_MAX_TOKENS

        # Multi-message context with long history → moderate-long response
        if history and len(history) >= 10:
            self._metrics.detailed_count += 1
            return DETAILED_MAX_TOKENS

        self._metrics.default_count += 1
        return DEFAULT_MAX_TOKENS

    @property
    def metrics(self) -> AdaptationMetrics:
        """Current adaptation metrics snapshot."""
        return self._metrics

    @staticmethod
    def system_prompt_instruction() -> str:
        """Instruction to inject into the system prompt about length adaptation."""
        return (
            "Adapt your response length to the user's query. "
            "Give brief, direct answers to simple or short questions. "
            "Provide detailed explanations only when the user asks for them."
        )
