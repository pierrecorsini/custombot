"""
serialization.py — LLM message serialization utilities.

Standalone functions for converting OpenAI SDK message objects into
plain dicts suitable for the chat completions API wire format.
"""

from __future__ import annotations

from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
)


def serialize_tool_call_message(
    message: ChatCompletionMessage,
) -> ChatCompletionAssistantMessageParam:
    """Convert a tool-call assistant message to a plain dict for context."""
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in (message.tool_calls or [])
        ],
    }
