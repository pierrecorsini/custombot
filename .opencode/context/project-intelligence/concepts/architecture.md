<!-- Context: project-intelligence/concepts/architecture | Priority: high | Version: 3.1 | Updated: 2026-05-03 -->

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

## Codebase References

- `main.py` — CLI entry point
- `src/bot.py` — Main bot orchestrator
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
