"""
src/llm/cost_tracker.py — Token cost estimation and tracking per model.

Estimates USD cost from token usage using configurable per-model pricing.
Tracks cumulative cost per model and per chat for monitoring dashboards.

Usage::

    from src.llm.cost_tracker import TokenCostTracker

    tracker = TokenCostTracker()
    tracker.record("gpt-4o", input_tokens=1000, output_tokens=500, chat_id="chat_123")
    print(tracker.estimate_cost("gpt-4o", 1000, 500))  # 0.0075
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

# Default pricing: USD per 1M tokens (May 2025 rates).
DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "o3-mini": {"input": 1.10, "output": 4.40},
}


@dataclass(slots=True)
class ModelCostAccumulator:
    """Cumulative cost and token counters for a single model."""

    total_cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cost_usd": round(self.total_cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "request_count": self.request_count,
        }


class TokenCostTracker:
    """Thread-safe token cost tracker with per-model and per-chat breakdowns.

    Args:
        model_pricing: Optional override for ``DEFAULT_MODEL_PRICING``.
            Keys are model names; values are ``{"input": $/1M, "output": $/1M}``.
    """

    def __init__(
        self,
        model_pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._pricing = model_pricing or dict(DEFAULT_MODEL_PRICING)
        self._lock = threading.Lock()
        self._per_model: dict[str, ModelCostAccumulator] = {}
        self._per_chat: dict[str, float] = {}
        self._total_cost_usd: float = 0.0

    def _get_pricing(self, model: str) -> dict[str, float]:
        """Return pricing for *model*, falling back to gpt-4o defaults."""
        return self._pricing.get(model, self._pricing.get("gpt-4o", {"input": 2.50, "output": 10.00}))

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for a single request.

        Args:
            model: Model identifier (e.g. ``"gpt-4o"``).
            input_tokens: Number of prompt tokens.
            output_tokens: Number of completion tokens.

        Returns:
            Estimated cost in USD.
        """
        pricing = self._get_pricing(model)
        cost = (input_tokens / 1_000_000) * pricing["input"] + (
            output_tokens / 1_000_000
        ) * pricing["output"]
        return round(cost, 8)

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        chat_id: str | None = None,
    ) -> float:
        """Record token usage and return the estimated cost.

        Args:
            model: Model identifier.
            input_tokens: Prompt tokens consumed.
            output_tokens: Completion tokens produced.
            chat_id: Optional chat ID for per-chat cost tracking.

        Returns:
            Estimated cost in USD for this request.
        """
        cost = self.estimate_cost(model, input_tokens, output_tokens)
        with self._lock:
            acc = self._per_model.get(model)
            if acc is None:
                acc = ModelCostAccumulator()
                self._per_model[model] = acc
            acc.total_cost_usd += cost
            acc.input_tokens += input_tokens
            acc.output_tokens += output_tokens
            acc.request_count += 1
            self._total_cost_usd += cost
            if chat_id is not None:
                self._per_chat[chat_id] = self._per_chat.get(chat_id, 0.0) + cost
        return cost

    @property
    def total_cost_usd(self) -> float:
        """Total estimated cost across all models."""
        return self._total_cost_usd

    def get_per_model_costs(self) -> dict[str, dict[str, Any]]:
        """Return per-model cost breakdowns."""
        with self._lock:
            return {model: acc.to_dict() for model, acc in self._per_model.items()}

    def get_per_chat_costs(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Return top-N chats by cumulative cost, descending."""
        with self._lock:
            sorted_chats = sorted(
                self._per_chat.items(), key=lambda item: item[1], reverse=True
            )
            return [
                {"chat_id": cid, "cost_usd": round(cost, 6)}
                for cid, cost in sorted_chats[:top_n]
            ]

    def to_dict(self) -> dict[str, Any]:
        """Serialize full tracker state for health endpoint."""
        return {
            "total_cost_usd": round(self._total_cost_usd, 6),
            "per_model": self.get_per_model_costs(),
        }

    def update_pricing(self, model: str, input_per_1m: float, output_per_1m: float) -> None:
        """Update pricing for a specific model at runtime.

        Only affects future ``record()`` calls; past costs are unchanged.
        """
        with self._lock:
            self._pricing[model] = {"input": input_per_1m, "output": output_per_1m}
