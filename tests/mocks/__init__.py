"""LLM response mock library for testing.

Provides configurable mock LLM responses and a MockLLMProvider
for use in tests instead of real LLM calls.

Usage::

    from tests.mocks import MockLLMProvider
    from tests.mocks.llm_responses import make_text_response, make_tool_call

    provider = MockLLMProvider(default_response=make_text_response("Hello!"))
"""
