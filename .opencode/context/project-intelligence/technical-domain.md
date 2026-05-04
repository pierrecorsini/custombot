<!-- Context: project-intelligence/technical | Priority: critical | Version: 2.8 | Updated: 2026-05-04 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-04 (harvest: all 219/219 PLAN.md items complete; 10 rounds, all done)

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
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks (82+ test+bench files); pytest-timeout 120s; dev extras in pyproject.toml |

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
@dataclass(frozen=True)      # Immutable containers (config, results)
class BotComponents:
    bot: Bot; db: Database; llm: LLMProvider

@dataclass(slots=True)       # Parameter bag (replaces 15-param Bot constructor)
class BotDeps:
    config: BotConfig; db: Database; llm: LLMProvider
    memory: MemoryProtocol; skills: SkillRegistry
    routing: RoutingEngine | None = None
    dedup: DeduplicationService | None = None

@dataclass(slots=True, frozen=True)  # ReAct loop invariants (replaces 18-param threading)
class ReactIterationContext:
    llm: LLMProvider; metrics: PerformanceMetrics; tool_executor: ToolExecutor
    chat_id: str; tools: list[...] | None; workspace_dir: Path
    stream_response: bool; max_tool_iterations: int
    max_retries: int; initial_delay: float; retryable_codes: frozenset[ErrorCode]
    stream_callback: StreamCallback | None = None
    channel: BaseChannel | None = None
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
# orjson-backed with stdlib fallback; all hot-path JSON should use these
data = json_loads(raw)                    # fast deserialization
text = json_dumps(obj, indent=2)          # fast serialization with formatting
result = safe_json_parse(raw, mode="strict")  # mode-based error handling (lenient/strict/line)
```

### Composable Message Pipeline
```python
from src.core.message_pipeline import build_pipeline_from_config, PipelineDependencies
# Middleware chain: operation_tracker → metrics → logging → preflight → typing → error → handle
# Dynamically extensible via config (built-in names + dotted import paths)
# Each middleware independently testable; _send_error_reply() centralizes error-channel responses
pipeline = build_pipeline_from_config(middleware_order=[...], deps=deps)
await pipeline.execute(MessageContext(msg=msg))
```

### Module Structure (every file follows this)
```python
"""module.py — One-line purpose."""
from __future__ import annotations
import logging; from typing import TYPE_CHECKING
if TYPE_CHECKING: from src.config import Config
log = logging.getLogger(__name__)
__all__ = ["PublicClass", "public_function"]
# Packages (e.g. src/llm/) use __init__.py to re-export public symbols for backward compat
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
- `TYPE_CHECKING` guard for type-only imports; `__all__` exports in all public modules
- Frozen dataclasses for immutable data; `slots=True` for mutable state
- Structured DI: `BotDeps`, `ShutdownContext`, `ReactIterationContext`, `BuilderContext` replace multi-param constructors
- Protocol classes for DI boundaries (14+ in `src/utils/protocols.py`): `MemoryProtocol`, `Stoppable`, `Closeable`, `BackgroundService`, `LockProvider`
- Channel abstraction: `BaseChannel` ABC with `CommandLineChannel` and `NeonizeWhatsAppChannel`
- Step orchestrator for multi-phase startup/build (declarative `ComponentSpec`)
- Config hot-reload: atomic reference swap (`Application._swap_config()`)
- Connection pooling for SQLite (sqlite_pool.py, file_pool.py); health server layered middleware with `HEALTH_ALLOWED_PATHS`
- Offload blocking I/O via `asyncio.to_thread()`; snapshot mutable state before use
- TOCTOU-safe file operations with `os.O_EXCL`; atomic file writes (temp→rename) in scheduler
- Swap-buffers pattern for `MessageQueue` flush; disk-full retry buffering
- Pre-compute `MatchingContext` in `Bot._build_turn_context()` before `match_with_rule()`
- Reverse index for `TokenUsage._leaderboard` → O(k·log n) purge
- Tool name sanitization: strips control chars + ANSI escapes before log/audit; name length validated (200 char max in ToolLogEntry); lazy args parsing (raw JSON stored, `parsed_args` property deserializes on demand)
- Unified `DeduplicationService`: check_and_record_outbound (single hash), fail-open on DB errors, in-memory LRU (10K, 5-min TTL)
- `BoundedOrderedDict(ttl=...)` for outbound dedup cache; `finish_reason="length"` handled explicitly
- `message_dropped` event for unmatched routing and oversized messages
- `generation_conflict` event for write conflicts in `_deliver_response()`
- `EVENT_STARTUP_COMPLETED` emitted after Application startup completes
- Error categorization in `Application.run()` main loop (LLM_TRANSIENT, CHANNEL_DISCONNECT, etc.)
- RoutingEngine: non-blocking async retry, symlink rejection, zero-rule graceful degradation
- VectorMemory decoupled via public `LLMProvider.openai_client` property
- `AppComponents.to_shutdown_context()` factory; parallel shutdown via `asyncio.gather()`
- Scheduler: cached `_last_run_dt`, orjson via `json_utils`, unified `_target_utc_time()`
- Timeout path: `_message_queue.complete()` in `except asyncio.TimeoutError`
- msgpack persistence for MessageQueue (JSON fallback for crash recovery)
- `QueuedMessage` with `__slots__` for reduced per-instance memory
- Message pipeline: composable middleware chain, dynamically extensible via config
- `_send_error_reply()` centralizes send → dedup → event for error-channel responses
- Double quotes (ruff format), line length 100; Google-style docstrings; dev deps in pyproject.toml `[dev]` extras
- `src/project/` — SQLite-backed knowledge tracking: ProjectStore (CRUD), ProjectGraph (BFS/traversal), ProjectRecall (hybrid vector+graph), dates utils
- `src/core/topic_cache.py` — Per-chat topic summary cache (mtime-cached, file-based) with META parsing for topic-change signals
- `src/core/context_builder.py` — LLM context assembly: system prompt, instructions, memory, topic summary, reduced history
- `src/core/project_context.py` — Lazy ProjectGraph/ProjectRecall initialization for LLM context
- `src/rate_limiter.py` — Sliding window rate limiting (per-chat 30/min, per-skill 10/min, env-configurable)
- `src/workspace_integrity.py` — Startup workspace verification (stale temps, corrupt JSONL, locked SQLite)
- `src/dependency_check.py` — Auto-update dependencies at startup (esp. neonize WhatsApp lib)
- `src/progress.py` — Rich-based SpinnerStatus, ProgressBar, ProgressTracker for long-running ops
- `src/ui/` — Presentation layer: cli_output.py (formatting), options_tui.py (interactive options)
- `src/utils/frontmatter.py` — YAML/frontmatter parsing for instruction files
- `src/utils/phone.py` — Phone number normalization utilities
- Ruff: pinned 0.15.12 in pyproject.toml (local+CI parity), PERF ruleset added; CI: check_plan_syntax.py validates PLAN.md checkbox format; pip-audit SARIF upload to GitHub Security tab

---

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

**Entry Point**: `main.py` — Click CLI: `start`, `options`, `diagnose` (+ `--health-host`)
**App**: `src/app.py` — `Application` + `AppPhase` state machine + error categorization + config swap + bounded semaphore
**Builder**: `src/builder.py` — `build_bot()` → `BotComponents`; VectorMemory decoupled from LLMClient
**Bot**: `src/bot/` — `_bot.py` (Bot+BotDeps, outbound dedup, update_config), `crash_recovery.py`, `preflight.py`, `react_loop.py` (ReactIterationContext)
**Channels**: `src/channels/` — `base.py` (BaseChannel ABC), `cli.py`, `neonize_backend.py`, `stealth.py`, `validation.py`, `whatsapp.py`
**Config**: `src/config/` — Schema defs, loader, validation, `config_watcher.py` (atomic swap, 6 modules)
**LLM**: `src/llm.py`, `src/llm_provider.py`, `src/llm_error_classifier.py` — httpx async client + circuit breaker + public `openai_client` property
**Memory**: `src/memory.py` — Per-chat MEMORY.md + chat dir caching + async recovery + TOCTOU-safe seeding
**Routing**: `src/routing.py` — YAML frontmatter rules, MatchingContext, symlink rejection, zero-rule degradation, non-blocking load
**Message Queue**: `src/message_queue.py`, `src/message_queue_persistence.py` — Swap-buffers flush, streaming JSONL, disk-full handling
**Scheduler**: `src/scheduler.py` — Cached `_last_run_dt`, orjson via json_utils, unified `_target_utc_time()`, outbound dedup
**Serialization**: `src/utils/json_utils.py` — Canonical `json_dumps`/`json_loads` (orjson + stdlib fallback), `safe_json_parse`, msgpack
**Pipeline**: `src/core/message_pipeline.py` — Composable middleware chain, dynamically extensible, `_send_error_reply()` helper
**Skills**: `src/skills/` — `BaseSkill` (Python) + prompt-based skills (Markdown)
**Security**: `src/security/` — Path validator, prompt injection, URL sanitizer, audit, signing, tool name sanitization (6 modules)
**Health**: `src/health/` — server.py, middleware.py (path/method/size/HMAC/rate-limit/security headers), prometheus.py, checks.py
**DB**: `src/db/` — sqlite_pool.py, file_pool.py, migration, message_store, generations, compression (14 modules)
**Utils**: `src/utils/` — 14+ Protocol classes, locking, circuit_breaker, dag, singleton, retry, timing, type_guards (19 modules)
**Constants**: `src/constants/` — Domain-organized constants (15 sub-modules: cache, db, health, llm, memory, messaging, network, routing, scheduler, security, shutdown, skills, workspace)
**Core**: `src/core/` — orchestrator, event_bus (10 events), dedup, errors, context_assembler, context_builder, topic_cache, project_context, tool_executor, tool_formatter (ToolLogEntry lazy args), message_pipeline, instruction_loader, stream_accumulator, serialization, startup (StartupOrchestrator + ComponentSpec)
**Project**: `src/project/` — ProjectStore (SQLite CRUD), ProjectGraph (BFS/traversal), ProjectRecall (hybrid vector+graph), dates
**Monitoring**: `src/monitoring/` — Performance, memory, metrics_types, tracing, workspace_monitor
**UI**: `src/ui/` — cli_output.py, options_tui.py (interactive options dialog)
**Utils**: `src/utils/` — 14+ Protocol classes, locking, circuit_breaker, dag, singleton, retry, timing, type_guards, frontmatter, phone (19 modules)
**Constants**: `src/constants/` — Domain-organized constants (15 sub-modules: cache, db, health, llm, memory, messaging, network, routing, scheduler, security, shutdown, skills, workspace)
**Logging**: `src/logging/` — http_logging, llm_logging (redaction), logging_config (3 modules)
**Rate Limiting**: `src/rate_limiter.py` — Sliding window (per-chat + per-skill, env-configurable)
**Workspace**: `src/workspace_integrity.py` — Startup verification; `src/dependency_check.py` — Dependency auto-update
**Progress**: `src/progress.py` — Rich-based spinners/progress bars; `src/templates/instructions/` — Agent instruction templates
**Tests**: `tests/` — 82+ test+bench files (unit=59, integration=13, e2e=10); bench_regression.py; pytest-timeout 120s; src/bot/ under mypy --strict
**Build**: `pyproject.toml`, `Makefile`, `requirements.txt`, `.pre-commit-config.yaml` (ruff+TCH strict), Dockerfile, `.gitattributes`
**CI Scripts**: `scripts/` — check_plan_syntax.py, check_coverage_floor.py, check_config_example_sync.py; pip-audit SARIF upload in CI

## Related Files
- Navigation: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture: `.opencode/context/project/concepts/architecture-overview.md`
