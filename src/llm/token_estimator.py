"""
llm.token_estimator — Token usage prediction before LLM calls.

Estimates token counts for text and message lists using the same
chars-per-token heuristic as ``context_builder.estimate_tokens``.
Provides budget checking so callers can trigger truncation or
summarisation before hitting model limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.constants import DEFAULT_CONTEXT_TOKEN_BUDGET

log = logging.getLogger(__name__)

# Approximate overhead per message in the OpenAI API format.
_PER_MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count for *text*.

    Uses the project-wide chars-per-token heuristic that accounts for
    CJK characters.  This is a re-export convenience so callers don't
    need to import from ``context_builder``.
    """
    from src.core.context_builder import estimate_tokens as _est

    return _est(text)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens across a list of API-format messages.

    Accounts for per-message overhead (role, separators) that the API
    adds on top of content tokens.
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        else:
            total += estimate_tokens(str(content))
        total += _PER_MESSAGE_OVERHEAD
    return total


@dataclass(slots=True)
class TokenEstimator:
    """Pre-flight token budget checker.

    Before sending to the LLM, estimate total tokens and compare
    against the model's context limit.  When approaching the limit
    (configurable warning threshold), log a warning.
    """

    model_limit: int = DEFAULT_CONTEXT_TOKEN_BUDGET
    warning_percent: float = 0.8

    def check_budget(
        self,
        messages: list[dict[str, Any]],
    ) -> TokenBudgetResult:
        """Estimate tokens and report budget status.

        Returns a ``TokenBudgetResult`` indicating whether the messages
        fit within the model limit, how many tokens are estimated, and
        whether the warning threshold has been crossed.
        """
        estimated = estimate_messages_tokens(messages)
        limit = self.model_limit
        within_limit = estimated <= limit
        warning_threshold = int(limit * self.warning_percent)
        over_warning = estimated >= warning_threshold

        if over_warning and within_limit:
            log.info(
                "Token budget warning: %d / %d tokens (%.0f%% of limit)",
                estimated,
                limit,
                (estimated / limit) * 100,
            )
        elif not within_limit:
            log.warning(
                "Token budget EXCEEDED: %d / %d tokens — truncation or "
                "summarisation required",
                estimated,
                limit,
            )

        return TokenBudgetResult(
            estimated_tokens=estimated,
            model_limit=limit,
            within_limit=within_limit,
            over_warning=over_warning,
        )

    def trim_to_budget(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove oldest non-system messages until within budget.

        System messages (role == "system") are never removed.
        Returns a new list — the input is not mutated.
        """
        result = self.check_budget(messages)
        if result.within_limit:
            return messages

        system: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system.append(msg)
            else:
                rest.append(msg)

        # Drop from the front (oldest) until within budget.
        while rest and not self.check_budget(system + rest).within_limit:
            rest.pop(0)

        log.info("Trimmed messages to %d non-system entries", len(rest))
        return system + rest


@dataclass(slots=True, frozen=True)
class TokenBudgetResult:
    """Outcome of a token budget check."""

    estimated_tokens: int
    model_limit: int
    within_limit: bool
    over_warning: bool
