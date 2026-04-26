"""
src.bot — Focused subpackage for the bot orchestrator.

Re-exports public symbols from :mod:`src.bot` (the ``Bot`` class) so that
``from src.bot import Bot`` continues to work as before.
"""

from src.bot._bot import Bot, BotConfig, PreflightResult, TurnContext

__all__ = [
    "Bot",
    "BotConfig",
    "PreflightResult",
    "TurnContext",
]
