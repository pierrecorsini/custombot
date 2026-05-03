<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.5 | Updated: 2026-05-03 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-03

## Quick Reference
**Update Triggers**: Tech stack changes | New patterns | Architecture decisions
**Audience**: Developers, AI agents

---

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | >=3.11 | Async/await, dataclasses, slots, type hints |
| Runtime | asyncio | stdlib | Event-driven message processing |
| LLM Client | openai | ~=2.29 | Supports OpenAI, Anthropic proxy, Ollama, OpenRouter, Groq |
| Channel | neonize | 0.3.17.post0 | Native WhatsApp Web client (exact-pinned, QR pairing, session persistence) |
| CLI | Click | ~=8.3 | Command groups, options, help text |
| Display | Rich | ~=14.3 | Terminal formatting, spinners, progress bars |
| HTTP | aiohttp | ~=3.13 | Async HTTP client for web search and external APIs |
| Search | duckduckgo-search | ~=8.1 | Web search integration for skills |
| Database | SQLite | stdlib | 3 databases: main (.data/), vector_memory, projects; connection pooling via sqlite_pool.py |
| Vector Search | sqlite-vec | 0.1.9 | Semantic memory with cosine similarity; detects embedding model changes across restarts |
| Serialization | orjson + msgpack | latest | Fast JSON + binary encoding |
| Hashing | xxhash | ~=3.5 | Deduplication hash computation; outbound dedup in scheduler |
| Logging | stdlib + Rich + OTel | 1.30 | Structured logs, correlation IDs, OpenTelemetry spans |
| Config | JSON + dataclasses + watchdog | >=4.0 | Hot-reload via atomic config swap pattern |
| Health | HTTP (stdlib) | — | Prometheus + JSON endpoints, configurable host (default localhost), rate-limited |
| Process | psutil | ~=6.0 | System resource monitoring |
| Linting | Ruff | >=0.15 | Combined linter + formatter (replaces flake8, black, isort) |
| Typing | mypy | >=1.20 | Gradual strict mode (`disallow_untyped_defs = false`) |
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks (72 test files); dev extras in pyproject.toml |

---

## Code Patterns

### Async LLM Client
```python
# OpenAI-compatible provider with circuit breaker + retry
from src.llm_provider import LLMProvider

provider = LLMProvider(config.llm)
response: str = await provider.chat(messages=[...], tools=[...])
# Supports streaming, tool calls, warmup probes, health checks
# Public config update: provider.update_config(new_llm_cfg)
```

### Config Hot-Reload (Atomic Swap Pattern)
```python
# Bot, LLMProvider, ContextAssembler expose public update_config() methods
# ConfigChangeApplier swaps the entire Config reference atomically
# (not field-by-field mutation) so concurrent coroutines never see
# a partially-updated config under the GIL.
bot.update_config(new_bot_cfg)              # validated, logged
provider.update_config(new_llm_cfg)         # temperature bounds, non-empty model
context_assembler.update_config(new_cfg)    # propagates to context assembly
```

### SQLite Connection Pooling
```python
# Reusable pool for SQLite; thread-safe lifecycle. Used by db.py, message_store.py, vector_memory
from src.db.sqlite_pool import ...
```

### Bounded Concurrency
```python
# App._on_message() → asyncio.Semaphore | DedupService → BoundedOrderedDict(ttl=...)
# TaskScheduler → _tasks_dirty flag | Memory → known chat dir cache to skip os calls
```

### Resilience & Error Categorization
```python
# _classify_main_loop_error() → LLM_TRANSIENT, CHANNEL_DISCONNECT, etc.
# EventBus emits EVENT_ERROR_OCCURRED | Routing retains previous rules on zero-reload
# React loop: finish_reason='length' → truncation warning | Context vars reset in finally
```

### Health Server Middleware Stack
```python
# Layered (cheapest first): method → path (HEALTH_ALLOWED_PATHS) → size → rate limit → HMAC
# SecretRedactingFilter scrubs HMAC tokens from all log output
```

### Structured Dependency Injection
```python
@dataclass(frozen=True)      # Immutable containers (config, results)
class BotComponents:
    bot: Bot; db: Database; llm: LLMProvider

@dataclass(slots=True)       # Parameter bag (replaces 15-param Bot constructor)
class BotDeps:
    config: BotConfig; db: Database; llm: LLMProvider
    memory: MemoryProtocol; skills: SkillRegistry
    routing: RoutingEngine | None = None      # Optional — Bot supplies fallbacks
    dedup: DeduplicationService | None = None
```

### Module Structure (every file follows this)
```python
"""module.py — One-line purpose."""
from __future__ import annotations
import logging; from typing import TYPE_CHECKING
if TYPE_CHECKING: from src.config import Config
log = logging.getLogger(__name__)
__all__ = ["PublicClass", "public_function"]
```

### Exception Hierarchy
```python
from src.exceptions import LLMError, ConfigurationError
raise LLMError("API timeout", provider="openai", model="gpt-4")
# .to_user_message() → formatted with emoji + ref code + docs link
```

---

## Naming Conventions

| Type | Convention | Example |
|------|-----------|---------|
| Files | snake_case | `message_queue.py`, `config_watcher.py` |
| Classes | PascalCase | `BotComponents`, `AppPhase`, `IncomingMessage` |
| Functions | snake_case | `build_bot()`, `perform_shutdown()` |
| Constants | UPPER_SNAKE_CASE | `WORKSPACE_DIR`, `MEMORY_FILENAME` |
| Private | Leading underscore | `_run_bot()`, `_setup_logging()` |
| Directories | snake_case | `vector_memory/`, `message_queue.py` |
| Tests | test_ prefix | `test_routing.py`, `test_config.py` |

---

## Code Standards

- `from __future__ import annotations` on line 1 of every file (after docstring)
- `log = logging.getLogger(__name__)` — module-level logger in every module
- `TYPE_CHECKING` guard for type-only imports (avoid circular deps)
- `__all__` exports in all public modules (100% coverage)
- Frozen dataclasses for immutable data; `slots=True` for mutable state
- Structured dependency injection: `BotDeps`, `ShutdownContext`, `BuilderContext` replace multi-param constructors
- Protocol classes for dependency injection boundaries
- Step orchestrator pattern for multi-phase startup/build (declarative `ComponentSpec`)
- Config hot-reload uses atomic reference swap (single assignment, not field-by-field mutation; `Application._swap_config()`)
- Connection pooling abstraction for SQLite (sqlite_pool.py, file_pool.py)
- Health server: layered middleware stack (path → method → size → rate limit → HMAC auth) with `HEALTH_ALLOWED_PATHS` frozenset
- Offload blocking I/O to threads via `asyncio.to_thread()` (e.g., Memory log recovery)
- Snapshot mutable state before use (scheduler snapshots task dict fields)
- TOCTOU-safe file operations with `os.O_EXCL` atomic open
- Double quotes for strings (ruff format), line length 100
- Docstrings: triple-double-quoted, Google-style Args/Returns
- Dev dependencies consolidated in pyproject.toml `[dev]` extras (no separate requirements-dev.txt)

---

## Security Requirements

- Path validation: all file access sandboxed to workspace directory (`is_path_in_workspace`)
- Instruction loader: `_validate_path()` rejects directory components and resolved-path escapes
- URL sanitization: strip credentials from logged URLs (`sanitize_url_for_logging`)
- Prompt injection detection: classify and reject adversarial inputs; strip inline `(?i)` flags from combined regex
- Secret redaction: `Config.__repr__()` uses `_redact_secrets()` to mask API keys
- Config file permission check: warn if config.json readable by group/others (Unix)
- Input validation: `IncomingMessage` fields validated before use; `ConfigurationError` raised on invalid config
- Defense-in-depth: 6-module security subsystem (`src/security/`)
- Supply-chain: Dockerfile pins base image by digest, neonize/sqlite-vec exact-pinned
- Health server: binds to configurable host (default 127.0.0.1 only), rate-limited endpoints
- TOCTOU-safe workspace seeding: uses `os.O_EXCL` atomic open for file creation

---

## 📂 Codebase References

**Entry Point**: `main.py` — Click CLI: `start`, `options`, `diagnose` (+ `--health-host`)
**App Lifecycle**: `src/app.py` — `Application` + `AppPhase` state machine + error categorization + config swap
**Builder**: `src/builder.py` — `build_bot()` → `BotComponents` (public API)
**Bot Core**: `src/bot/` — `_bot.py` (Bot+BotDeps, handle_message, outbound dedup), `crash_recovery.py`, `preflight.py`, `react_loop.py` (truncation handling)
**Config**: `src/config/` — Schema defs, loader, validation, `config_watcher.py` (atomic swap, 5 modules)
**LLM**: `src/llm.py`, `src/llm_provider.py`, `src/llm_error_classifier.py` — Async client + circuit breaker + error classification
**Memory**: `src/memory.py` — Per-chat `MEMORY.md` files + chat dir caching + async recovery via to_thread
**Routing**: `src/routing.py` — YAML frontmatter rules + retry on transient parse failures + zero-rule graceful degradation
**Message Queue**: `src/message_queue.py`, `src/message_queue_persistence.py` — Streaming JSONL parsing
**Scheduler**: `src/scheduler.py` — Cached time-to-next-due via _tasks_dirty flag; outbound dedup
**Skills**: `src/skills/` — `BaseSkill` (Python) + prompt-based skills (Markdown)
**Security**: `src/security/` — Path validator, prompt injection, URL sanitizer, audit, signing (6 modules)
**Health**: `src/health/` — server.py, middleware.py (path/method/size validation, HMAC, rate limiting, secret redaction), prometheus.py, checks.py, models.py (6 modules, configurable host)
**DB**: `src/db/` — sqlite_pool.py, file_pool.py, migration, message_store, generations (12 modules)
**Monitoring**: `src/monitoring/` — Performance, memory, tracing, workspace monitor
**Other**: `src/workspace_integrity.py`, `src/core/startup.py`, `src/rate_limiter.py`, `src/lifecycle.py`, `src/shutdown.py`
**Tests**: `tests/` — 72 test files; dev extras in pyproject.toml
**Build**: `pyproject.toml`, `Makefile` (pip-compile), `requirements.txt`, `.pre-commit-config.yaml` (ruff), Dockerfile

## Related Files
- Project Context: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture Concepts: `.opencode/context/project/concepts/architecture-overview.md`
