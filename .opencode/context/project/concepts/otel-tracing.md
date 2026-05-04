<!-- Context: project/concepts/otel-tracing | Priority: medium | Version: 1.0 | Updated: 2026-05-02 -->

# Concept: OpenTelemetry Tracing

**Core Idea**: Custombot uses OpenTelemetry (OTel) spans throughout the message pipeline for distributed tracing — from LLM calls and tool execution to context assembly and ReAct iterations. Graceful degradation when OTel SDK is not installed.

**Source**: `src/monitoring/tracing.py`

---

## Key Points

- **Span helpers**: `llm_call_span()`, `react_loop_span()`, `tool_calls_span()`, `skill_execution_span()`, `context_assembly_span()` — context managers
- **Correlation IDs**: `set_correlation_id_on_span()` links all spans to the originating message
- **Graceful degradation**: No-ops when `opentelemetry-sdk` not installed (optional dependency)
- **Custom attributes**: `custombot.react.finish_reason`, `custombot.llm.latency_ms`, `custombot.skill.result_length`
- **Error recording**: `record_exception_safe()` captures exceptions on spans without crashing

---

## Span Hierarchy

```
message.process (per incoming message)
  ├─ context.assembly (rule matching + memory + instructions)
  ├─ react.loop (iteration 1..N)
  │    ├─ llm.call (with attempt tracking)
  │    ├─ tool.calls (parallel execution)
  │    │    └─ skill.execution (per tool call)
  │    └─ llm.call (next iteration)
  └─ [scheduled tasks use same structure without routing span]
```

---

## Quick Example

```python
# Span helper usage
with react_loop_span(chat_id, iteration, max_iter) as span:
    span.set_attribute("custombot.react.finish_reason", "stop")
    # ... ReAct iteration logic
```

---

## Codebase

- `src/monitoring/tracing.py` — All span helpers, tracer singleton
- `src/bot/react_loop.py` — Spans for LLM calls, tool execution
- `src/bot/_bot.py` — Spans for message processing, context assembly

## Related

- `guides/otel-tracing.md` — Setup and configuration guide
- `concepts/monitoring-metrics.md` — Prometheus metrics
