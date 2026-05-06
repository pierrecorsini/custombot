<!-- Context: project-intelligence/lookup/project-structure | Priority: high | Version: 4.9 | Updated: 2026-05-06 -->

# Project Structure

> Directory layout and key locations in the custombot codebase.

## Directory Tree

```
custombot/
├── main.py                  # CLI entry point (start, cli commands)
├── config.example.json      # Example configuration template
├── pyproject.toml           # Python project config, deps, ruff, mypy, pytest
├── Makefile                 # Dependency management targets (pip-compile)
├── Dockerfile               # Multi-stage Docker build
├── PLAN.md                  # (archived to .tmp/archive/ after Round 11)
├── requirements.txt         # Auto-generated from pyproject.toml
├── requirements-lock.txt    # Hash-locked dependencies
│
├── src/                     # Core application logic
│   ├── __init__.py
│   ├── __version__.py
│   ├── app.py              # Main Application class
│   ├── builder.py          # Bot builder (public API: build_bot())
│   ├── lifecycle.py        # Lifecycle management (ShutdownContext)
│   ├── llm.py              # LLM client wrapper
│   ├── llm_provider.py     # LLM provider abstraction
│   ├── llm_error_classifier.py  # Error classification + circuit breaker
│   ├── memory.py           # Conversation memory management
│   ├── message_queue.py    # Persistent queue for crash recovery
│   ├── message_queue_buffer.py    # Queue buffer management
│   ├── message_queue_persistence.py  # WAL-protected msgpack+base64 JSONL persistence
│   ├── progress.py         # Progress tracking
│   ├── rate_limiter.py     # Sliding window per-chat and per-skill rate limiting
│   ├── routing.py          # Message routing engine
│   ├── scheduler/          # Async task scheduler (decomposed from monolithic scheduler.py)
│   │   ├── __init__.py     # Re-exports: TaskScheduler, cron helpers, persistence constants
│   │   ├── engine.py       # Tick loop, heap scheduling, adaptive sleep
│   │   ├── cron.py         # UTC offset, local-to-UTC conversion, weekday matching
│   │   └── persistence.py  # JSONL I/O, HMAC integrity, atomic save
│   ├── shutdown.py         # Graceful shutdown
│   ├── dependency_check.py # Dependency verification
│   ├── diagnose.py         # Diagnostics
│   ├── exceptions.py       # Custom exception types
│   ├── health.py           # Health check
│   ├── workspace_integrity.py  # Workspace validation
│   ├── py.typed            # PEP 561 marker
│   │
│   ├── bot/                # Bot core (split from monolithic bot.py)
│   │   ├── _bot.py         # Bot implementation
│   │   ├── context_building.py  # LLM turn context assembly from routing match
│   │   ├── response_delivery.py # Post-ReAct delivery pipeline (filter, dedup, persist)
│   │   ├── crash_recovery.py  # Crash recovery
│   │   ├── preflight.py    # Preflight checks
│   │   └── react_loop.py   # ReAct agentic loop
│   │
│   ├── channels/           # Communication channel implementations
│   │   ├── base.py         # Channel base classes
│   │   ├── cli.py          # CLI channel for testing
│   │   ├── whatsapp.py     # WhatsApp channel
│   │   ├── neonize_backend.py  # Neonize WhatsApp backend
│   │   ├── stealth.py      # Stealth/anti-detection
│   │   └── validation.py   # Channel validation
│   │
│   ├── config/             # Configuration system (split from monolithic config.py)
│   │   ├── config.py       # Facade re-exporting from split modules
│   │   ├── config_schema.py     # JSON Schema validation
│   │   ├── config_schema_defs.py  # Pure dataclass definitions
│   │   ├── config_loader.py     # JSON I/O, dict→dataclass, env overrides
│   │   ├── config_validation.py # Validation helpers
│   │   └── config_watcher.py    # Polling-based hot-reload
│   │
│   ├── constants/          # Named constants split by domain (15 modules)
│   │   ├── cache.py, db.py, health.py, llm.py, memory.py
│   │   ├── messaging.py, network.py, routing.py, scheduler.py
│   │   ├── security.py, shutdown.py, skills.py, workspace.py
│   │   └── ...
│   │
│   ├── core/               # Core engine (16 modules)
│   │   ├── context_assembler.py  # Memory + instructions + project context
│   │   ├── dedup.py         # Inbound + outbound dedup with buffered batch
│   │   ├── event_bus.py     # Async typed pub/sub (10 events + emit_error_event)
│   │   ├── instruction_loader.py # Instruction file loading
│   │   ├── message_pipeline.py   # Message processing middleware chain
│   │   ├── orchestrator.py  # StepOrchestrator for dependency-ordered execution
│   │   ├── startup.py       # Startup sequence (StartupOrchestrator + ComponentSpec)
│   │   ├── tool_executor.py # Skill execution with rate-limit, timeout, audit
│   │   └── ... (context_builder, errors, project_context, serialization, etc.)
│   │
│   ├── db/                 # Database layer (13 modules)
│   │   ├── db.py            # File-based JSONL persistence (facade)
│   │   ├── sqlite_pool.py   # Shared connection pool for SQLite
│   │   ├── file_pool.py     # Bounded file handle pool
│   │   ├── message_store.py # JSONL message persistence
│   │   └── ... (compression, db_utils, db_index, db_integrity, db_validation, generations, migration, sqlite_utils)
│   │
│   ├── health/             # Health check HTTP endpoint
│   │   ├── checks.py, middleware.py, models.py, prometheus.py, registry.py, server.py
│   │
│   ├── logging/            # Structured logging (3 modules: config, llm, http)
│   │
│   ├── monitoring/         # Monitoring & metrics (5 modules: performance, memory, metrics_types, tracing, workspace_monitor)
│   │
│   ├── project/            # Project & knowledge (dates, graph, recall, store)
│   │
│   ├── security/           # Security subsystem (defense-in-depth: audit, path_validator, prompt_injection, signing, url_sanitizer)
│   │
│   ├── skills/             # Dual skill system (builtin + user)
│   │   ├── base.py, prompt_skill.py
│   │   └── builtin/ files, media, memory_vss, planner, shell, web_research, etc.
│   │
│   ├── templates/instructions/  # Instruction templates (chat.agent.md, personal.agent.md)
│   │
│   ├── ui/                 # User interface (cli_output, options_tui)
│   │
│   ├── utils/              # Utilities (20 modules)
│   │   ├── async_executor, async_file, background_service, circuit_breaker
│   │   ├── dag, disk, frontmatter, json_utils, locking, logging_utils
│   │   ├── path, phone, protocols, registry, retry, singleton, timing
│   │   ├── type_guards, validation
│   │
│   └── vector_memory/      # Semantic memory (sqlite-vec): __init__, _utils, batch, health
│
├── tests/                   # Test suite (3 tiers)
│   ├── conftest.py           # Shared fixtures (fully-wired Bot mock)
│   ├── unit/                 # Unit tests
│   ├── integration/          # Integration tests
│   └── e2e/                  # End-to-end tests (76 total)
│
├── workspace/               # Runtime workspace (configurable)
│   ├── config.json           # Active configuration
│   ├── routing.json          # Routing rules
│   ├── whatsapp_session.db   # WhatsApp session
│   ├── .data/                # Database files
│   ├── instructions/         # LLM instruction files
│   ├── logs/                 # Application logs
│   ├── skills/               # User-defined skills
│   └── whatsapp_data/        # Per-chat workspaces
│
├── .opencode/context/       # AI assistant context files (~235 files)
├── .github/workflows/ci.yml # CI pipeline
└── .pre-commit-config.yaml  # Pre-commit hooks (ruff)
```

## Key Directories

| Directory | Purpose | Important |
|-----------|---------|-----------|
| `src/` | All application logic organized by module | Core codebase |
| `src/bot/` | Bot orchestrator (split from monolith) | ReAct loop, context building, response delivery, crash recovery |
| `src/config/` | Config system (split into 6 modules) | Schema, loader, watcher |
| `src/core/` | Engine: event bus, pipeline, orchestration | 16 modules |
| `src/channels/` | Communication channel implementations | WhatsApp, CLI |
| `src/skills/` | Dual skill system (builtin + user) | 11 builtin skills |
| `src/security/` | Defense-in-depth (5 modules) | Audit, injection detection |
| `src/db/` | Database layer with connection pooling | 13 modules |
| `workspace/` | ALL runtime files | logs, database, session, per-chat data |

## Codebase References

- `main.py` — Entry point
- `src/app.py` — Application class
- `src/bot/_bot.py` — Core bot implementation
- `src/builder.py` — Public builder API

## Related Files

- `concepts/architecture.md` — How these directories relate architecturally
- `lookup/tech-stack.md` — What technologies live where
- `guides/dev-environment.md` — Setup and development workflow
