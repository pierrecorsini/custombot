<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.8 | Updated: 2026-05-04 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-04

## Quick Reference
**Update Triggers**: Tech stack changes | New patterns | Architecture decisions
**Audience**: Developers, AI agents

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | >=3.11 | Async/await, dataclasses, slots, type hints |
| Runtime | asyncio | stdlib | Event-driven message processing |
| LLM Client | openai | ~=2.29 | Supports OpenAI, Anthropic proxy, Ollama, OpenRouter, Groq |
| Channel | neonize | 0.3.17.post0 | Native WhatsApp Web client (exact-pinned, QR pairing, session persistence) |
| HTTP Client | httpx | ~=0.28 | Async HTTP for LLM connections, embedding, logging |
| HTTP Server | aiohttp | ~=3.13 | Health server (Prometheus + JSON endpoints) |
| CLI | Click | ~=8.3 | Command groups, options, help text |
| Display | Rich | ~=14.3 | Terminal formatting, spinners, progress bars |
| Search | duckduckgo-search | ~=8.1 | Web search integration for skills |
| Database | SQLite | stdlib | 3 databases: main (.data/), vector_memory, projects; connection pooling via sqlite_pool.py |
| Vector Search | sqlite-vec | 0.1.9 | Semantic memory with cosine similarity; detects embedding model changes across restarts |
| Serialization | orjson + msgpack | latest | Fast JSON + binary encoding |
| Hashing | xxhash | ~=3.5 | Deduplication hash computation; outbound dedup in scheduler |
| Logging | stdlib + Rich + OTel | 1.30 | Structured logs, correlation IDs, OpenTelemetry spans; http_logging, llm_logging, logging_config |
| Config | JSON + dataclasses + watchdog | >=4.0 | Hot-reload via atomic config swap pattern |
| Process | psutil | ~=6.0 | System resource monitoring |
| Linting | Ruff | >=0.15 | Combined linter + formatter (E/W/F/I/UP/B/SIM/TCH/PL rulesets); PL non-blocking |
| Typing | mypy | >=1.20 | Gradual strict mode (`disallow_untyped_defs = false`) |
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks (75 test+bench files); dev extras in pyproject.toml |

---

## Code Patterns

### Async LLM Client
```python
from src.llm_provider import LLMProvider
provider = LLMProvider(config.llm)
response: str = await provider.chat(messages=[...], tools=[...])
# Supports streaming, tool calls, warmup probes, health checks
# Public config update: provider.update_config(new_llm_cfg)
```

### Config Hot-Reload (Atomic Swap Pattern)
```python
# Bot, LLMProvider, ContextAssembler expose public update_config() methods
# ConfigChangeApplier swaps entire Config reference atomically (not field-by-field)
bot.update_config(new_bot_cfg)
provider.update_config(new_llm_cfg)
context_assembler.update_config(new_cfg)
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
    routing: RoutingEngine | None = None
    dedup: DeduplicationService | None = None

@dataclass(slots=True, frozen=True)  # ReAct loop invariants (replaces 13-param threading)
class ReactIterationContext:
    llm: LLMProvider; metrics: PerformanceMetrics; tool_executor: ToolExecutor
    chat_id: str; tools: list[...] | None; workspace_dir: Path
```

### Channel Abstraction Layer
```python
from src.channels import BaseChannel, IncomingMessage, CommandLineChannel
# BaseChannel ABC: connect(), disconnect(), send_text(), send_media()
# Bot depends on BaseChannel, never on a specific transport
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
| Tests | test_ prefix | `test_routing.py`, `test_config.py` |

---

## Code Standards

- `from __future__ import annotations` on line 1 of every file (after docstring)
- `log = logging.getLogger(__name__)` — module-level logger in every module
- `TYPE_CHECKING` guard for type-only imports (avoid circular deps)
- `__all__` exports in all public modules (100% coverage)
- Frozen dataclasses for immutable data; `slots=True` for mutable state
- Structured dependency injection: `BotDeps`, `ShutdownContext`, `ReactIterationContext`, `BuilderContext` replace multi-param constructors
- Protocol classes for DI boundaries: `MemoryProtocol`, `Stoppable`, `Closeable`, `BackgroundService`, `LockProvider` (14+ in `src/utils/protocols.py`)
- Channel abstraction: `BaseChannel` ABC with `CommandLineChannel` and `NeonizeWhatsAppChannel` implementations
- Step orchestrator pattern for multi-phase startup/build (declarative `ComponentSpec`)
- Config hot-reload uses atomic reference swap (`Application._swap_config()`)
- Connection pooling for SQLite (sqlite_pool.py, file_pool.py)
- Health server: layered middleware (path → method → size → rate limit → HMAC) with `HEALTH_ALLOWED_PATHS` frozenset
- Offload blocking I/O via `asyncio.to_thread()`; snapshot mutable state before use
- TOCTOU-safe file operations with `os.O_EXCL` atomic open
- Double quotes for strings (ruff format), line length 100
- Docstrings: triple-double-quoted, Google-style Args/Returns
- Dev dependencies in pyproject.toml `[dev]` extras

---

## Security Requirements

- Path validation: all file access sandboxed to workspace (`is_path_in_workspace`)
- Instruction loader: `_validate_path()` rejects directory components and resolved-path escapes
- URL sanitization: strip credentials from logged URLs (`sanitize_url_for_logging`)
- Prompt injection detection: classify and reject adversarial inputs
- Secret redaction: `Config.__repr__()` uses `_redact_secrets()` to mask API keys
- Config file permission check: warn if config.json readable by group/others (Unix)
- Input validation: `IncomingMessage` fields validated before use
- Defense-in-depth: 6-module security subsystem (`src/security/`)
- Supply-chain: Dockerfile pins base image by digest, neonize/sqlite-vec exact-pinned
- Health server: binds to configurable host (default 127.0.0.1), rate-limited
- TOCTOU-safe workspace seeding with `os.O_EXCL`; symlink rejection in routing

---

## 📂 Codebase References

**Entry Point**: `main.py` — Click CLI: `start`, `options`, `diagnose` (+ `--health-host`)
**App**: `src/app.py` — `Application` + `AppPhase` state machine + error categorization + config swap
**Builder**: `src/builder.py` — `build_bot()` → `BotComponents`
**Bot**: `src/bot/` — `_bot.py` (Bot+BotDeps, outbound dedup), `crash_recovery.py`, `preflight.py`, `react_loop.py` (ReactIterationContext)
**Channels**: `src/channels/` — `base.py` (BaseChannel ABC), `cli.py`, `neonize_backend.py`, `stealth.py`, `validation.py`, `whatsapp.py`
**Config**: `src/config/` — Schema defs, loader, validation, `config_watcher.py` (atomic swap, 5 modules)
**LLM**: `src/llm.py`, `src/llm_provider.py`, `src/llm_error_classifier.py` — httpx-based async client + circuit breaker
**Memory**: `src/memory.py` — Per-chat MEMORY.md + chat dir caching + async recovery
**Routing**: `src/routing.py` — YAML frontmatter rules, MatchingContext pre-computation, zero-rule graceful degradation
**Message Queue**: `src/message_queue.py`, `src/message_queue_persistence.py` — Swap-buffers flush, streaming JSONL
**Scheduler**: `src/scheduler.py` — Cached time-to-next-due, outbound dedup
**Skills**: `src/skills/` — `BaseSkill` (Python) + prompt-based skills (Markdown)
**Security**: `src/security/` — Path validator, prompt injection, URL sanitizer, audit, signing (6 modules)
**Health**: `src/health/` — server.py, middleware.py (path/method/size/HMAC/rate-limit), prometheus.py, checks.py (6 modules)
**DB**: `src/db/` — sqlite_pool.py, file_pool.py, migration, message_store, generations (12 modules)
**Utils**: `src/utils/` — 14+ Protocol classes, locking, circuit_breaker, dag, singleton, retry, timing, type_guards (18 modules)
**Constants**: `src/constants/` — Domain-organized constants (13 sub-modules: cache, db, health, llm, memory, messaging, network, routing, scheduler, security, shutdown, skills, workspace)
**Project**: `src/project/` — store, graph, recall, dates (project knowledge subsystem)
**Monitoring**: `src/monitoring/` — Performance, memory, tracing, workspace monitor
**Logging**: `src/logging/` — http_logging, llm_logging (redaction), logging_config (3 modules)
**UI**: `src/ui/` — cli_output.py, options_tui.py (optional questionary dependency)
**Templates**: `src/templates/` — Instruction templates
**Tests**: `tests/` — 75 test+bench files; dev extras in pyproject.toml
**Build**: `pyproject.toml`, `Makefile`, `requirements.txt`, `.pre-commit-config.yaml` (ruff), Dockerfile

## Related Files
- Navigation: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture: `.opencode/context/project/concepts/architecture-overview.md`
