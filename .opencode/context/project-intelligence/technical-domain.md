<!-- Context: project-intelligence/technical | Priority: critical | Version: 3.1 | Updated: 2026-05-05 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-05 (PLAN.md: 7/38 complete — all 6 Architecture items + 1 Performance item; recent: frontmatter LRU cache, send_and_track extraction, ComponentRegistry DI)

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
| Logging | stdlib + Rich + OTel | 1.30 | Structured logs, correlation IDs, OpenTelemetry spans |
| Config | JSON + dataclasses + watchdog | >=4.0 | Hot-reload via atomic config swap pattern |
| Process | psutil | ~=6.0 | System resource monitoring |
| Linting | Ruff | 0.15.12 | Combined linter + formatter (E/W/F/I/UP/B/SIM/PL/TCH/PERF); PL+PERF non-blocking |
| Typing | mypy | >=1.20 | Gradual strict mode; `src/bot/` under `--strict` |
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks (82+ files); pytest-timeout 120s |

---

## Code Patterns

### Async LLM Client
```python
from src.llm import LLMProvider
provider = LLMProvider(config.llm)
response: str = await provider.chat(messages=[...], tools=[...])
# Streaming, tool calls, warmup probes, health checks; update_config() for hot-reload
```

### Immutable Turn-Preparation
```python
@dataclass(frozen=True)
class _PreparedTurn:
    messages: list[dict]; tools: list | None; context: str
    workspace_dir: Path; matching_ctx: MatchingContext | None
# Bot._process() → _prepare_turn() → _react_loop() → _deliver_response()
```

### Config Hot-Reload (Atomic Swap)
```python
# Bot, LLMProvider, ContextAssembler expose public update_config()
# ConfigChangeApplier swaps entire Config reference atomically
bot.update_config(new_bot_cfg); provider.update_config(new_llm_cfg)
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
# ComponentRegistry replaces BuilderContext/StartupContext mutable bags; protocol-based DI
```

### Channel Abstraction Layer
```python
from src.channels import BaseChannel, IncomingMessage, CommandLineChannel
# BaseChannel ABC: connect(), disconnect(), send_text(), send_media()
```

### Canonical JSON Serialization
```python
from src.utils.json_utils import json_dumps, json_loads, safe_json_parse
# orjson-backed with stdlib fallback; modes: lenient/strict/line
```

### Scheduler Package (Decomposed)
```python
from src.scheduler import TaskScheduler
# engine.py (tick loop, heap scheduling), cron.py (UTC conversion, weekday matching)
# persistence.py (JSONL, HMAC integrity, atomic save); adaptive sleep via time-to-next-due
```

### Bot Sub-Module Decomposition
```python
# _bot.py — Bot orchestrator (thin coordinator)
# context_building.py — _build_turn_context() assembly
# response_delivery.py — _send_to_chat() send→dedup→event pattern
# react_loop.py — ReAct iteration loop (ReactIterationContext)
# crash_recovery.py — Message recovery; preflight.py — Pre-processing checks
```

### Composable Message Pipeline
```python
from src.core.message_pipeline import build_pipeline_from_config
# Middleware: operation_tracker → metrics → logging → preflight → typing → error → handle
```

### Error Event Emission
```python
from src.core.event_bus import emit_error_event
await emit_error_event(exc, "Application.run", extra_data={"category": category})
# Consolidates error-emission boilerplate; auto-populates error_type + error_message
```

### Module Structure
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

- `from __future__ import annotations` on line 1; `log = logging.getLogger(__name__)`; `TYPE_CHECKING` + `__all__` exports
- Frozen dataclasses for immutable data; `slots=True` for mutable; structured DI (`BotDeps`, `ShutdownContext`, `ReactIterationContext`)
- ComponentRegistry: protocol-based DI with `validate_populated()` → guaranteed non-None typed context
- Protocol classes for DI boundaries (14+ in `src/utils/protocols.py`); Channel abstraction via `BaseChannel` ABC
- Config hot-reload: atomic reference swap; Connection pooling (sqlite_pool, file_pool); `asyncio.to_thread()` for blocking I/O
- TOCTOU-safe file ops (`os.O_EXCL`); atomic writes (temp→rename); swap-buffers for `MessageQueue` flush
- Scheduler: decomposed `src/scheduler/` package; adaptive sleep with heap-based time-to-next-due
- DeduplicationService: buffered outbound with configurable cap, batch flush, in-memory LRU (10K, 5-min TTL)
- `emit_error_event()` helper; Direct `InstructionLoader.load()` calls; `_send_error_reply()` centralizes error responses
- Bot decomposition: _bot.py → context_building.py + response_delivery.py + per-chat lock
- MessageQueue: message_queue.py (public API) + _persistence.py + _buffer.py (buffer management, background flush)
- Unified chat_id validation in `src/utils/validation.py`; Events: message_dropped, generation_conflict, EVENT_STARTUP_COMPLETED
- RoutingEngine: non-blocking async retry, symlink rejection, zero-rule degradation; VectorMemory via LLMProvider.openai_client
- `time.monotonic()` in timing utilities; msgpack for MessageQueue; `QueuedMessage` with `__slots__`
- Double quotes (ruff format), line length 100; Google-style docstrings; dev deps in pyproject.toml `[dev]`
- Ruff 0.15.12 pinned; CI: check_plan_syntax.py; pip-audit SARIF to GitHub Security

## Security Requirements

- Path validation: sandboxed to workspace (`is_path_in_workspace`); symlink rejection in routing
- Instruction loader: `_validate_path()` rejects directory components and resolved-path escapes
- URL sanitization: strip credentials from logged URLs; Secret redaction in `Config.__repr__()`
- Prompt injection detection: classify and reject adversarial inputs; 6-module security subsystem (`src/security/`)
- Input validation: `IncomingMessage` fields + correlation_id sanitized; sender_name truncated
- Config file permission check; TOCTOU-safe workspace seeding with `os.O_EXCL`
- Health server: binds to configurable host (default 127.0.0.1), rate-limited, security headers
- Supply-chain: Dockerfile pins base image by digest, neonize/sqlite-vec exact-pinned
- Stdin read timeout in `_confirm_send()` prevents indefinite blocking

---

## Codebase References

**Entry**: `main.py` (Click CLI) | **App**: `src/app.py` | **Builder**: `src/builder.py`
**Bot**: `src/bot/` — _bot.py, context_building.py, response_delivery.py, react_loop.py, crash_recovery.py, preflight.py
**Channels**: `src/channels/` — base.py (ABC), cli.py, neonize_backend.py, stealth.py, whatsapp.py
**Config**: `src/config/` (5 modules, atomic swap) | **LLM**: `src/llm/` (3 modules)
**Core**: `src/core/` (15 modules) — event_bus, dedup, errors, context_assembler/builder, tool_executor/formatter, instruction_loader, startup, orchestrator, project_context, serialization, stream_accumulator, topic_cache, message_pipeline
**DB**: `src/db/` (12 modules) — connection pooling, migrations, integrity | **Utils**: `src/utils/` (19 modules)
**Scheduler**: `src/scheduler/` (engine, cron, persistence) | **Queue**: `src/message_queue.py` + _persistence.py + _buffer.py
**Memory**: `src/memory.py` | **Routing**: `src/routing.py` | **Vector**: `src/vector_memory/` (batch, health)
**Security**: `src/security/` (6 modules) | **Health**: `src/health/` | **Monitoring**: `src/monitoring/`
**Logging**: `src/logging/` (3 modules) | **Project**: `src/project/` (store, graph, recall, dates)
**Skills**: `src/skills/` | **UI**: `src/ui/` | **Constants**: `src/constants/` (15 sub-modules)
**Tests**: `tests/` (82+ files) | **Build**: pyproject.toml, Makefile, Dockerfile

## Related Files
- Navigation: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture: `.opencode/context/project/concepts/architecture-overview.md`
