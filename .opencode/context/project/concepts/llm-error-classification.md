<!-- Context: project/concepts/llm-error-classification | Priority: high | Version: 1.0 | Updated: 2026-04-30 -->

# Concept: LLM Error Classification

**Core Idea**: CustomBot classifies every LLM error into retryable vs non-retryable categories, integrates with a circuit breaker to prevent cascading failures, and supports health-check-driven failover for automatic recovery.

**Source**: `src/llm_error_classifier.py`, `src/llm.py`, `src/circuit_breaker.py`

---

## Key Points

- **Three-tier error handling**: OpenAI SDK retries → CustomBot circuit breaker → structured LLMError classification
- **Circuit breaker states**: Closed (normal) → Open (failing, reject fast) → Half-Open (probe recovery)
- **Health-check failover**: Proactive endpoint polling auto-closes breaker on recovery instead of waiting for full cooldown
- **Streaming parity**: Error classification shared between `chat()` and `chat_stream()` via dedicated module

---

## Classification Map

```
OpenAI Exception          →  LLMError Type         →  Retryable?
─────────────────────────────────────────────────────────────────
APITimeoutError           →  connection_failed      →  ✅ Yes
APIConnectionError        →  connection_failed      →  ✅ Yes
RateLimitError (429)      →  rate_limited           →  ✅ Yes (after cooldown)
InternalServerError (5xx) →  server_error           →  ✅ Yes
ConflictError (409)       →  conflict               →  ✅ Yes
BadRequestError (400)     →  bad_request            →  ❌ No
AuthenticationError (401) →  auth_failed            →  ❌ No
PermissionDeniedError     →  permission_denied      →  ❌ No
NotFoundError (404)       →  not_found              →  ❌ No
UnprocessableEntity (422) →  invalid_request        →  ❌ No
```

---

## Circuit Breaker Integration

```python
# Simplified flow in src/llm.py
if self._breaker.is_open:
    raise LLMError("circuit_open", retryable=True)

try:
    response = await self._raw_chat(...)
    self._breaker.record_success()      # → may close circuit
except RetryableError:
    self._breaker.record_failure()      # → may open circuit
    raise
```

### State Transitions

```
CLOSED ──(failure threshold)──→ OPEN ──(cooldown expires)──→ HALF-OPEN
   ↑                                                          │
   └──────────────(probe succeeds)────────────────────────────┘
                              ┌──(probe fails)──→ OPEN (reset cooldown)
```

---

## Error Flow Through ReAct Loop

```
Bot.handle_message()
  └── ReActLoop.run()
       └── LLMClient.chat()
            ├── Circuit breaker open? → return error message
            ├── SDK retries (max_retries=2) → exhausted → raise
            ├── Classifier maps exception → LLMError(retryable=True/False)
            ├── retryable=True → retry_with_backoff(max_retries=3)
            └── retryable=False → log + return error to user
```

---

## Codebase

- `src/llm_error_classifier.py` — Exception → error type mapping
- `src/llm.py` — Circuit breaker integration, retry logic
- `src/circuit_breaker.py` — State machine (closed/open/half-open)
- `src/utils/retry.py` — `retry_with_backoff()` decorator

## Related

- `lookup/openai-exceptions.md` — Full OpenAI exception hierarchy reference
- `concepts/react-loop.md` — How errors affect the processing pipeline
- `concepts/monitoring-metrics.md` — LLM latency tracking
