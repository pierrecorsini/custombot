<!-- Context: project-intelligence/concepts/architecture | Priority: high | Version: 3.9 | Updated: 2026-05-07 -->

# Architecture

> Native Python WhatsApp bot using ctypes bindings — no subprocess, no HTTP bridge.

## Core Pattern

```
Type: Native Python
Pattern: Direct ctypes bindings — Python calls whatsmeow (Go) via neonize
```

The native Python approach was chosen because:
- **neonize** wraps whatsmeow (Go) via ctypes — no subprocess, no HTTP bridge
- **Pure Python stack** eliminates Node.js dependency and subprocess management
- **Single SQLite session file** (`whatsapp_session.db`) replaces multi-file auth directories
- **Lower latency** — direct function calls instead of HTTP round-trips to a bridge

## Integration Points

| System | Purpose | Protocol | Direction |
|--------|---------|----------|-----------|
| LLM API | AI response generation | REST (OpenAI-compatible) | Outbound |
| WhatsApp (neonize) | Message send/receive | ctypes → Go → WebSocket | Bidirectional |
| SQLite | Conversation storage | File-based | Internal |
| Log Files | Debugging and monitoring | File write | Internal |

## Key Technical Decisions

| Decision | Rationale | Impact |
|----------|-----------|--------|
| neonize for WhatsApp | Native Python bindings to whatsmeow (Go), no subprocess | Pure Python stack, lower latency |
| SQLite for storage | Single-instance bot, no distributed requirements | Simple deployment, no external DB needed |
| Per-chat workspaces | Isolation between conversations | Clean separation, easier debugging |
| Rotating log files | Production-ready logging without disk overflow | Easy log management and rotation |
| .workspace/ for all runtime files | Centralized dynamic content | Clear separation of code vs data |

## Technical Constraints

| Constraint | Origin | Impact |
|------------|--------|--------|
| WhatsApp single device | WhatsApp limitation | Only one active session per number |
| Local files only | Architecture choice | No cloud sync, manual backup needed |

## Resilience Patterns

| Pattern | Module | Description |
|---------|--------|-------------|
| Error categorization | `src/app.py` | `_classify_main_loop_error()` maps exceptions to categories (LLM_TRANSIENT, CHANNEL_DISCONNECT, etc.) with EventBus emission |
| Zero-rule retention | `src/routing.py` | `load_rules()` retains previous rules when reload yields zero (handles transient empty-file states) |
| Truncation handling | `src/bot/react_loop.py` | `finish_reason='length'` returns user-visible warning |
| Resource cleanup on degradation | `src/builder.py` | Closes dedicated embed_http client when vector memory degrades |
| HealthCheckRegistry | `src/health/registry.py` | Centralized registry for health checks — replaces scattered `validate_connection()` / `get_llm_status()` / `get_dedup_stats()` accessors on Bot |
| NullMemoryMonitor | `src/monitoring/memory.py` | NullObject satisfying MemoryMonitor Protocol — eliminates downstream None-checks when psutil unavailable |
| StructuredContextFilter | `src/logging/logging_config.py` | `logging.Filter` auto-injecting correlation_id, chat_id, app_phase, session_id into every LogRecord |
| MessageValidator | `src/channels/message_validator.py` | Cohesive validation class with single `validate(raw: dict) -> IncomingMessage` entry point |
| Connection pooling (vector memory) | `src/builder.py` | Shared, long-lived `httpx.AsyncClient` with configurable connection pool limits for embedding HTTP calls |
| TTL eviction (LRULockCache) | `src/utils/` | Configurable TTL on `BoundedOrderedDict` — idle locks evicted, reclaiming memory from transient group chats |
| Per-skill circuit breaker | `src/core/tool_executor.py` | Per-skill-name `CircuitBreaker` prevents broken/hanging skills from consuming all ReAct loop iterations |
| EventBus backpressure | `src/core/event_bus.py` | Bounded semaphore caps concurrent handler invocations per emission |
| ComponentRegistry DI | `src/utils/registry.py` | Replaces mutable `field: X | None = None` bags with dict-backed store; surfaces missing deps at access time |
| RegistryBackedMixin | `src/utils/registry.py` | Shared mixin for `StartupContext`/`BuilderContext` — eliminates duplicated `__getattr__`/`__setattr__` attribute-forwarding boilerplate |
| SkillBreakerRegistry | `src/core/skill_breaker_registry.py` | Capped per-skill circuit breaker registry with LRU eviction — prevents unbounded memory growth from adversarial tool names |
| BotDeps injection | `src/builder.py` | BotDeps dataclass receives fully-wired collaborators — tests construct manually, production uses `build_bot()` |
| Batch inbound dedup | `src/core/dedup.py` | `batch_check_inbound(message_ids)` queries index once for all IDs during burst/crash-recovery |
| Shared error classification | `src/llm/_error_classifier.py` | `is_retryable(code)` helper replaces duplicated `_RETRYABLE_LLM_ERROR_CODES` across modules |
| Raw payload cap | `src/channels/message_validator.py` | `MAX_RAW_PAYLOAD_SIZE` (64 KB) strips oversized `IncomingMessage.raw` at boundary |
| Tracked flush futures | `src/db/db.py` | `_start_tracked_flush()` stores future + done-callback — prevents unhandled-exception warnings |
| `message_dropped` event | `src/bot/_bot.py` | Emits event on rate-limit — closes observability gap with other rejection paths |
| `send_and_track` guard | `src/channels/base.py` | Returns early on send failure — skips dedup recording and event emission |
| Scheduler `BaseException` guard | `src/scheduler/engine.py` | Catches `BaseException` separately — logs at CRITICAL + re-raises for clean state |
| Off-event-loop validation | `src/db/db.py` | `validate_connection()` via `asyncio.to_thread()` — no startup stall from filesystem I/O |
| HMAC audit event | `src/bot/_bot.py` | `error_occurred` event on HMAC failure — security alerting via event bus |

## Codebase References

- `main.py` — CLI entry point
- `src/bot/` — Main bot orchestrator (split into focused sub-modules)
- `src/llm.py` — LLM client wrapper
- `src/memory.py` — Conversation memory management
- `src/routing.py` — Message routing engine
- `channels/whatsapp.py` — WhatsApp channel via neonize
- `channels/base.py` — Channel base classes

## Related Files

- `lookup/tech-stack.md` — Full stack details with versions
- `lookup/project-structure.md` — Directory tree and key directories
- `lookup/decisions-log.md` — Full decision history with alternatives
- `concepts/business-domain.md` — Why this architecture exists
- `concepts/business-tech-bridge.md` — How business needs map to solutions
