"""
llm.fallback — Model fallback on LLM failure.

When an LLM call fails, automatically retries with the next model in a
configured fallback list before surfacing the error to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionMessageParam,
        ChatCompletionToolParam,
    )
    from src.llm import LLMProvider

log = logging.getLogger(__name__)

MAX_FALLBACK_ATTEMPTS = 2


@dataclass(slots=True)
class FallbackResult:
    """Result of a fallback-aware LLM call."""

    completion: ChatCompletion
    model_used: str
    fallback_attempted: bool = False
    original_model: str = ""
    fallback_models_tried: list[str] = field(default_factory=list)


class FallbackModelManager:
    """Retries LLM calls with fallback models on failure.

    Configured with an ordered list of fallback model names.  On failure,
    retries up to ``MAX_FALLBACK_ATTEMPTS`` times with the next available
    model before surfacing the error.
    """

    def __init__(self, fallback_models: list[str] | None = None) -> None:
        self._fallback_models = fallback_models or []

    @property
    def fallback_models(self) -> list[str]:
        return list(self._fallback_models)

    async def call_with_fallback(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        *,
        tools: list[ChatCompletionToolParam] | None = None,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> FallbackResult:
        """Call the LLM, falling back to alternate models on failure.

        The primary model from the LLM config is tried first.  On exception,
        subsequent models from the fallback list are tried up to
        ``MAX_FALLBACK_ATTEMPTS`` times.
        """
        original_model = _get_model_name(llm)

        # Try the primary model first
        try:
            completion = await llm.chat(messages, tools=tools, chat_id=chat_id, timeout=timeout)
            return FallbackResult(
                completion=completion,
                model_used=original_model,
            )
        except Exception as primary_exc:
            if not self._fallback_models:
                raise

            log.warning(
                "Primary model %s failed: %s — trying %d fallback models",
                original_model,
                type(primary_exc).__name__,
                min(len(self._fallback_models), MAX_FALLBACK_ATTEMPTS),
            )

            models_tried: list[str] = []
            last_exc = primary_exc

            for model_name in self._fallback_models[:MAX_FALLBACK_ATTEMPTS]:
                models_tried.append(model_name)
                try:
                    _swap_model(llm, model_name)
                    log.info("Fallback attempt: trying model %s", model_name)
                    completion = await llm.chat(
                        messages, tools=tools, chat_id=chat_id, timeout=timeout
                    )
                    log.info(
                        "Fallback succeeded with model %s (original: %s)",
                        model_name,
                        original_model,
                    )
                    return FallbackResult(
                        completion=completion,
                        model_used=model_name,
                        fallback_attempted=True,
                        original_model=original_model,
                        fallback_models_tried=models_tried,
                    )
                except Exception as fallback_exc:
                    last_exc = fallback_exc
                    log.warning(
                        "Fallback model %s also failed: %s",
                        model_name,
                        type(fallback_exc).__name__,
                    )
                finally:
                    _swap_model(llm, original_model)

            raise last_exc


def _get_model_name(llm: LLMProvider) -> str:
    """Extract the current model name from an LLMProvider."""
    cfg = getattr(llm, "_cfg", None)
    if cfg is not None:
        return getattr(cfg, "model", "unknown")
    return "unknown"


def _swap_model(llm: LLMProvider, model_name: str) -> None:
    """Temporarily swap the model name on the provider's config."""
    cfg = getattr(llm, "_cfg", None)
    if cfg is not None:
        object.__setattr__(cfg, "model", model_name)
