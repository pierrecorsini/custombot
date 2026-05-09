"""
src/ui/reaction_handler.py — WhatsApp message reaction feedback.

Accepts thumbs-up/down reactions as implicit signal about response quality.
Tracks per-chat reaction statistics and optionally triggers regeneration
on negative feedback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

THUMBS_UP = "\U0001f44d"  # 👍
THUMBS_DOWN = "\U0001f44e"  # 👎


@dataclass(slots=True)
class ReactionStats:
    """Per-chat reaction statistics."""

    positive: int = 0
    negative: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.negative


@dataclass(slots=True)
class ReactionConfig:
    """Configuration for reaction-based feedback."""

    enabled: bool = True
    auto_regenerate: bool = False


class ReactionHandler:
    """Process WhatsApp message reactions as response quality feedback."""

    def __init__(
        self,
        config: ReactionConfig | None = None,
        stats: dict[str, ReactionStats] | None = None,
    ) -> None:
        self._config = config or ReactionConfig()
        self._stats: dict[str, ReactionStats] = stats if stats is not None else {}

    def handle_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> str | None:
        """Process a reaction and return an optional action.

        Args:
            chat_id: Chat where the reaction was sent.
            message_id: ID of the reacted-to message.
            emoji: The reaction emoji.

        Returns:
            Action string ("regenerate", "log_positive", "log_negative")
            or None if reactions are disabled or emoji is not recognised.
        """
        if not self._config.enabled:
            return None

        stats = self._stats_for(chat_id)

        if emoji == THUMBS_UP:
            stats.positive += 1
            log.info("Positive reaction in chat %s on msg %s", chat_id, message_id)
            return "log_positive"

        if emoji == THUMBS_DOWN:
            stats.negative += 1
            log.info("Negative reaction in chat %s on msg %s", chat_id, message_id)
            if self._config.auto_regenerate:
                return "regenerate"
            return "log_negative"

        return None

    def get_stats(self, chat_id: str) -> ReactionStats:
        """Return reaction statistics for a chat."""
        return self._stats_for(chat_id)

    def _stats_for(self, chat_id: str) -> ReactionStats:
        if chat_id not in self._stats:
            self._stats[chat_id] = ReactionStats()
        return self._stats[chat_id]
