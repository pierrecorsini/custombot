<!-- Context: project-intelligence/lookup/tech-stack | Priority: high | Version: 3.0 | Updated: 2026-04-06 -->

# Tech Stack

> Quick lookup for all technologies, versions, and their roles in the project.

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | 3.13+ | Modern async support, type hints, rich ecosystem |
| Framework | asyncio + Click | N/A | Async-first architecture with CLI interface |
| Database | SQLite (aiosqlite) | 3.x | Lightweight, embedded, perfect for single-instance bot |
| LLM | OpenAI-compatible API | N/A | Flexible model support via configurable endpoints |
| WhatsApp | neonize (whatsmeow/Go via ctypes) | Latest | WhatsApp Web API via native Python bindings |
| Key Libraries | httpx, rich, textual | Latest | HTTP client, terminal UI, TUI framework |

## Supporting Modules

| Module | Purpose | File |
|--------|---------|------|
| Circuit Breaker | Fault tolerance pattern | `src/circuit_breaker.py` |
| Rate Limiter | Request throttling | `src/rate_limiter.py` |
| Retry | Exponential backoff | `src/retry.py` |
| Message Queue | Persistence | `src/message_queue.py` |
| Exceptions | Custom error types | `src/exceptions.py` |
| Protocols | Type interfaces | `src/protocols.py` |
| Type Guards | Runtime type checking | `src/type_guards.py` |
| Constants | Named constants | `src/constants.py` |
| CLI Output | Colorful terminal output | `src/cli_output.py` |
| Progress | Progress indicators | `src/progress.py` |

## Codebase References

- `requirements.txt` — All Python dependencies
- `src/` — Core application modules
- `channels/` — Communication channel implementations

## Related Files

- `concepts/architecture.md` — How these technologies fit together
- `guides/dev-environment.md` — Setup instructions
- `lookup/project-structure.md` — Where everything lives in the codebase
