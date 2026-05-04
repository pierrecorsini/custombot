<!-- Context: project/errors/bug-fixes | Priority: high | Version: 1.0 | Updated: 2026-04-06 -->

# Errors: Bug Fixes Log

**Source**: `project-intelligence/harvested-sessions.md` — Bug Fixes Applied 2026-03-23

---

## Bug Fix 1: Dict Attribute Error in LLM Usage Response

**Error**: `'dict' object has no attribute 'prompt_tokens'`
**Location**: `src/llm.py:121`
**Severity**: Medium — causes crash on LLM response parsing

### Root Cause
The LLM API response's `usage` field was sometimes returned as a plain dict instead of an object with attribute access. The code assumed object-style access (`usage.prompt_tokens`) but the API returned dict-style data.

### Fix
Added handling for both dict and object access patterns:
```python
# Before (crashed on dict):
prompt_tokens = usage.prompt_tokens

# After (handles both):
if isinstance(usage, dict):
    prompt_tokens = usage.get('prompt_tokens', 0)
else:
    prompt_tokens = getattr(usage, 'prompt_tokens', 0)
```

### Prevention
When accessing LLM response attributes, always handle both dict and object access patterns. Some OpenAI-compatible providers return raw dicts.

---

## Bug Fix 2: Unawaited Coroutine in Tool Processing

**Error**: `RuntimeWarning: coroutine was never awaited`
**Location**: `src/bot.py:363, 366`
**Severity**: High — causes silent failures in tool execution, messages never processed

### Root Cause
`_process_tool_calls` is an async method but was called without `await` in two places. The coroutine was created but never executed, causing the tool results to never be appended to the message history.

### Fix
Added `await` to both call sites:
```python
# Before (silent failure):
_process_tool_calls(messages, tool_calls, chat_id, ...)

# After (correct):
await _process_tool_calls(messages, tool_calls, chat_id, ...)
```

### Prevention
Always `await` async method calls. Use linting rules to catch unawaited coroutines.

---

## Codebase

- `src/llm.py` — LLM client with token tracking
- `src/bot.py` — Core ReAct loop orchestrator

## Related

- `concepts/react-loop.md` — Where tool processing fits in the pipeline
- `project-intelligence/harvested-sessions.md` — Source of these fixes
