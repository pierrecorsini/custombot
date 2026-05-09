<!-- Context: project/lookup/openai-exceptions | Priority: high | Version: 1.0 | Updated: 2026-04-30 -->

# Lookup: OpenAI SDK Exception Reference

**Purpose**: Quick-reference for OpenAI Python SDK exception hierarchy, retryability, and handling
**Source**: `.tmp/external-context/openai/exception-hierarchy.md` (313 lines, distilled)

---

## Exception Hierarchy (Quick)

```
OpenAIError                              # Root base
├── APIError                             # Core HTTP base (message, body, code, param, type)
│   ├── APIStatusError                   # 4xx/5xx base (status_code, response, request_id)
│   │   ├── BadRequestError              # 400
│   │   ├── AuthenticationError          # 401 (+ OAuthError subclass)
│   │   ├── PermissionDeniedError        # 403
│   │   ├── NotFoundError                # 404
│   │   ├── ConflictError                # 409
│   │   ├── UnprocessableEntityError     # 422
│   │   ├── RateLimitError               # 429
│   │   └── InternalServerError          # 5xx (covers 500-599)
│   ├── APIConnectionError               # Network failure (no response)
│   │   └── APITimeoutError              # Request timeout
│   └── APIResponseValidationError       # 2xx with bad schema
├── LengthFinishReasonError              # max_tokens hit (carries completion)
├── ContentFilterFinishReasonError       # Content moderation rejected
└── WebSocketConnectionClosedError       # WS closed with unsent messages
```

---

## Retryability Classification

### Auto-Retried by SDK (max_retries=2 default)

| Exception | Status | Backoff |
|-----------|--------|---------|
| `APITimeoutError` | N/A | 0.5s × 2^n, cap 8s |
| `APIConnectionError` | N/A | 0.5s × 2^n, cap 8s |
| `RateLimitError` | 429 | Respects `retry-after` header |
| `ConflictError` | 409 | 0.5s × 2^n, cap 8s |
| `InternalServerError` | ≥500 | 0.5s × 2^n, cap 8s |

### Not Retried (raised immediately)

| Exception | Status | Action |
|-----------|--------|--------|
| `BadRequestError` | 400 | Fix request parameters |
| `AuthenticationError` | 401 | Check/refresh API key |
| `PermissionDeniedError` | 403 | Check key permissions |
| `NotFoundError` | 404 | Verify resource ID |
| `UnprocessableEntityError` | 422 | Review request semantics |

---

## CustomBot Error Handling Pattern

CustomBot classifies LLM errors via `src/llm_error_classifier.py` and wraps calls with a circuit breaker:

```python
# Simplified pattern from src/llm.py
try:
    response = await self._raw_chat(...)
except RateLimitError as e:
    self._breaker.record_failure()  # → may open circuit
    raise LLMError("rate_limited", retryable=True)
except AuthenticationError:
    raise LLMError("auth_failed", retryable=False)
except APIConnectionError:
    self._breaker.record_failure()
    raise LLMError("connection_failed", retryable=True)
except InternalServerError:
    self._breaker.record_failure()
    raise LLMError("server_error", retryable=True)
```

---

## Key Implementation Details

1. **`APITimeoutError` ⊂ `APIConnectionError`** — catching `APIConnectionError` also catches timeouts
2. **`InternalServerError` covers ALL 5xx** — no literal narrowing (500, 502, 503, 504)
3. **`InvalidWebhookSignatureError` ⊂ `ValueError`** — NOT caught by `except OpenAIError`
4. **SDK retries `max_retries + 1` total attempts** — exceptions only raised after all retries exhausted
5. **`request_id` on `APIStatusError`** — correlation ID for OpenAI support

---

## Codebase

- `src/llm.py` — LLM client with circuit breaker and error classification
- `src/llm_error_classifier.py` — Maps OpenAI exceptions to retryable/error categories
- `src/circuit_breaker.py` — Circuit breaker pattern (closed → open → half-open)

## Related

- `concepts/react-loop.md` — How LLM errors affect the ReAct loop
- `errors/bug-fixes.md` — Past LLM-related bugs (dict attribute error)
