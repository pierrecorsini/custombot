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
from src.llm._error_classifier import classify_llm_error
from src.llm._provider import LLMProvider, TokenUsage

__all__ = [
    "LLMClient",
    "LLMProvider",
    "TokenUsage",
    "classify_llm_error",
]
