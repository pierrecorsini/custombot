<!-- Context: project-intelligence/lookup/tech-stack | Priority: high | Version: 3.1 | Updated: 2026-05-02 -->

# Tech Stack

> Quick lookup for all technologies, versions, and their roles in the project.

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | 3.11+ | Async/await, performance, type hints |
| Framework | asyncio + Click | stdlib/8.3+ | Async-first architecture with CLI interface |
| Database | SQLite + sqlite-vec | stdlib/0.1 | Embedded DB with vector search |
| LLM | OpenAI SDK | 2.29+ | Chat completions with tool calling + streaming |
| WhatsApp | neonize (whatsmeow/Go) | 0.3+ | WhatsApp Web API via native Python bindings |
| Serialization | orjson + msgpack | 3.10+/1.1+ | Fast JSON + binary serialization |
| Terminal UI | Rich + questionary | 14.3+/2.1+ | Formatted output, interactive menus |
| Search | duckduckgo-search | 8.1+ | Web search skill |
| Tracing | OpenTelemetry | 1.30+ | Distributed tracing (optional) |
| File Watching | watchdog | 4.0+ | OS-native file change detection |
| Hashing | xxhash | 3.5+ | Fast non-cryptographic hashing |
| Testing | pytest + pytest-asyncio | 9.0+/1.3+ | Async test support, coverage, hypothesis |
| Linting | ruff + mypy | 0.15+/1.20+ | Fast linting, type checking |

## Supporting Modules

| Module | Purpose | File |
|--------|---------|------|
| Circuit Breaker | Fault tolerance | `src/utils/circuit_breaker.py` |
| Rate Limiter | Request throttling | `src/rate_limiter.py` |
| Retry | Exponential backoff | `src/utils/retry.py` |
| Message Queue | Crash recovery persistence | `src/message_queue.py` |
| Exceptions | Domain error hierarchy | `src/exceptions.py` |
| Protocols | Type interfaces | `src/utils/protocols.py`, `src/llm_provider.py` |
| Type Guards | Runtime type checking | `src/utils/type_guards.py` |
| Constants | Named constants (14 domains) | `src/constants/` |
| CLI Output | Terminal output | `src/ui/cli_output.py` |
| Progress | Spinner + progress bar | `src/progress.py` |
| Diagnostics | Self-service checks | `src/diagnose.py` |
| Workspace Integrity | Startup verification | `src/workspace_integrity.py` |

## Codebase References

- `pyproject.toml` — All Python dependencies and project config
- `requirements-lock.txt` — Hash-locked dependencies
- `src/` — Core application modules

## Related Files

- `concepts/architecture.md` — How these technologies fit together
- `guides/dev-environment.md` — Setup instructions
- `lookup/project-structure.md` — Where everything lives in the codebase
