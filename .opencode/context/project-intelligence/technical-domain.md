<!-- Context: project-intelligence/technical | Priority: critical | Version: 3.0 | Updated: 2026-05-04 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-04 (PLAN.md v3: 8/34 items complete — scheduler decomposition, DeduplicationService buffer cap, monotonic timing; previous: emit_error_event helper, Bot._load_instruction removal, pytest-timeout)

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
| Serialization | orjson + msgpack (via json_utils) | latest | Canonical json_dumps/json_loads + orjson accel + stdlib fallback; msgpack for binary |
| Hashing | xxhash | ~=3.5 | Deduplication hash computation; outbound dedup in scheduler |
| Logging | stdlib + Rich + OTel | 1.30 | Structured logs, correlation IDs, OpenTelemetry spans; http_logging, llm_logging, logging_config |
| Config | JSON + dataclasses + watchdog | >=4.0 | Hot-reload via atomic config swap pattern |
| Process | psutil | ~=6.0 | System resource monitoring |
| Linting | Ruff | 0.15.12 | Combined linter + formatter (E/W/F/I/UP/B/SIM/PL/TCH/PERF rulesets); PL+PERF non-blocking; pinned in pyproject.toml |
| Typing | mypy | >=1.20 | Gradual strict mode; `src/bot/` under `--strict` |
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks (82+ test+bench files); pytest-timeout 120s; dev extras in pyproject.toml `[dev]` |

---

## Code Patterns

### Async LLM Client
```python
from src.llm import LLMProvider  # src/llm/ package with backward-compat re-exports
provider = LLMProvider(config.llm)
response: str = await provider.chat(messages=[...], tools=[...])
# Supports streaming, tool calls, warmup probes, health checks
# Public config update: provider.update_config(new_llm_cfg)
```

### Immutable Turn-Preparation
```python
@dataclass(frozen=True)  # _PreparedTurn — turn setup extracted from Bot._process()
class _PreparedTurn:
    messages: list[dict]; tools: list | None; context: str
    workspace_dir: Path; matching_ctx: MatchingContext | None
# Bot._process() → _prepare_turn() → _react_loop() → _deliver_response()
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
@dataclass(frozen=True)      # Immutable containers
class BotComponents: bot: Bot; db: Database; llm: LLMProvider

@dataclass(slots=True)       # Parameter bag (replaces 15-param constructor)
class BotDeps:
    config: BotConfig; db: Database; llm: LLMProvider
    memory: MemoryProtocol; skills: SkillRegistry
    routing: RoutingEngine | None = None; dedup: DeduplicationService | None = None
# Also: ReactIterationContext(slots=True, frozen=True) for ReAct loop invariants
```

### Channel Abstraction Layer
```python
from src.channels import BaseChannel, IncomingMessage, CommandLineChannel
# BaseChannel ABC: connect(), disconnect(), send_text(), send_media()
# Bot depends on BaseChannel, never on a specific transport
```

### Canonical JSON Serialization (json_utils)
```python
from src.utils.json_utils import json_dumps, json_loads, safe_json_parse
# orjson-backed with stdlib fallback; modes: lenient/strict/line
```

### Scheduler Package (Decomposed from Monolith)
```python
from src.scheduler import TaskScheduler  # Re-exports from package
# Internally: scheduler/engine.py (tick loop, heap scheduling)
#             scheduler/cron.py (UTC conversion, weekday matching)
#             scheduler/persistence.py (JSONL read/write, HMAC integrity, atomic save)
# Adaptive sleep: computes time-to-next-due, avoids fixed TICK_SECONDS polling
```

### Composable Message Pipeline
```python
from src.core.message_pipeline import build_pipeline_from_config
# Middleware chain: operation_tracker → metrics → logging → preflight → typing → error → handle
# Dynamically extensible via config; _send_error_reply() centralizes error-channel responses
```

### Error Event Emission Helper
```python
from src.core.event_bus import emit_error_event
# Consolidates duplicate try/except error-emission boilerplate across Application, Bot, etc.
# Auto-populates error_type + error_message; catches its own failures as non-critical
await emit_error_event(exc, "Application.run", extra_data={"category": category})
```

### Module Structure (every file follows this)
```python
"""module.py — One-line purpose."""
from __future__ import annotations
import logging; from typing import TYPE_CHECKING
log = logging.getLogger(__name__); __all__ = ["PublicClass", "public_function"]
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

- `from __future__ import annotations` on line 1; `log = logging.getLogger(__name__)` in every module; `TYPE_CHECKING` guard + `__all__` exports
- Frozen dataclasses for immutable data; `slots=True` for mutable; structured DI (`BotDeps`, `ShutdownContext`, `ReactIterationContext`, `BuilderContext`)
- Protocol classes for DI boundaries (14+ in `src/utils/protocols.py`); Channel abstraction via `BaseChannel` ABC; Step orchestrator (declarative `ComponentSpec`)
- Config hot-reload: atomic reference swap (`Application._swap_config()`); Connection pooling (sqlite_pool.py, file_pool.py); Offload blocking I/O via `asyncio.to_thread()`
- TOCTOU-safe file ops (`os.O_EXCL`); atomic writes (temp→rename); swap-buffers for `MessageQueue` flush
- Scheduler: decomposed into `src/scheduler/` package (engine.py, cron.py, persistence.py); adaptive sleep with heap-based time-to-next-due
- `DeduplicationService`: buffered outbound with configurable cap, batch `flush_outbound_batch()`, in-memory LRU (10K, 5-min TTL); Tool name sanitization (ANSI stripped, 200 char max)
- `emit_error_event()` helper; Direct `InstructionLoader.load()` calls (removed `Bot._load_instruction()`); `_send_error_reply()` centralizes error-channel responses
- Explicit discard of DB return values (`_ids =`); Events: `message_dropped`, `generation_conflict`, `EVENT_STARTUP_COMPLETED`; Error categorization in `Application.run()`
- RoutingEngine: non-blocking async retry, symlink rejection, zero-rule degradation; VectorMemory via `LLMProvider.openai_client`; Parallel shutdown via `asyncio.gather()`
- `time.monotonic()` in timing utilities; context-var helpers `set_timer_start()` + `elapsed()`; msgpack for MessageQueue; `QueuedMessage` with `__slots__`
- Double quotes (ruff format), line length 100; Google-style docstrings; dev deps in pyproject.toml `[dev]` extras
- Ruff 0.15.12 pinned (PERF ruleset); CI: check_plan_syntax.py validates PLAN.md; pip-audit SARIF to GitHub Security; Messaging constants in `src/constants/messaging.py`

## Security Requirements

- Path validation: all file access sandboxed to workspace (`is_path_in_workspace`)
- Instruction loader: `_validate_path()` rejects directory components and resolved-path escapes
- URL sanitization: strip credentials from logged URLs (`sanitize_url_for_logging`)
- Prompt injection detection: classify and reject adversarial inputs
- Secret redaction: `Config.__repr__()` uses `_redact_secrets()` to mask API keys
- Config file permission check: warn if config.json readable by group/others (Unix)
- Input validation: `IncomingMessage` fields + correlation_id sanitized; sender_name truncated + stripped
- Defense-in-depth: 6-module security subsystem (`src/security/`)
- Supply-chain: Dockerfile pins base image by digest, neonize/sqlite-vec exact-pinned
- Health server: binds to configurable host (default 127.0.0.1), rate-limited, security headers (CSP, HSTS, X-Content-Type-Options)
- TOCTOU-safe workspace seeding with `os.O_EXCL`; symlink rejection in routing
- Stdin read timeout in `_confirm_send()` prevents indefinite blocking on pipes

---

## 📂 Codebase References

**Entry**: `main.py` (Click CLI) | **App**: `src/app.py` (Application + AppPhase + config swap) | **Builder**: `src/builder.py`
**Bot**: `src/bot/` — _bot.py, crash_recovery.py, preflight.py, react_loop.py (ReactIterationContext)
**Channels**: `src/channels/` — base.py (ABC), cli.py, neonize_backend.py, stealth.py, validation.py, whatsapp.py
**Config**: `src/config/` (6 modules, atomic swap) | **LLM**: `src/llm.py` + llm_provider.py + llm_error_classifier.py
**Memory**: `src/memory.py` | **Routing**: `src/routing.py` | **Scheduler**: `src/scheduler/` (engine.py, cron.py, persistence.py)
**Queue**: `src/message_queue.py` + persistence | **Serialization**: `src/utils/json_utils.py` (orjson + fallback)
**Pipeline**: `src/core/message_pipeline.py` | **Skills**: `src/skills/` | **Security**: `src/security/` (6 modules)
**Health**: `src/health/` (server + middleware + prometheus) | **DB**: `src/db/` (14 modules, connection pooling)
**Utils**: `src/utils/` (19 modules) | **Constants**: `src/constants/` (15 sub-modules) | **Logging**: `src/logging/` (3 modules)
**Core**: `src/core/` — event_bus (10 events + emit_error_event), dedup, errors, context_assembler/builder, tool_executor/formatter, instruction_loader, startup (StartupOrchestrator + ComponentSpec)
**Project**: `src/project/` (CRUD + graph + recall) | **Monitoring**: `src/monitoring/` | **UI**: `src/ui/`
**Rate Limiting**: `src/rate_limiter.py` | **Workspace**: `src/workspace_integrity.py` + dependency_check.py
**Tests**: `tests/` (82+ files) | **Build**: pyproject.toml, Makefile, Dockerfile | **CI**: `scripts/` + pip-audit SARIF

## Related Files
- Navigation: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture: `.opencode/context/project/concepts/architecture-overview.md`
