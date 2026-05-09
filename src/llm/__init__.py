"""
src.llm — LLM client and provider package.

Provides:
  - LLMClient: OpenAI-compatible async LLM client
  - LLMProvider: Protocol interface for LLM backends
  - TokenUsage: Token usage statistics
  - classify_llm_error: OpenAI SDK exception classifier

Re-exports public symbols so that ``from src.llm import LLMClient`` and
``from src.llm import TokenUsage`` continue to work as before.
"""

from src.llm._client import LLMClient
from src.llm._error_classifier import (
    RETRYABLE_LLM_ERROR_CODES,
    classify_llm_error,
    is_retryable,
)
from src.llm._provider import LLMProvider, TokenUsage

__all__ = [
    "LLMClient",
    "LLMProvider",
    "RETRYABLE_LLM_ERROR_CODES",
    "TokenUsage",
    "classify_llm_error",
    "is_retryable",
]

# ── Lazy imports for optional modules ──────────────────────────────────────
# Imported via ``from src.llm.reflection import ResponseReflector`` etc.
# Listed here for discoverability:
#   src.llm.reflection    — ResponseReflector
#   src.llm.tool_selector — DynamicToolSelector, select_tools
#   src.llm.topic_detector — TopicDetector
#   src.llm.structured_output — StructuredOutputManager
#   src.llm.fallback          — FallbackModelManager
#   src.llm.context_compressor — ContextCompressor
#   src.llm.loop_strategies   — LoopStrategy, get_strategy
#   src.llm.response_cache    — LLMResponseCache
#   src.llm.tool_validator    — ToolResultValidator
