<!-- Context: project-intelligence/lookup/project-structure | Priority: high | Version: 4.0 | Updated: 2026-05-02 -->

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
├── PLAN.md                  # Improvement plan (50 items, 47 done)
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
│   ├── progress.py         # Progress tracking
│   ├── rate_limiter.py     # Sliding window per-chat and per-skill rate limiting
│   ├── routing.py          # Message routing engine
│   ├── scheduler.py        # Background scheduled tasks
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
│   ├── core/               # Core engine
│   │   ├── context_assembler.py  # Memory + instructions + project context
│   │   ├── context_builder.py    # LLM context construction
│   │   ├── dedup.py         # Inbound + outbound dedup
│   │   ├── errors.py        # 25+ categorized fire-and-forget errors
│   │   ├── event_bus.py     # Async typed pub/sub
│   │   ├── instruction_loader.py # Instruction file loading
│   │   ├── message_pipeline.py   # Message processing pipeline
│   │   ├── orchestrator.py  # StepOrchestrator for dependency-ordered execution
│   │   ├── project_context.py    # Project context injection
│   │   ├── serialization.py # Safe JSON serialization
│   │   ├── startup.py       # Startup sequence
│   │   ├── stream_accumulator.py # SSE streaming delta reconstruction
│   │   ├── tool_executor.py # Skill execution with rate-limit, timeout, audit
│   │   ├── tool_formatter.py # Tool call result formatting
│   │   └── topic_cache.py   # Topic-based context caching
│   │
│   ├── db/                 # Database layer
│   │   ├── db.py            # File-based JSONL persistence
│   │   ├── db_utils.py      # Shared DB helper functions
│   │   ├── db_index.py      # Message search index
│   │   ├── db_integrity.py  # Database integrity checks
│   │   ├── db_validation.py # Database validation
│   │   ├── compression.py   # Data compression
│   │   ├── file_pool.py     # Bounded file handle pool
│   │   ├── generations.py   # LLM response generation tracking
│   │   ├── message_store.py # JSONL message persistence
│   │   ├── migration.py     # Schema migration support
│   │   ├── sqlite_pool.py   # Shared connection pool for SQLite
│   │   └── sqlite_utils.py  # SqliteHelper with pool integration
│   │
│   ├── health/             # Health check HTTP endpoint
│   │   ├── checks.py, middleware.py, models.py
│   │   ├── prometheus.py, server.py
│   │
│   ├── logging/            # Structured logging
│   │   ├── logging_config.py    # Structured logging with JSON format
│   │   ├── llm_logging.py       # Per-request LLM logging to JSON
│   │   └── http_logging.py      # HTTP request/response logging
│   │
│   ├── monitoring/         # Monitoring & metrics
│   │   ├── performance.py       # Performance metrics
│   │   ├── memory.py            # Memory monitoring
│   │   ├── metrics_types.py     # Metrics type definitions
│   │   ├── tracing.py           # OpenTelemetry span helpers
│   │   └── workspace_monitor.py # Filesystem cleanup
│   │
│   ├── project/            # Project & knowledge
│   │   ├── dates.py, graph.py, recall.py, store.py
│   │
│   ├── security/           # Security subsystem (defense-in-depth)
│   │   ├── audit.py             # HMAC-SHA256 chained audit log
│   │   ├── path_validator.py    # TOCTOU-safe path validation
│   │   ├── prompt_injection.py  # Multi-language injection detection
│   │   ├── signing.py           # HMAC-SHA256 task integrity
│   │   └── url_sanitizer.py     # URL redaction for logging
│   │
│   ├── skills/             # Dual skill system (builtin + user)
│   │   ├── base.py              # BaseSkill abstract class
│   │   ├── prompt_skill.py      # Prompt-based skills (Markdown)
│   │   └── builtin/
│   │       ├── files.py, media.py, memory_vss.py
│   │       ├── planner.py, project_skills.py, routing.py
│   │       ├── shell.py, skills_manager.py
│   │       ├── task_scheduler.py, web_research.py
│   │
│   ├── templates/          # Instruction templates
│   │   └── instructions/
│   │       ├── chat.agent.md
│   │       └── personal.agent.md
│   │
│   ├── ui/                 # User interface
│   │   ├── cli_output.py        # Colorful CLI output (Rich)
│   │   └── options_tui.py       # Configuration editor TUI
│   │
│   ├── utils/              # Utilities (19 modules)
│   │   ├── async_executor.py    # Bounded concurrency executor
│   │   ├── async_file.py        # Async file operations
│   │   ├── background_service.py # Background service pattern
│   │   ├── circuit_breaker.py   # Circuit breaker pattern
│   │   ├── dag.py               # Topological sort
│   │   ├── disk.py              # Disk utilities
│   │   ├── frontmatter.py       # YAML frontmatter parsing
│   │   ├── json_utils.py        # JSON utilities
│   │   ├── locking.py           # Lock utilities
│   │   ├── logging_utils.py     # Logging helpers
│   │   ├── path.py              # Path utilities
│   │   ├── phone.py             # Phone normalization
│   │   ├── protocols.py         # Protocol classes (Channel, Skill, Storage)
│   │   ├── retry.py             # Exponential backoff retry
│   │   ├── singleton.py         # Singleton pattern
│   │   ├── timing.py            # Timing utilities
│   │   └── type_guards.py       # Runtime type checking
│   │
│   └── vector_memory/      # Semantic memory (sqlite-vec)
│       ├── __init__.py, _utils.py, batch.py, health.py
│
├── tests/                   # Test suite (3 tiers)
│   ├── conftest.py           # Shared fixtures (fully-wired Bot mock)
│   ├── unit/                 # 40+ unit tests
│   ├── integration/          # 8 integration tests
│   └── e2e/                  # 5 end-to-end tests
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
| `src/bot/` | Bot orchestrator (split from monolith) | ReAct loop, crash recovery |
| `src/config/` | Config system (split into 6 modules) | Schema, loader, watcher |
| `src/core/` | Engine: event bus, pipeline, orchestration | 16 modules |
| `src/channels/` | Communication channel implementations | WhatsApp, CLI |
| `src/skills/` | Dual skill system (builtin + user) | 11 builtin skills |
| `src/security/` | Defense-in-depth (5 modules) | Audit, injection detection |
| `src/db/` | Database layer with connection pooling | 12 modules |
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
