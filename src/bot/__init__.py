"""
src.bot — Focused subpackage for the bot orchestrator.

Re-exports public symbols from submodules so that
``from src.bot import Bot`` continues to work as before.

Submodules:

- :mod:`src.bot._bot` — thin ``Bot`` orchestrator class
- :mod:`src.bot.preflight` — lightweight pre-filter checks
- :mod:`src.bot.crash_recovery` — stale message recovery
- :mod:`src.bot.react_loop` — ReAct (Reason + Act) loop
- :mod:`src.bot.context_building` — routing match + context assembly
- :mod:`src.bot.response_delivery` — post-ReAct response delivery pipeline
- :mod:`src.bot.turn_context` — immutable full-turn context snapshot
"""

from src.bot._bot import Bot, BotConfig, BotDeps
from src.bot.context_building import TurnContext
from src.bot.preflight import PreflightResult
from src.bot.turn_context import FullTurnContext

__all__ = [
    "Bot",
    "BotConfig",
    "BotDeps",
    "PreflightResult",
    "TurnContext",
    "FullTurnContext",
]
