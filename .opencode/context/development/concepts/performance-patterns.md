<!-- Context: development/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-27 -->

# Concept: Performance Patterns

**Purpose**: Optimize by rejecting invalid inputs early

---

## Core Idea

Fail fast: validate inputs at function entry and return early for invalid data. Avoids expensive operations (LLM calls, DB queries) on data that won't succeed anyway.

---

## Key Points

- Validate inputs at function entry
- Return early for invalid/empty inputs
- Skip LLM calls for empty messages
- Cache expensive computations where safe
- Profile before optimizing

---

## Quick Example

```python
async def handle_message(message: str, user_id: str) -> Response:
    # Early rejection - save expensive LLM call
    if not message or not message.strip():
        return Response(error="Empty message")
    
    if not is_valid_user(user_id):
        return Response(error="Invalid user")
    
    # Only now do expensive work
    return await llm_process(message)


def process_config(config: dict) -> Result:
    # Validate all required fields upfront
    required = ['api_key', 'model', 'timeout']
    missing = [f for f in required if f not in config]
    if missing:
        return Result(error=f"Missing: {missing}")
    
    # Safe to proceed
    return do_work(config)
```

---

## Benefits

- Saves CPU cycles on invalid data
- Reduces unnecessary LLM/API calls
- Clearer error messages at boundaries
- Easier debugging (fail at entry point)

---

## Related

- concepts/memory-safety-patterns.md
- examples/rate-limiter-bounded.md

**Source**: Harvested from session 2026-03-26-code-optimization
