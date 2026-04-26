"""
preflight.py — Lightweight message pre-filter checks.

Runs read-only validation (type check, empty, dedup, routing match)
before expensive processing begins. Used to decide whether to show
typing indicators to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.channels.base import IncomingMessage
from src.core.dedup import DeduplicationService
from src.routing import RoutingEngine

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class PreflightResult:
    """Immutable result of preflight filter checks.

    Returned by :func:`preflight_check` to indicate whether a message
    passed all read-only filters (validation, emptiness, dedup, routing).
    Used to decide whether to show typing indicators before expensive processing.
    """

    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


async def preflight_check(
    msg: IncomingMessage,
    dedup: DeduplicationService,
    routing: RoutingEngine | None,
) -> PreflightResult:
    """Run read-only filter checks before expensive processing.

    Performs lightweight checks (validation, empty, dedup, routing match)
    without side effects. Use before showing typing indicators to avoid
    revealing bot activity for messages that will be filtered out.

    Does NOT check rate limits (check_message_rate records timestamps).
    """
    if not isinstance(msg, IncomingMessage):
        return PreflightResult(passed=False, reason="invalid")

    if not msg.text or not msg.text.strip():
        return PreflightResult(passed=False, reason="empty")

    if await dedup.is_inbound_duplicate(msg.message_id):
        return PreflightResult(passed=False, reason="duplicate")

    if routing:
        matched_rule, _ = routing.match_with_rule(msg)
        if not matched_rule:
            return PreflightResult(passed=False, reason="no_routing_rule")

    return PreflightResult(passed=True)
