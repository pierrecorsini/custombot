"""
bot/turn_context.py — Immutable snapshot of all state for a single bot turn.

Captures the complete context needed to process one user message:
incoming message, assembled LLM messages, workspace path, and routing
metadata. Frozen after creation to prevent accidental mutation across
the ReAct loop iterations.

Complements the existing TurnContext in context_building.py (which is
focused on routing + context assembly) by providing the full-turn
snapshot used by the response delivery pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class FullTurnContext:
    """Immutable snapshot of all state needed for a single turn.

    Built once after context assembly and passed through the response
    pipeline.  Frozen to guarantee no downstream code mutates state
    that other parts of the pipeline depend on.
    """

    msg: Any  # IncomingMessage — using Any to avoid circular imports
    messages: tuple[Any, ...]  # tuple of ChatMessage for immutability
    workspace_dir: str
    rule_id: str
    chat_id: str
    show_errors: bool = False
    skill_exec_verbose: str = ""
    extra: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_lists(
        cls,
        msg: Any,
        messages: list[Any],
        workspace_dir: str,
        rule_id: str,
        chat_id: str,
        *,
        show_errors: bool = False,
        skill_exec_verbose: str = "",
        **extra: Any,
    ) -> FullTurnContext:
        """Construct from mutable lists, snapshotting into tuples."""
        return cls(
            msg=msg,
            messages=tuple(messages),
            workspace_dir=workspace_dir,
            rule_id=rule_id,
            chat_id=chat_id,
            show_errors=show_errors,
            skill_exec_verbose=skill_exec_verbose,
            extra=tuple(extra.items()),
        )
