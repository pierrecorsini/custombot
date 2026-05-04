<!-- Context: project/concepts/middleware-pipeline | Priority: high | Version: 2.0 | Updated: 2026-05-02 -->

# Concept: Message Middleware Pipeline

**Core Idea**: Incoming messages are processed through a 7-step configurable middleware chain (`MessagePipeline`) wrapped in an OTel span. Each concern (shutdown tracking, metrics, logging, preflight, typing, error handling, message handling) is a discrete middleware. Order and custom middleware paths are driven by `config.json`.

**Source**: `src/core/message_pipeline.py`

---

## Key Points

- **Configurable order**: `middleware_order` in config.json controls execution sequence
- **Extensible**: Custom middleware loaded from `extra_middleware_paths` via `module:factory` dotted paths
- **PipelineDependencies**: Typed DI — shutdown_mgr, session_metrics, bot, channel, verbose
- **MessageContext**: Mutable context carrying `IncomingMessage`, `op_id`, and `response`
- **OTel wrapped**: `execute()` wraps the entire chain in a `message_pipeline_span`
- **Default order**: operation_tracker → metrics → inbound_logging → preflight → typing → error_handler → handle_message

---

## Pipeline Flow (7-Step Default)

```
Application._on_message(msg)  [bounded semaphore]
  └─ pipeline.execute(MessageContext(msg))  [OTel span]
       ├─ 1. OperationTrackerMiddleware  — tracks in-flight ops for graceful shutdown
       ├─ 2. MetricsMiddleware           — increments session message count
       ├─ 3. InboundLoggingMiddleware    — logs message flow (direction=IN)
       ├─ 4. PreflightMiddleware         — bot preflight check (short-circuits if rejected)
       ├─ 5. TypingMiddleware            — sends typing indicator
       ├─ 6. ErrorHandlerMiddleware      — catches errors, sends user-facing error message
       ├─ 7. HandleMessageMiddleware     — bot.handle_message → channel.send_message
       └─ [Custom middleware from extra_middleware_paths]
```

---

## Built-in Middleware Registry

| Name | Factory | Purpose |
|------|---------|---------|
| `operation_tracker` | `_operation_tracker_factory` | Graceful shutdown in-flight tracking |
| `metrics` | `_metrics_factory` | Session message counter |
| `inbound_logging` | `_inbound_logging_factory` | Message flow logging |
| `preflight` | `_preflight_factory` | Bot preflight check (reject/allow) |
| `typing` | `_typing_factory` | Typing indicator before processing |
| `error_handler` | `_error_handler_factory` | Error catch + user notification |
| `handle_message` | `_handle_message_factory` | Core: bot → response → send |

---

## Custom Middleware

```python
from src.core.message_pipeline import MessageMiddleware, MessageContext

class MyMiddleware:
    async def __call__(self, ctx: MessageContext, call_next) -> None:
        # Pre-processing
        await call_next()  # Delegate to next middleware
        # Post-processing
```

Load via config: `"extra_middleware_paths": ["my_module:create_my_middleware"]`

---

## Codebase

- `src/core/message_pipeline.py` — Pipeline builder, 7 built-in middleware, config-driven builder
- `src/app.py` — `_build_pipeline()` constructs pipeline from config; `_on_message()` wraps with semaphore
- `src/config/config.py` — `middleware` config section

## Related

- `concepts/react-loop.md` — What HandleMessageMiddleware triggers
- `concepts/routing-engine.md` — How preflight uses routing
- `concepts/graceful-shutdown.md` — OperationTrackerMiddleware purpose
- `concepts/otel-tracing.md` — OTel span wrapping the pipeline
