<!-- Context: project-intelligence/errors/bug-fixes | Priority: high | Version: 3.0 | Updated: 2026-04-06 -->

# Bug Fixes Applied

> Record of bugs fixed in the codebase — patterns to watch for in future.

## 2026-03-23 Fixes

### Fix 1: LLM usage dict access

- **File**: `src/llm.py:121`
- **Error**: `'dict' object has no attribute 'prompt_tokens'`
- **Root cause**: LLM API returns usage as dict in some responses, but code expected object attribute access
- **Fix**: Added handling for both dict and object access patterns in usage response
- **Pattern to watch**: When consuming external API responses, always handle both dict and object access patterns

### Fix 2: Unawaited coroutine in bot

- **File**: `src/bot.py:363,366`
- **Error**: `RuntimeWarning: coroutine was never awaited`
- **Root cause**: `_process_tool_calls` is async but was called without `await`
- **Fix**: Added `await` to `_process_tool_calls` calls
- **Pattern to watch**: When calling async functions in async context, always use `await`. RuntimeWarning about unawaited coroutines is a signal.

## Diagnostic Pattern

When similar issues appear:

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `AttributeError` on API response | Response format varies (dict vs object) | Use `.get()` or `getattr()` with fallback |
| `RuntimeWarning: coroutine was never awaited` | Missing `await` on async call | Add `await` keyword |
| `'dict' object has no attribute 'X'` | API response shape mismatch | Handle both access patterns |

## Codebase References

- `src/llm.py` — LLM client (Fix 1)
- `src/bot.py` — Bot orchestrator (Fix 2)

## Related Files

- `errors/known-issues.md` — Current open issues
- `guides/log-diagnostics.md` — How to find bugs via logs
- `lookup/completed-sessions.md` — Session where fixes were applied
