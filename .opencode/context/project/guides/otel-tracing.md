<!-- Context: project/guides/otel-tracing | Priority: medium | Version: 1.0 | Updated: 2026-04-30 -->

# Guide: OpenTelemetry Tracing in CustomBot

**Purpose**: How OTel tracing is set up and used in this project
**Source**: `.tmp/external-context/opentelemetry-python/` (3 files, ~500 lines, distilled)

---

## Setup

CustomBot uses `opentelemetry-api` + `opentelemetry-sdk` (already in `pyproject.toml`):

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource

resource = Resource.create({"service.name": "custombot", "service.version": "1.0.0"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)
```

---

## Span Patterns Used

### 1. Context Manager (primary pattern)

```python
with tracer.start_as_current_span("react-iteration") as span:
    span.set_attribute("chat.id", chat_id)
    span.set_attribute("iteration.count", iteration)
    # ... work ...
    span.set_status(Status(StatusCode.OK))
```

### 2. Async Context Manager (auto-propagation)

OTel context propagates automatically through asyncio coroutines:

```python
async def handle_message(self, msg):
    with tracer.start_as_current_span("handle-message") as span:
        span.set_attribute("chat.id", msg.chat_id)
        result = await self._react_loop(...)  # child spans auto-nested
```

### 3. Error Recording

```python
with tracer.start_as_current_span("llm-call") as span:
    try:
        response = await self._raw_chat(...)
    except Exception as e:
        span.set_status(Status(StatusCode.ERROR, str(e)))
        span.record_exception(e)
        raise
```

---

## CustomBot Span Hierarchy

```
handle-message
├── react-iteration (1..N)
│   ├── context-assembly
│   │   ├── load-memory
│   │   ├── load-instructions
│   │   └── load-topic-cache
│   ├── llm-chat (or llm-chat-stream)
│   ├── tool-execution (if tool_calls)
│   │   └── skill-{name}
│   └── message-persist
└── send-response
```

---

## Span Attributes Reference

| Attribute | Type | Set On | Description |
|-----------|------|--------|-------------|
| `chat.id` | str | handle-message | Chat identifier |
| `iteration.count` | int | react-iteration | ReAct loop iteration number |
| `llm.model` | str | llm-chat | Model name used |
| `llm.tokens.prompt` | int | llm-chat | Prompt tokens consumed |
| `llm.tokens.completion` | int | llm-chat | Completion tokens generated |
| `skill.name` | str | tool-execution | Skill being executed |
| `skill.duration_ms` | float | tool-execution | Skill execution time |

---

## Shutdown (Critical)

**Always call `shutdown()`** before process exit — `BatchSpanProcessor` batches spans asynchronously:

```python
# In GracefulShutdown
tracer_provider = trace.get_tracer_provider()
tracer_provider.shutdown()  # flushes all buffered spans

# For intermediate flushes:
tracer_provider.force_flush(timeout_millis=5000)
```

### Shutdown Registration

```python
import atexit
atexit.register(lambda: trace.get_tracer_provider().shutdown())
```

---

## Processor Selection

| Processor | Behavior | Use In |
|-----------|----------|--------|
| `SimpleSpanProcessor` | Sync, immediate per-span | Dev, debug, ConsoleExporter |
| `BatchSpanProcessor` | Async batched, efficient | Production, OTLP exporters |

---

## Codebase

- `src/core/react_loop.py` — ReAct iteration spans (`react-iteration`)
- `src/llm.py` — LLM call spans (`llm-chat`, `llm-chat-stream`)
- `src/core/tool_executor.py` — Skill execution spans (`tool-execution`)
- `src/lifecycle.py` — TracerProvider setup and shutdown
- `src/bot/_bot.py` — Top-level `handle-message` span

## Related

- `concepts/monitoring-metrics.md` — Performance metrics alongside tracing
- `concepts/graceful-shutdown.md` — Where OTel shutdown fits in teardown order
