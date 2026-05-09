<!-- Context: project/lookup/implemented-modules | Priority: medium | Version: 5.0 | Updated: 2026-05-07 -->

# Lookup: Implemented Modules

**Purpose**: Quick reference of infrastructure modules already implemented in the codebase
**Source**: `project-intelligence/harvested-sessions.md` — Implemented Improvements

---

## Configuration Modules (Split from config.py)

| Module | File | Purpose |
|--------|------|---------|
| Config Dataclasses | `src/config/config_schema_defs.py` | Pure data model (LLMConfig, WhatsAppConfig, Config) |
| Config Loader | `src/config/config_loader.py` | JSON I/O, dict→dataclass, env overrides |
| Config Validation | `src/config/config_validation.py` | Validation helpers, deprecated option tracking |
| Config Schema | `src/config/config_schema_defs.py` | Dataclass definitions + JSON Schema validation |
| Config Watcher | `src/config/config_watcher.py` | Polling-based hot-reload (mtime debounced) |
| Config Facade | `src/config/config.py` | Re-exports from split modules |
| Shutdown Context | `src/lifecycle.py` (ShutdownContext) | Dataclass replacing 12-param perform_shutdown |

## Stability Modules

| Module | File | Purpose |
|--------|------|---------|
| Circuit Breaker | `src/utils/circuit_breaker.py` | Circuit breaker pattern for fault tolerance |
| Rate Limiter | `src/rate_limiter.py` | Sliding window per-chat and per-skill rate limiting |
| Retry | `src/utils/retry.py` | Retry decorator with exponential backoff |
| Message Queue | `src/message_queue.py` | Persistent queue for crash recovery (swap-buffers flush, msgpack) |
| Queue Persistence | `src/message_queue_persistence.py` | JSONL/msgpack file I/O, crash recovery logic |
| Queue Buffer | `src/message_queue_buffer.py` | FlushManager — swap-buffers write management, background flush loop |

## Code Quality Modules

| Module | File | Purpose |
|--------|------|---------|
| Exceptions | `src/exceptions.py` | Custom exception types + user-friendly formatting |
| Protocols | `src/utils/protocols.py` | Protocol classes for Channel, Skill, Storage, ProjectStore |
| Type Guards | `src/utils/type_guards.py` | Runtime type checking utilities |
| Constants | `src/constants/` | Split by domain (app, cache, db, health, llm, memory, network, routing, scheduler, security, shutdown, skills, workspace) |
| Component Registry | `src/utils/registry.py` | Lightweight DI registry — `require()`/`get()` with fail-fast |
| Shared Validation | `src/utils/validation.py` | Consolidated `_validate_chat_id()` with regex safety checks |
| LRUDict | `src/utils/__init__.py` | Generic bounded LRU dictionary |
| DAG / Topo Sort | `src/utils/dag.py` | Topological sort for dependency-ordered startup |

## Logging & Monitoring Modules

| Module | File | Purpose |
|--------|------|---------|
| Logging Config | `src/logging/logging_config.py` | Structured logging with JSON format option |
| LLM Logging | `src/logging/llm_logging.py` | Per-request LLM logging to JSON files |
| HTTP Logging | `src/logging/http_logging.py` | HTTP request/response logging |
| Monitoring | `src/monitoring/performance.py` | Performance metrics, memory monitoring |
| OTel Tracing | `src/monitoring/tracing.py` | OpenTelemetry span helpers |
| Workspace Monitor | `src/monitoring/workspace_monitor.py` | Filesystem cleanup and monitoring |
| Health | `src/health/` | Health check HTTP endpoint (server, checks, models, prometheus, registry) |

| Health Check Registry | `src/health/registry.py` | Centralized discoverable registry with error isolation |

## UX Modules

| Module | File | Purpose |
|--------|------|---------|
| CLI Output | `src/ui/cli_output.py` | Colorful CLI output with Rich |
| Progress | `src/progress.py` | Progress indicators |
| Options TUI | `src/ui/options_tui.py` | Configuration editor TUI |

## Security Modules

| Module | File | Purpose |
|--------|------|---------|
| Path Validator | `src/security/path_validator.py` | TOCTOU-safe path validation with symlink detection |
| Prompt Injection | `src/security/prompt_injection.py` | Multi-language injection detection + content filtering |
| Signing | `src/security/signing.py` | HMAC-SHA256 for scheduled task prompt integrity |
| Audit | `src/security/audit.py` | HMAC-SHA256 chained audit log |
| URL Sanitizer | `src/security/url_sanitizer.py` | URL redaction for safe logging |
| Shell Security | `src/skills/builtin/shell.py` | Command blocklist (backticks, subshells, chaining) |

## Performance Modules

| Module | File | Purpose |
|--------|------|---------|
| Database | `src/db/db.py` | File-based JSONL persistence with file pool |
| File Pool | `src/db/file_pool.py` | Bounded file handle pool |
| DB Index | `src/db/db_index.py` | Message search index |
| DB Integrity | `src/db/db_integrity.py` | Database integrity checks |
| DB Validation | `src/db/db_validation.py` | Database validation utilities |
| DB Utils | `src/db/db_utils.py` | Shared DB helper functions |
| Migration | `src/db/migration.py` | Schema migration support |
| Compression | `src/db/compression.py` | Data compression |
| Message Store | `src/db/message_store.py` | JSONL message persistence |
| Generations | `src/db/generations.py` | LLM response generation tracking |
| SQLite Pool | `src/db/sqlite_pool.py` | Shared connection pool for SQLite databases |
| SQLite Utils | `src/db/sqlite_utils.py` | SqliteHelper with pool integration |
| Vector Memory | `src/vector_memory/` | sqlite-vec semantic search (batch.py, health.py, _utils.py) |
| Async Executor | `src/utils/async_executor.py` | Bounded concurrency executor |
| Background Service | `src/utils/background_service.py` | Background service pattern |
| Singleton | `src/utils/singleton.py` | Singleton pattern helper |

---

| Bot Orchestrator | `src/bot/_bot.py` | Thin orchestrator delegating to submodules (`BotDeps` constructor) |
| Context Building | `src/bot/context_building.py` | TurnContext assembly from routing + instruction + memory |
| Response Delivery | `src/bot/response_delivery.py` | Post-ReAct delivery: finalize, filter, dedup, persist, emit |

## Core Modules

| Module | File | Purpose |
|--------|------|---------|
| Orchestrator | `src/core/orchestrator.py` | Generic StepOrchestrator[C,S] for dependency-ordered execution |
| Event Bus | `src/core/event_bus.py` | Async typed pub/sub (10 events, singleton, error-isolated) |
| Dedup Service | `src/core/dedup.py` | Unified inbound (message-id) + outbound (xxhash) dedup with `check_and_record_outbound()` |
| Non-Critical Errors | `src/core/errors.py` | 25+ categorized fire-and-forget error logging |
| Context Assembler | `src/core/context_assembler.py` | Memory + instructions + project context assembly |
| Context Builder | `src/core/context_builder.py` | LLM context construction |
| Tool Executor | `src/core/tool_executor.py` | Skill execution with rate-limit, timeout, audit, sanitized tool names |
| Tool Formatter | `src/core/tool_formatter.py` | ToolLogEntry with lazy args parsing (`parsed_args` property), name length validation |
| Stream Accumulator | `src/core/stream_accumulator.py` | SSE streaming delta reconstruction |
| Topic Cache | `src/core/topic_cache.py` | Topic-based context caching |
| Serialization | `src/core/serialization.py` | Safe JSON serialization helpers |
| Instruction Loader | `src/core/instruction_loader.py` | Instruction file loading with path validation |
| Project Context | `src/core/project_context.py` | Project context for LLM |
| Message Pipeline | `src/core/message_pipeline.py` | Configurable middleware chain with reusable `MiddlewareChain` class |
| Startup | `src/core/startup.py` | Declarative `StartupOrchestrator` with `ComponentSpec` registry (495 lines) |

## LLM Package (src/llm/)

| Module | File | Purpose |
|--------|------|---------|
| LLM Client | `src/llm/_client.py` | OpenAI-compatible async client (chat + stream) |
| LLM Provider | `src/llm/_provider.py` | Public API: `chat()`, `update_config()`, `openai_client` property, circuit breaker |
| Error Classifier | `src/llm/_error_classifier.py` | LLM error → category/retryability mapping |
| Package Init | `src/llm/__init__.py` | Backward-compatible re-exports (`LLMClient`, `LLMProvider`, etc.) |

## Completed Optimization Tasks

277/328 PLAN.md items across 14 rounds complete (51 remaining).
See `lookup/plan-progress.md` for per-round breakdown.

## Related

- `concepts/architecture-overview.md` — How these modules fit together
- `lookup/workspace-structure.md` — Where modules live on disk
