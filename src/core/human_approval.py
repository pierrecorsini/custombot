"""
src/core/human_approval.py — Human-in-the-loop approval for dangerous skills.

Before executing a skill marked as ``dangerous``, the ToolExecutor sends a
confirmation prompt to the user via the channel and waits for explicit
approval (with a configurable timeout).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 60


@dataclass(slots=True)
class PendingApproval:
    """Tracks a single pending approval request for a chat."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    skill_name: str = ""
    args_summary: str = ""


class ApprovalManager:
    """Manages pending human-approval requests keyed by chat_id.

    Only one approval can be pending per chat at a time.  The manager
    exposes :meth:`request_approval` (used by ToolExecutor) and
    :meth:`resolve_approval` (called when the user replies).
    """

    def __init__(self, timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds
        self._pending: dict[str, PendingApproval] = {}

    def resolve(self, chat_id: str, approved: bool) -> None:
        """Resolve a pending approval for *chat_id*."""
        entry = self._pending.get(chat_id)
        if entry is None:
            log.warning("No pending approval for chat %s", chat_id)
            return
        entry.approved = approved
        entry.event.set()

    async def request_approval(
        self,
        chat_id: str,
        skill_name: str,
        args_summary: str,
        send_message: Callable[[str, str], Awaitable[None]],
    ) -> bool:
        """Send confirmation prompt and wait for the user's response.

        Returns ``True`` if approved, ``False`` if denied or timed out.
        """
        entry = PendingApproval(skill_name=skill_name, args_summary=args_summary)
        self._pending[chat_id] = entry

        prompt = (
            f"⚠️ About to execute **{skill_name}** with: `{args_summary}`.\n"
            f"Reply **yes** to confirm or **no** to cancel."
        )
        try:
            await send_message(chat_id, prompt)
        except Exception:
            log.error("Failed to send approval prompt for chat %s", chat_id)
            self._pending.pop(chat_id, None)
            return False

        try:
            await asyncio.wait_for(entry.event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            log.info("Approval timed out for skill %s in chat %s", skill_name, chat_id)
            return False
        finally:
            self._pending.pop(chat_id, None)

        return entry.approved

    @property
    def pending_count(self) -> int:
        return len(self._pending)
