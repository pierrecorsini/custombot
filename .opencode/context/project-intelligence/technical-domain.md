<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.4 | Updated: 2026-05-06 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot.
**Last Updated**: 2026-05-06

## Quick Reference

**Update Triggers**: Tech stack changes | New patterns | Architecture decisions
**Audience**: Developers, AI agents

---

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | 3.11+ | asyncio, type hints, dataclasses |
| LLM Client | OpenAI Python SDK | 2.29.x | Async Chat Completions, any OpenAI-compatible provider |
| WhatsApp | neonize | 0.3.17 | Native Python whatsmeow binding, no Node.js bridge |
| Vector Search | sqlite-vec | 0.1.9 | Embedded vector similarity search |
| CLI | Click | 8.3.x | Command groups, options, TUI integration |
| Terminal UI | Rich | 14.3.x | Colored output, progress bars |
| HTTP | httpx + aiohttp | 0.28/3.13 | Async HTTP, SSE streaming |
| Monitoring | OpenTelemetry | 1.30.x | Tracing, metrics |
| Serialization | orjson + msgpack | 3.10/1.1 | Fast JSON/binary encoding |

---

## Architecture

```
main.py (Click CLI)
  └── Application (src/app.py) — lifecycle state machine
        ├── StartupOrchestrator — ordered dependency-graph startup
        ├── BaseChannel — abstract channel (WhatsApp, CLI)
        ├── MessagePipeline — middleware chain (dedup, routing, processing)
        ├── Bot (src/bot/) — ReAct loop + context building + delivery
        ├── LLMProvider (src/llm/) — OpenAI-compatible async client
        ├── Database (src/db/) — JSONL persistence, compression, pooling
        ├── Skills (src/skills/) — Python classes + markdown prompt skills
        ├── TaskScheduler (src/scheduler/) — cron, interval, daily tasks
        └── GracefulShutdown — ordered cleanup with timeouts
```

### Key Modules (159 Python files, 18 packages)

| Module | Purpose | Key Files |
|--------|---------|-----------|
| `src/bot/` | ReAct loop, context building, crash recovery, preflight, response delivery | `_bot.py`, `react_loop.py`, `context_building.py`, `response_delivery.py` |
| `src/channels/` | Abstract channel + WhatsApp/neonize + stealth mode | `base.py`, `whatsapp.py`, `neonize_backend.py` |
| `src/config/` | Dataclass config + JSON schema validation + hot-reload | `config_schema_defs.py`, `config_watcher.py` |
| `src/core/` | Orchestrator, event bus, pipeline, tool execution | `orchestrator.py`, `message_pipeline.py`, `dedup.py` |
| `src/db/` | JSONL storage, file pool, compression, validation, migration | `db.py`, `file_pool.py`, `sqlite_pool.py` |
| `src/llm/` | Async OpenAI client, circuit breaker, streaming | `_client.py`, `_provider.py`, `_error_classifier.py` |
| `src/scheduler/` | Cron expressions, persistence, result comparison | `engine.py`, `cron.py`, `persistence.py` |
| `src/security/` | Path validation, prompt injection detection, signing | `path_validator.py`, `prompt_injection.py` |
| `src/skills/` | BaseSkill ABC + builtins + prompt skill loader | `base.py`, `prompt_skill.py`, `builtin/` |
| `src/vector_memory/` | sqlite-vec embeddings, batch indexing, health checks | `batch.py`, `health.py` |
| `src/monitoring/` | Metrics, tracing, workspace monitoring, NullMemoryMonitor | `performance.py`, `tracing.py`, `memory.py` |
| `src/health/` | HTTP /health endpoint, HealthCheckRegistry, Prometheus metrics | `server.py`, `registry.py`, `checks.py`, `prometheus.py` |
| `src/utils/` | Circuit breaker, DAG, locking, retry, registry, validation | `circuit_breaker.py`, `dag.py`, `retry.py`, `registry.py` |

---

## Code Patterns

### Application Lifecycle

```python
class AppPhase(Enum):
    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    SHUTTING_DOWN = auto()
    STOPPED = auto()
```

Validated transitions via `_transition()` — prevents misuse.

### ReAct Loop Pattern

```python
async def react_loop(bot, messages, tools, max_iterations=10):
    for i in range(max_iterations):
        response = await llm.chat(messages, tools=tools)
        if response.has_tool_calls:
            results = await execute_tools(response.tool_calls)
            messages.append(tool_results(results))
        else:
            return response.content  # final answer
```

### Channel Abstract Base

```python
class BaseChannel(ABC):
    async def start(self, on_message): ...
    async def send(self, chat_id, text): ...
    async def wait_connected(self): ...
```

### WAL-Protected Persistence

Queue writes go through a write-ahead log for crash safety. Each flush writes to
`.wal.tmp`, atomically commits to `.wal`, then appends to the main file. On
startup, `_replay_wal()` re-applies any committed but unmerged entries.

```python
def _wal_append(self, lines):
    self._wal_tmp_file.write_text(content)      # Step 1: temp write
    self._wal_tmp_file.replace(self._wal_file)   # Step 2: atomic commit
    # Step 3: append to main file + fsync
    # Step 4: remove committed WAL
```

### Msgpack+Base64 Queue Serialization

Message queue lines are base64-wrapped msgpack blobs (~3–5× faster than JSON
for structured data). JSON fallback on read ensures backward compatibility.

```python
def _encode_record(data: dict) -> str:
    return base64.b64encode(msgpack_dumps(data)).decode("ascii")
def _decode_line(line: str) -> dict | None:
    # try msgpack+base64 first, fallback to JSON
```

---

## Naming Conventions

| Type | Convention | Example |
|------|-----------|---------|
| Files | `snake_case.py` | `context_building.py`, `react_loop.py` |
| Classes | `PascalCase` | `MessagePipeline`, `BaseChannel` |
| Functions | `snake_case` | `handle_message`, `process_scheduled` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_MESSAGE_LENGTH`, `WORKSPACE_DIR` |
| Private | `_leading_underscore` | `_validate_chat_id`, `_on_message` |
| Config | `snake_case` fields | `max_tokens`, `base_url` |

---

## Code Standards

- **Ruff** for linting + formatting (target: py311, line-length: 100)
- **flake8-type-checking strict** — imports under `TYPE_CHECKING` guard
- **mypy** strict mode for `src.bot.*`; check_untyped_defs elsewhere
- **pytest** with asyncio_mode="auto", pytest-timeout, hypothesis
- **Dataclasses** for config and data models; frozen=True for immutable state
- **Protocol-based structural subtyping** — avoid inheritance for interfaces
- **AsyncLock** for all file I/O (never blocking the event loop)
- **Circuit breaker** for external calls (LLM, DB writes)
- **Per-module constants** in `src/constants/` — no magic numbers

---

## Security Requirements

- Path traversal protection (`..` blocked in file skills)
- Shell command denylist/allowlist configuration
- Prompt injection detection in `src/security/prompt_injection.py`
- Config file permission checks (chmod 600 warning)
- URL sanitization for logging (strip API keys)
- HMAC signing for scheduled task prompts
- Input validation with `_validate_chat_id` regex

---

## 📂 Codebase References

**Entry Point**: `main.py` — Click CLI with start/options/diagnose commands
**Application**: `src/app.py` — Lifecycle state machine, startup/shutdown
**Config**: `src/config/config_schema_defs.py` — All dataclass definitions
**Build Config**: `pyproject.toml` — Ruff, mypy, pytest settings
**Docker**: `Dockerfile` — Container build

## Related Files

- Navigation: `navigation.md`
- Core Standards: `../core/standards/code-quality.md`
- Development Context: `../development/navigation.md`
