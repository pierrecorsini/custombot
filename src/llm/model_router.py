"""
llm.model_router — Multi-model routing for different message types.

Routes messages to specialised models based on keyword patterns defined
in routing rules.  Falls back to a default model when no rule matches.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field


log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class RoutingRule:
    """A single pattern → model mapping."""

    pattern: str
    model: str
    _compiled: re.Pattern | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "_compiled", re.compile(self.pattern, re.IGNORECASE))
        except re.error:
            log.warning("Invalid model routing pattern: %s", self.pattern)
            object.__setattr__(self, "_compiled", None)

    def matches(self, text: str) -> bool:
        """Return True when *text* matches this rule's pattern."""
        if self._compiled is None:
            return self.pattern.lower() in text.lower()
        return bool(self._compiled.search(text))


# Sensible defaults when no custom rules are configured.
_DEFAULT_RULES: list[dict[str, str]] = [
    {"pattern": r"\b(code|function|debug|error|python|javascript|typescript|sql|api|bug)\b", "model": "code"},
    {"pattern": r"\b(write|story|poem|creative|imagine|compose)\b", "model": "creative"},
]


class ModelRouter:
    """Selects the best model for a given message text.

    Evaluates routing rules in order; the first match wins.  When no
    rule matches the default model is returned.
    """

    def __init__(
        self,
        rules: list[dict[str, str]] | None = None,
        default_model: str = "",
        model_map: dict[str, str] | None = None,
    ) -> None:
        """Initialise the router.

        Args:
            rules: List of ``{"pattern": ..., "model": ...}`` dicts.
                When *None*, built-in defaults for code/creative are used.
            default_model: Model identifier returned when no rule matches.
            model_map: Maps symbolic model names (e.g. ``"code"``) to
                actual model identifiers.  When a rule's ``model`` value
                appears as a key here, the mapped value is returned.
        """
        raw = rules if rules is not None else _DEFAULT_RULES
        self._rules: list[RoutingRule] = [RoutingRule(p=r["pattern"], model=r["model"]) for r in raw]
        self._default_model = default_model
        self._model_map = model_map or {}

    def select_model(self, text: str) -> str:
        """Return the model identifier best suited for *text*.

        Evaluates rules in definition order; first match wins.  Symbolic
        names from rules are resolved through *model_map* when present.
        """
        for rule in self._rules:
            if rule.matches(text):
                resolved = self._model_map.get(rule.model, rule.model)
                if resolved != self._default_model:
                    log.debug("Model routing: pattern=%s → %s", rule.pattern, resolved)
                return resolved
        return self._default_model

    def update_rules(self, rules: list[dict[str, str]]) -> None:
        """Replace routing rules (e.g. during hot-reload)."""
        self._rules = [RoutingRule(p=r["pattern"], model=r["model"]) for r in rules]
