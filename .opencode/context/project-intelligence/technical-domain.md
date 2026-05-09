<!-- Context: project-intelligence/technical | Priority: critical | Version: 2.15 | Updated: 2026-05-08 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot.
**Last Updated**: 2026-05-08

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
        ├── BuilderOrchestrator (builder.py) — dependency-ordered component wiring
        ├── BaseChannel — abstract channel (WhatsApp, CLI)
        ├── RoutingEngine (routing.py) — frontmatter-based message routing
        ├── MessagePipeline — middleware chain (dedup, routing, processing)
        ├── Bot (src/bot/) — ReAct loop + context building + delivery
        │     └── BotDeps (builder.py) — injected collaborators
        ├── LLMProvider (src/llm/) — OpenAI-compatible async client
        ├── Database (src/db/) — JSONL persistence, compression, pooling
        ├── Memory (memory.py) — per-chat persistent MEMORY.md files
        ├── MessageQueue (message_queue.py) — persistent queue with crash recovery
        │     ├── MessageQueuePersistence — WAL-protected msgpack serialization
        │     └── MessageQueueBuffer — swap-buffers write pattern
        ├── RateLimiter (rate_limiter.py) — per-chat, per-skill sliding window
        ├── Skills (src/skills/) — 10 builtins + prompt skills
        ├── TaskScheduler (src/scheduler/) — cron, interval, daily tasks
        ├── ProjectKnowledge (src/project/) — graph, recall, store
        ├── HealthServer (src/health/) — HTTP /health + Prometheus /metrics
        └── GracefulShutdown (shutdown.py) — ordered cleanup with in-flight tracking
```

### Key Modules (161 Python files, 19 packages)

| Module | Purpose | Key Files |
|--------|---------|-----------|
| `src/bot/` | ReAct loop, context building, crash recovery, preflight, response delivery | `_bot.py`, `react_loop.py`, `context_building.py`, `crash_recovery.py`, `response_delivery.py`, `preflight.py` |
| `src/channels/` | Abstract channel + WhatsApp/neonize + stealth mode + message validation | `base.py`, `whatsapp.py`, `neonize_backend.py`, `cli.py`, `message_validator.py`, `stealth.py`, `validation.py` |
| `src/config/` | Dataclass config + JSON schema validation + hot-reload + structured diff | `config.py`, `config_schema_defs.py`, `config_loader.py`, `config_watcher.py`, `config_validation.py` |
| `src/core/` | Orchestrator, event bus (backpressure + strict validation + bounded counts + rate-tracker memory exposure + BaseException hardening), pipeline, tool execution (per-skill breaker registry), unified dedup (NullDedupService + single-hash check+record + pooled xxhash), correlation scope, instance-level safe-mode | `orchestrator.py`, `message_pipeline.py`, `dedup.py`, `event_bus.py`, `tool_executor.py`, `skill_breaker_registry.py`, `startup.py`, `context_assembler.py`, `context_builder.py`, `topic_cache.py`, `instruction_loader.py`, `project_context.py`, `serialization.py`, `stream_accumulator.py`, `tool_formatter.py`, `errors.py` |
| `src/db/` | JSONL storage, file pool, compression, SQLite pooling, indexing, integrity, migration | `db.py`, `file_pool.py`, `sqlite_pool.py`, `db_utils.py`, `compression.py`, `message_store.py`, `migration.py`, `db_index.py`, `db_integrity.py`, `db_validation.py`, `generations.py`, `sqlite_utils.py` |
| `src/llm/` | Async OpenAI client, circuit breaker, streaming, error classification | `_client.py`, `_provider.py`, `_error_classifier.py` |
| `src/scheduler/` | Cron expressions, persistence, result comparison | `engine.py`, `cron.py`, `persistence.py` |
| `src/security/` | Path validation, prompt injection detection, signing, audit, URL sanitizer | `path_validator.py`, `prompt_injection.py`, `audit.py`, `url_sanitizer.py`, `signing.py` |
| `src/skills/` | BaseSkill ABC + 10 builtins + prompt skill loader | `base.py`, `prompt_skill.py`, `builtin/` (files, media, memory_vss, planner, project_skills, routing, shell, skills_manager, task_scheduler, web_research) |
| `src/vector_memory/` | sqlite-vec embeddings, batch indexing, health checks, graceful degradation | `__init__.py`, `batch.py`, `health.py`, `_utils.py` |
| `src/monitoring/` | Metrics, tracing, workspace monitoring, NullMemoryMonitor, per-chat latency percentiles, error-rate alerting | `performance.py`, `tracing.py`, `memory.py`, `workspace_monitor.py`, `metrics_types.py` |
| `src/logging/` | Structured logging — JSON/text, StructuredContextFilter, redaction | `logging_config.py`, `llm_logging.py`, `http_logging.py` |
| `src/health/` | HTTP /health, HealthCheckRegistry (per-check timeout), Prometheus /metrics | `server.py`, `registry.py`, `checks.py`, `prometheus.py`, `middleware.py`, `models.py` |
| `src/constants/` | Per-domain constants (14 modules) | `app.py`, `cache.py`, `db.py`, `health.py`, `llm.py`, `memory.py`, `messaging.py`, `network.py`, `routing.py`, `scheduler.py`, `security.py`, `shutdown.py`, `skills.py`, `workspace.py` |
| `src/project/` | Knowledge graph (BFS), recall, persistent store, date parsing | `dates.py`, `graph.py`, `recall.py`, `store.py` |
| `src/ui/` | CLI output formatting, interactive TUI options | `cli_output.py`, `options_tui.py` |
| `src/utils/` | Circuit breaker, DAG, locking, retry, ComponentRegistry, validation, protocols (19 modules) | `circuit_breaker.py`, `dag.py`, `registry.py`, `validation.py`, `retry.py`, `protocols.py`, `locking.py`, `async_executor.py`, `async_file.py`, `background_service.py`, `disk.py`, `frontmatter.py`, `json_utils.py`, `logging_utils.py`, `path.py`, `phone.py`, `singleton.py`, `timing.py`, `type_guards.py` |

### Top-Level Modules

| Module | Lines | Purpose |
|--------|-------|---------|
| `app.py` | ~844 | Application lifecycle state machine, startup/shutdown, main retry loop |
| `builder.py` | ~624 | BuilderOrchestrator — dependency-ordered component wiring with progress + `_build_bot_deps` factory |
| `routing.py` | ~849 | RoutingEngine — frontmatter rules, watchdog/mtime reload, match cache |
| `rate_limiter.py` | ~585 | Per-chat, per-skill sliding-window rate limiting + expensive skill flag |
| `memory.py` | ~676 | Per-chat MEMORY.md files, LRU cache, mtime-based freshness |
| `message_queue.py` | ~525 | Persistent queue orchestrator with crash recovery, stale reprocessing |
| `message_queue_persistence.py` | ~641 | WAL-protected msgpack+base64+CRC32 serialization, crash replay |
| `message_queue_buffer.py` | ~212 | Swap-buffers write pattern, detached flush without lock hold |
| `exceptions.py` | ~495 | Domain exception hierarchy with user-friendly errors + error codes |
| `lifecycle.py` | ~464 | Startup/shutdown lifecycle logging with timing and component status |
| `progress.py` | ~539 | Rich spinner/progress bar with automatic threshold detection |
| `diagnose.py` | ~1084 | System diagnostic report (config, connectivity, workspace, deps) |
| `shutdown.py` | ~219 | GracefulShutdown — ordered signal-based teardown, in-flight tracking |
| `workspace_integrity.py` | ~262 | Workspace structure validation and repair |
| `dependency_check.py` | ~183 | Dependency version validation |

---

## Code Patterns

### Application Lifecycle

```python
class AppPhase(Enum):
    CREATED = auto(); STARTING = auto(); RUNNING = auto()
    SHUTTING_DOWN = auto(); STOPPED = auto()
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
            return response.content
```

### BuilderOrchestrator (Dependency-Ordered Wiring)

`BuilderOrchestrator` in `builder.py` — declarative `BuilderComponentSpec` (name, factory, deps)
executed in dependency order with logging, timing, and Rich progress bars. Produces `BotComponents`
dataclass with all wired collaborators. `_build_bot_deps()` factory isolates collaborator wiring.

### WAL-Protected Persistence

Queue writes: `.wal.tmp` → atomic `replace()` → `.wal` → append to main + fsync.
On startup, `_replay_wal()` re-applies committed but unmerged entries.

### Queue Decomposition (3-File Split)

`message_queue.py` (orchestrator) → `message_queue_persistence.py` (WAL, msgpack+CRC32
serialization, crash replay) → `message_queue_buffer.py` (swap-buffers flush, detached
write without lock hold). Separation isolates persistence and buffer concerns for independent testing.

### Structured Logging (Auto-Inject)

`StructuredContextFilter` auto-injects `correlation_id`, `chat_id`, `app_phase`,
`session_id` into every `LogRecord` via `ContextVar`. Eliminates manual `extra={}` dicts.

### Component Registry (Lightweight DI)

`ComponentRegistry` — fail-fast deps: `require("db")` raises if missing, `get("vm")` returns None.
`StartupContext` / `BuilderContext` both delegate to it via shared attribute-forwarding.

### Per-Skill Circuit Breaker

`ToolExecutor` wraps each skill in a per-skill-name `CircuitBreaker`. Broken/hanging skills
don't consume ReAct loop iterations. Capped with `MAX_TRACKED_SKILLS` + LRU eviction.

### Unified Deduplication Service

`DeduplicationService` in `src/core/dedup.py` — three strategies:
- **Inbound**: message_id vs DB persistent index + LRU cache
- **Outbound**: xxHash (xxh64) content hash + TTL-based LRU cache
- **Request**: per-chat content-hash within short TTL (catches double-sends)

### Incremental Chats Persistence

`_save_chats()` tracks dirty IDs → append-only changelog (O(dirty)). Compacts to full
snapshot at threshold. Startup replays changelog on base snapshot.

### Batch Inbound Dedup Lookups

`batch_check_inbound(message_ids)` queries the message_id_index once for all IDs.

### BotDeps Injection Pattern

`BotDeps` receives fully-wired collaborators (constructed in `builder.py`). Tests construct
manually, production uses `build_bot()`. RateLimiter, ToolExecutor, ContextAssembler injected.

### Health Check Per-Check Timeout

`HealthCheckRegistry.run_all()` wraps each check in `asyncio.wait_for(timeout=...)`.
Hung check → DEGRADED status, doesn't block `/health` endpoint.

### Retry Budget Recovery

`Database._retry_budget_spent` resets to 0 when circuit breaker `record_success()` fires.
Prevents permanent retry disablement after transient filesystem degradation.

### Skills Declare Expensive

`BaseSkill` has `expensive: bool` attribute → merged into `RateLimiter._skill_limiters`
expensive set at registration. No hardcoded `EXPENSIVE_SKILLS` frozenset.

### Msgpack+Base64+CRC32 Queue Serialization

Queue lines are `CRC32:base64(msgpack)` blobs. CRC32 checksum detects truncated writes
and bit-rot. JSON fallback on read ensures backward compatibility.
Legacy unguarded msgpack+base64 lines also supported on read.

### Coalesced Debounce Flush

`Database._save_chats()` uses `loop.call_later` to coalesce rapid `upsert_chat` calls —
multiple dirty writes within the debounce window share a single scheduled flush,
reducing redundant disk I/O. `_chats_flush_handle` tracks pending flush, cancelled on close.

### RegistryBackedMixin (Attribute Forwarding)

`RegistryBackedMixin` in `src/utils/registry.py` — shared mixin for `StartupContext` and
`BuilderContext` providing `__getattr__`/`__setattr__` delegation to `ComponentRegistry`.
Eliminates duplicated attribute-forwarding boilerplate; both contexts evolve in lockstep.

### SkillBreakerRegistry (Capped Per-Skill Breakers)

`SkillBreakerRegistry` in `src/core/skill_breaker_registry.py` — wraps per-skill circuit
breaker management with configurable `max_skills` cap and LRU eviction. Replaces raw
`dict[str, CircuitBreaker]` in `ToolExecutor` to prevent unbounded memory growth.

### Per-Chat Latency Percentiles

`PerformanceMetrics.track_message_latency()` records per-chat latency samples in bounded
LRU deques (top-N chats by volume). Exposes `p50`/`p95`/`p99` per chat via `ChatLatencyPercentiles`
in `PerformanceSnapshot`. Identifies slow conversations without global averaging.

### Error-Rate Alerting Thresholds

Configurable per-window alert thresholds (`DEFAULT_ERROR_ALERT_THRESHOLDS`) check error rates
over 5m/15m/60m windows. When exceeded, structured `error_rate_exceeded` warning logged with
extra fields for external alerting (ELK, Datadog). `ERROR_ALERT_COOLDOWN_SECONDS` prevents spam.

### Fail Fast on Persistence Failure

`_prepare_turn()` sets `persistence_failed` flag when user-message persistence fails.
`deliver_response()` skips assistant persistence too, preventing inconsistent state where
the assistant references a user message that doesn't exist in history on restart.

### TTL-Based Lock Cache Eviction

`LRULockCache` optional `ttl` parameter lazily evicts idle locks not accessed within the
TTL window. Reclaims memory from transient group chats. Configurable via `max_chat_lock_cache_ttl`.

### Semaphore Resize on Hot-Reload

`Application._message_semaphore` resizes when `max_concurrent_messages` changes via config
hot-reload, allowing runtime concurrency adjustments without restart.

### Attempt Counter Reset on Category Transition

`_run_with_retry()` resets the attempt counter when the error category changes between
consecutive failures, giving each error category its own independent retry budget.

### Strict Event Name Validation

`EventBus` opt-in `strict_event_names` mode raises `ValueError` for unknown event names,
preventing silent typos. Default mode logs WARNING for unknown names. `EVENT_ERROR_RATE_EXCEEDED`
added as new known event.

### EventBus Backpressure

`EventBus.emit()` uses a bounded semaphore to cap concurrent handler invocations per emission,
preventing unbounded `asyncio.gather` fan-out from overwhelming subscribers.

### Shared Error Classification

`src/llm/_error_classifier.py` provides `is_retryable(code)` — single source of truth
for LLM error retry decisions across `_bot.py` and the classifier.

### Exception Hierarchy

`src/exceptions.py` — domain exceptions (LLMError, DatabaseError, BridgeError, SkillError,
ConfigurationError, RoutingError) with user-friendly messages, error codes, and doc links.

### Raw Payload Size Cap

`MessageValidator._validate_raw()` serializes `IncomingMessage.raw` to JSON bytes and strips
the field when it exceeds `MAX_RAW_PAYLOAD_SIZE` (64 KB). Matches the truncation-not-rejection
pattern used by `_validate_sender_name`. Constant in `src/constants/security.py`.

### Tracked Flush Futures

`Database._start_tracked_flush()` replaces bare `asyncio.ensure_future()` with stored future
references + `_on_flush_done` callback. Prevents "Task exception was never retrieved" warnings
in Python 3.11+. `_cancel_flush_future()` cancels on close/manual flush.

### `message_dropped` Event for Rate-Limited Messages

`Bot.handle_message()` emits `message_dropped` event with `reason`, `limit_type`, `limit_value`
when rate-limited — closing the observability gap where other rejection paths (no routing, ACL)
already emitted events but rate-limiting was silent.

### `send_and_track` Guard on Send Failure

`BaseChannel.send_and_track()` now returns early on `send_message()` exception — skips outbound
dedup recording and `response_sent` event emission. Previously both fired unconditionally,
inflating metrics and preventing retry via dedup cache for messages that were never actually sent.

### `BaseException` Guard in TaskScheduler

`TaskScheduler._run_loop()` catches `BaseException` (SystemExit, KeyboardInterrupt) separately
from `Exception`, logs at CRITICAL, and re-raises — ensuring task state is consistent on
non-recoverable signals instead of leaving partial mutations.

### HMAC Verification `error_occurred` Event

`Bot.process_scheduled()` emits `error_occurred` event with `error_type: hmac_verification_failure`
alongside existing `audit_log()` call — enabling event-bus subscribers to alert on potential
prompt-injection attacks against scheduled tasks.

### `validate_connection()` Off Event Loop

`Database.validate_connection()` delegates synchronous filesystem checks to `asyncio.to_thread()`
via `_validate_connection_sync()` — eliminates startup stalls from blocking reads of
`chats.json`, `message_index.json`, and JSONL samples.

### `is_outbound_duplicate()` Deprecation Warning

`DeduplicationService.is_outbound_duplicate()` emits `DeprecationWarning` at runtime directing
callers to the two-phase `check_outbound_duplicate()` + `record_outbound()` API. Scheduled
for removal in a future version.

### `HandleMessageMiddleware` Outbound Tracking

`HandleMessageMiddleware.__call__()` now delegates to `BaseChannel.send_and_track()` for
outbound responses, ensuring outbound dedup recording and `response_sent` event emission
are uniform across all outbound paths (normal + error responses).

### Error-Reply Rate Limiting (`ErrorHandlerMiddleware`)

`ErrorHandlerMiddleware._send_error_reply()` applies per-chat sliding-window rate limiting
via `_error_reply_trackers` (LRU OrderedDict, `SlidingWindowTracker`). After
`_ERROR_REPLY_MAX_LIMIT` error replies within `_ERROR_REPLY_WINDOW_SECONDS`, further
replies are suppressed and logged — preventing traffic amplification attacks.

### Bounded EventBus Rate Trackers

`EventBus._rate_trackers` uses `LRUDict` with `max_rate_trackers` cap (default:
`DEFAULT_MAX_RATE_TRACKERS`). Unknown event names don't cause unbounded memory growth.
Eviction follows LRU policy matching the rate limiter pattern.

### `_write_marker()` Helper (DB Compaction)

Extracted from `_compact_chats` into `_write_marker(marker_path, content)` using
`_atomic_write` consistently. Eliminates crash-safety gap where partial marker writes
could cause incorrect recovery behavior.

### `per_chat_timeout` Error Event

`Bot._handle_message_inner()` emits `error_occurred` event via `emit_error_event()` when
`asyncio.wait_for` raises `TimeoutError`. Monitoring subscribers see stuck turns in the
event stream alongside existing structured log output.

### ACL Rejection `message_dropped` Event

`Bot.handle_message()` emits `message_dropped` event with `reason="acl_rejected"` when
`msg.acl_passed` is `False`, closing the observability gap where other rejection paths
(no routing, too long, rate-limited) already emitted events.

### InstructionLoader File-Size Cap

`_check_file_size()` in `instruction_loader.py` rejects files exceeding
`MAX_INSTRUCTION_FILE_SIZE` (1 MiB) with `ValueError`. Applied at both `load()` and
`get_raw_content()` entry points before reading. Prevents compromised or accidentally
huge instruction files from exhausting LLM context memory. Constant in
`src/constants/security.py`.

### High-Confidence Injection Block in Scheduled Tasks

`process_scheduled()` blocks execution when injection confidence >=
`INJECTION_BLOCK_CONFIDENCE` (0.8), returning `None` instead of forwarding to the LLM.
Emits `error_occurred` event with `error_type: injection_blocked` for security alerting.
Medium-confidence detections (0.6) continue to be sanitized and forwarded. Constant in
`src/constants/security.py`.

### NullDedupService (NullObject Pattern)

`NullDedupService` in `src/core/dedup.py` — no-op implementation of the
`DeduplicationService` interface. `Bot._dedup` is now typed as `DeduplicationService`
(not `Optional`), defaulting to `NullDedupService` when no real dedup is provided.
Eliminates 8+ `if self._dedup` guards across `_bot.py`, `response_delivery.py`,
and `message_pipeline.py`. Check methods always return `False`; record methods are
no-ops; `stats` returns zeroed `DedupStats`. Follows the same `NullMemoryMonitor`
pattern established earlier.

### Per-Component Applier Results in Config Hot-Reload

`ConfigChangeApplier.apply()` now includes `applier_results: {"bot": "ok",
"llm": "failed", ...}` in the `config_changed` event data. Each component applier's
success/failure is tracked independently so monitoring dashboards can detect partial
hot-reload failures without parsing structured logs.

### EventBus Rate-Tracker Memory Exposure

`EventBus.get_metrics()` returns `rate_tracker_stats` with `tracked_event_type_count`
and `max_tracked_event_types`. When capacity is reached, a warning log fires.
Prometheus exporter exposes `custombot_event_rate_tracker_types_current` and
`custombot_event_rate_tracker_types_max` gauges for operators to detect memory leaks
from unknown event name accumulation.

### HandleMessageMiddleware Send-Ownership (Architecture Decision)

`HandleMessageMiddleware` owns the send responsibility directly — calls
`channel.send_and_track()` after `bot.handle_message()` returns. This is intentionally
separate from `Bot._deliver_response()` which handles persistence, content filtering,
and generation-conflict resolution. The middleware path is the fast-path for already-
formed responses. Documented in the class docstring to prevent accidental path merging.

### `correlation_id_scope()` Context Manager

`correlation_id_scope(correlation_id)` context manager in `src/bot/_bot.py` — wraps
`set_correlation_id()` on entry and `clear_correlation_id()` on all exit paths
(including early returns and exceptions). Replaces manual clear calls on 6+ paths
in `handle_message()` and `process_scheduled()`. Eliminates risk of leaked context vars.

### Bounded EventBus Internal Counts

`EventBus._emission_counts` and `_handler_invocation_counts` are `LRUDict` with
`max_tracked_event_names` cap. Prevents unbounded growth from third-party plugins
or dynamic event names. Eviction follows LRU policy matching `_rate_trackers` pattern.

### Instance-Level `_safe_mode_lock`

`BaseChannel.__init__` creates `_safe_mode_lock` as instance attribute, replacing the
module-level `AsyncLock`. Multiple channel instances (e.g. in tests) no longer contend
on the same lock. Module has no mutable state at import time.

### Cached `tool_definitions` with Invalidation

`SkillRegistry.tool_definitions` property caches the JSON schema list. Cache invalidated
only when skills are added/removed via `load_builtins()` or `load_user_skills()`.
Eliminates per-message rebuild for high-volume chats.

### Pooled xxHash Instances

`DeduplicationService.outbound_key()` reuses a single `xxhash.xxh64()` hasher via
`reset()` instead of creating a new instance per call. Reduces object allocation
overhead during burst outbound dedup operations.

### Single-Hash Outbound Check+Record

`check_and_record_outbound(chat_id, text)` computes `outbound_key()` once for the
combined check-then-record operation. Replaces the double-hash two-phase API
(`check_outbound_duplicate()` + `record_outbound()`) at remaining call sites.

### `process_scheduled()` Timeout

`Bot.process_scheduled()` wraps inner processing in `asyncio.wait_for(per_chat_timeout)`.
A stuck LLM call in a scheduled task no longer blocks all subsequent messages to that
chat indefinitely. Matches the timeout pattern used in `_handle_message_inner()`.

### `_safe_call` BaseException Hardening

`EventBus._safe_call()` catches `BaseException` (not just `Exception`). Re-raises only
`KeyboardInterrupt` and `CancelledError`; logs all others (SystemExit, GeneratorExit)
as non-critical. Prevents subscriber crashes from propagating through `asyncio.gather`.

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
- **mypy** strict for `src.bot.*`; check_untyped_defs elsewhere
- **pytest** with asyncio_mode="auto", pytest-timeout, hypothesis
- **make test-categories** — layer-level test failure identification (unit/integration/e2e separately)
- **Dataclasses** for config/data models; frozen=True for immutable state
- **Protocol-based structural subtyping** — avoid inheritance for interfaces
- **AsyncLock** for all file I/O + **Circuit breaker** for external calls
- **Per-domain constants** in `src/constants/` (14 modules) — no magic numbers
- **Shared validation** in `src/utils/validation.py` — no duplicate checks
- **Swap-buffers flush** (FlushManager) + **Cross-operation retry budget** (`_guarded_write`)
- **WAL mode** on all SQLite connections at creation time
- **Msgpack+Base64** queue serialization with JSON read fallback

---

## Security Requirements

- Path traversal protection (`..` blocked in file skills)
- Shell command denylist/allowlist + Prompt injection detection (`src/security/`)
- Config file permission checks (chmod 600 warning) + URL sanitization for logging
- HMAC signing for scheduled task prompts + Input validation (`src/utils/validation.py`)
- Response content filtering + Bounded error responses (`format_skill_error` length cap) + never expose raw technical error text
- Structured config diff logging for audit trailing
- Request deduplication for concurrent LLM calls (content-hash within per-chat lock)
- WAL journal mode enforced on all SQLite connections (vector_memory + sqlite_pool)
- Raw payload cap (`MAX_RAW_PAYLOAD_SIZE` 64 KB) strips oversized `IncomingMessage.raw` at boundary
- HMAC verification failure emits `error_occurred` event to event bus for security alerting
- Instruction file size cap (`MAX_INSTRUCTION_FILE_SIZE` 1 MiB) enforced by InstructionLoader before reading
- High-confidence injection detection (>=0.8) blocks scheduled task execution outright with audit log + event emission

---

## 📂 Codebase References

**Entry Point**: `main.py` — Click CLI with start/options/diagnose commands
**Application**: `src/app.py` — Lifecycle state machine, startup/shutdown
**Config**: `src/config/config_schema_defs.py` — All dataclass definitions
**Build Config**: `pyproject.toml` — Ruff, mypy, pytest settings

## Related Files

- Navigation: `navigation.md`
- Core Standards: `../core/standards/code-quality.md`
- Development Context: `../development/navigation.md`
