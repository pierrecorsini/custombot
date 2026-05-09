<!-- Context: project-intelligence/lookup/decisions-log | Priority: high | Version: 5.0 | Updated: 2026-05-08 -->

# Decisions Log

> Record major architectural and business decisions with full context. Prevents "why was this done?" debates.

## Decision Template

```markdown
## [Title]
Date: YYYY-MM-DD | Status: [Decided/Pending/Under Review/Deprecated]

Context: [What prompted this decision?]
Decision: [What was decided?]
Rationale: [Why this choice?]
Alternatives: [What was rejected and why?]
Impact: [What this enables, trades off, or risks]
Related: [Links to PRs, issues, docs]
```

---

## Decision: Native Python via neonize

**Date**: 2026-03 | **Status**: Decided
**Decision**: Use neonize (ctypes bindings to whatsmeow/Go) for WhatsApp. Eliminates Node.js dependency, HTTP bridge latency.
**Alternatives**: whatsapp-web.js + bridge (rejected: complexity), Baileys HTTP (rejected: latency).
**Risk**: neonize API changes require code updates.

---

## Decision: SQLite + Per-Chat Workspaces + .workspace/

**Date**: 2026-03 | **Status**: Decided
**Decision**: SQLite via aiosqlite for all storage. Each chat gets `workspace/<chat_id>/`. All runtime files in `.workspace/`.
**Rationale**: Embedded, zero-config, single-instance. Clean isolation. Code vs data separation.

---

## Decision: Media Output via Callback Injection

**Date**: 2026-04-12 | **Status**: Decided
**Decision**: Thread `send_media` callback from channel → bot → ToolExecutor → skill. No breaking changes.
**Alternatives**: Return media path (rejected: changes return type), Direct channel access (rejected: breaks layering).
**Libraries**: edge-tts (TTS), xhtml2pdf (PDF), markdown (HTML).

---

## Decision: Config Split + ShutdownContext + Bot Decomposition

**Date**: 2026-05-02 to 2026-05-04 | **Status**: Decided
**Decisions**: (1) Split `config.py` (785 lines) into schema/loader/validation. (2) `ShutdownContext` frozen dataclass replaces 12 positional params. (3) Extract `context_building.py`, `response_delivery.py` from `_bot.py`; `message_queue_buffer.py` from `message_queue.py`.
**Rationale**: Single responsibility per module. Named params prevent ordering bugs. Each sub-module owns one concern.

---

## Decision: HealthCheckRegistry + NullMemoryMonitor + StructuredContextFilter + MessageValidator

**Date**: 2026-05-06 | **Status**: Decided
**Decisions**: (1) `HealthCheckRegistry` centralizes health checks with standardized signatures. (2) `NullMemoryMonitor` NullObject eliminates None-guards. (3) `StructuredContextFilter` auto-injects context vars into LogRecords. (4) `MessageValidator` class replaces 6 standalone `_validate_*()` functions.
**Files**: `src/health/registry.py`, `src/monitoring/memory.py`, `src/logging/logging_config.py`, `src/channels/message_validator.py`

---

## Decision: Performance Resilience Batch (4 items)

**Date**: 2026-05-06 | **Status**: Decided
**Decisions**: (1) Shared `httpx.AsyncClient` with connection pooling, (2) TTL-based eviction on `BoundedOrderedDict`, (3) Per-skill circuit breakers in `ToolExecutor`, (4) Bounded semaphore on `EventBus.emit()`.
**Files**: `src/builder.py`, `src/utils/`, `src/core/tool_executor.py`, `src/core/event_bus.py`

---

## Decision: Resilience Batch — Request Dedup + WAL + Retry Budget + Unified Dedup

**Date**: 2026-05-07 | **Status**: Decided
**Context**: Dedup was scattered; WAL mode inconsistent across SQLite connections; retry storms under I/O degradation.
**Decisions**: (1) Short-window content-hash dedup within per-chat lock scope. (2) `PRAGMA journal_mode=WAL` enforced at connection creation. (3) Cross-operation retry budget caps cumulative delay across concurrent DB writes. (4) Consolidate inbound, outbound, and request dedup into unified `DeduplicationService`.
**Files**: `src/core/dedup.py`, `src/constants/messaging.py`, `src/vector_memory/__init__.py`, `src/db/sqlite_pool.py`, `src/db/db.py`

---

## Decision: Round 15 Architecture — SkillBreakerRegistry + RegistryBackedMixin + Event Validation + Bot Factory

**Date**: 2026-05-07 | **Status**: Decided
**Context**: Per-skill breaker dict unbounded; duplicated attribute forwarding in StartupContext/BuilderContext; silent event typos; _step_bot wiring untestable.
**Decisions**: (1) `SkillBreakerRegistry` with max_skills cap and LRU eviction replaces raw dict. (2) `RegistryBackedMixin` consolidates `__getattr__`/`__setattr__` delegation. (3) Opt-in strict event name validation on `EventBus.emit()`. (4) `_build_bot_deps()` factory extracted from `_step_bot`.
**Files**: `src/core/skill_breaker_registry.py`, `src/utils/registry.py`, `src/core/event_bus.py`, `src/builder.py`, `src/core/tool_executor.py`, `src/core/startup.py`

---

## Decision: Round 15 Observability — Per-Chat Latency + Error-Rate Alerting + CRC32 Queue Guards + Coalesced Flush

**Date**: 2026-05-07 | **Status**: Decided
**Context**: No per-chat latency visibility; error trends not alerted; queue persistence had no corruption detection; burst chat saves caused redundant I/O.
**Decisions**: (1) Bounded per-chat latency tracker with percentile computation. (2) Configurable error-rate alert thresholds with cooldown. (3) CRC32 checksum guard on queue file lines (3-tier decode: CRC-guarded → legacy msgpack → JSON). (4) Coalesced debounce flush for chat saves.
**Files**: `src/monitoring/performance.py`, `src/monitoring/metrics_types.py`, `src/message_queue_persistence.py`, `src/db/db.py`, `src/health/prometheus.py`

---

## Decision: Round 14 Resilience + Security Hardening

**Date**: 2026-05-07 | **Status**: Decided
**Context**: Persistence failure during message prep created inconsistent state; group chat rate-limit noise; env var misconfiguration disabling rate limits; unvalidated sender_name length.
**Decisions**: (1) `_prepare_turn()` fails fast on persistence failure — aborts turn to maintain conversation state consistency. (2) Suppress outbound rate-limit notifications in group chats (log-only when `msg.toMe`). (3) `RATE_LIMIT_EFFECTIVE_MAX` secondary cap with loud warning on override. (4) `MAX_SENDER_NAME_LENGTH` constant with truncation + warning. (5) `sqlcipher`-compatible `connection_factory` parameter on SQLite pool for at-rest encryption.
**Files**: `src/bot/_bot.py`, `src/rate_limiter.py`, `src/channels/message_validator.py`, `src/db/sqlite_pool.py`

---

## Decision: Round 14 Observability + Performance

**Date**: 2026-05-07 | **Status**: Decided
**Context**: Retry storms invisible in traces; dedup effectiveness not externally visible; JSON depth validation used recursive approach; skill limiters unbounded dict.
**Decisions**: (1) OTel span per retry iteration in `_guarded_write` with attempt/delay/budget attributes. (2) `build_dedup_prometheus_output()` exposes dedup hit/miss counts via `/metrics`. (3) Iterative BFS replaces recursive `_measured_depth()` for JSON depth validation. (4) LRU eviction on `_skill_limiters` matching existing chat-limiter pattern.
**Files**: `src/db/db.py`, `src/health/prometheus.py`, `src/core/tool_executor.py`, `src/rate_limiter.py`

---

## Decision: Round 15 Resilience + Safety

**Date**: 2026-05-07 | **Status**: Decided
**Context**: CancelledError left queue entries PENDING; crash between snapshot and changelog unlink caused data duplication; single bad config field blocked all hot-reload changes; HTTP probes assumed JSON responses.
**Decisions**: (1) `finally` guard in `_handle_message_inner` completes queue entry on any exit path. (2) Compaction marker file written after snapshot; checked during `_replay_changelog` to skip stale entries. (3) Per-field try/except in `ConfigWatcher._apply_safe_changes` — bad field doesn't block others. (4) `Content-Type` validation on HTTP probe responses before JSON parse. (5) Lazy-init `EventBus._emit_semaphore` to first `emit()` call (avoids event-loop timing issues).
**Files**: `src/bot/_bot.py`, `src/db/db.py`, `src/config/config_watcher.py`, `src/health/checks.py`, `src/core/event_bus.py`

---

## Decision: Round 15 Performance — Shared HTTP Clients + Batch Flush

**Date**: 2026-05-07 | **Status**: Decided
**Context**: Embedding and LLM endpoints on same host opened separate connection pools; outbound buffer flush computed hashes sequentially.
**Decisions**: (1) Pool `AsyncOpenAI` clients — share `httpx.AsyncClient` when `embedding_base_url` matches `llm.base_url`. (2) Hash precomputation + batch set for outbound buffer flush. (3) Event.data optional validation warns on non-JSON-serializable types.
**Files**: `src/builder.py`, `src/core/dedup.py`, `src/core/event_bus.py`

---

## Decision: Round 18 — NullDedupService + Scoped Correlation + Bounded State + Performance Caching

**Date**: 2026-05-08 | **Status**: Decided
**Context**: Bot._dedup was Optional forcing 8+ None-guards; correlation_id clear on 6+ early-return paths risked leaks; EventBus internal dicts unbounded; module-level safe-mode lock shared across channel instances; tool definitions rebuilt per message.
**Decisions**: (1) `NullDedupService` — no-op dedup following NullMemoryMonitor pattern. (2) `correlation_id_scope()` context manager for lifecycle safety. (3) `max_tracked_event_names` LRU cap on emission/handler counts. (4) Instance-level `_safe_mode_lock` in BaseChannel. (5) Cached `tool_definitions` with invalidation on skill load. (6) Pooled xxhash hasher instances via `reset()`. (7) `check_and_record_outbound()` single-hash variant. (8) `process_scheduled()` timeout matching per_chat_timeout. (9) `_safe_call` BaseException hardening.
**Files**: `src/core/dedup.py`, `src/bot/_bot.py`, `src/core/event_bus.py`, `src/channels/base.py`, `src/skills/base.py`, `src/core/tool_executor.py`

---

## Deprecated Decisions

| Decision | Date | Replaced By | Why |
|----------|------|-------------|-----|

## Decision: Round 16 Resilience + Security + Observability

**Date**: 2026-05-07 | **Status**: Decided
**Context**: `is_outbound_duplicate()` lacked runtime deprecation; `validate_connection()` blocked event loop during startup; untracked flush futures caused "Task exception was never retrieved" warnings; rate-limit rejection was observability-silent; `send_and_track` recorded dedup even on send failure; scheduler had no `BaseException` guard; raw channel payloads uncapped; HMAC verification failure not on event bus.
**Decisions**: (1) `DeprecationWarning` on `is_outbound_duplicate()` directing to two-phase API. (2) `validate_connection()` delegates sync I/O to `asyncio.to_thread()`. (3) `_start_tracked_flush()` stores future + done-callback. (4) `message_dropped` event on rate-limit. (5) `send_and_track` returns early on send failure. (6) `BaseException` catch in `_run_loop`. (7) `MAX_RAW_PAYLOAD_SIZE` (64 KB) cap on raw payloads. (8) `error_occurred` event on HMAC verification failure.
**Files**: `src/core/dedup.py`, `src/db/db.py`, `src/bot/_bot.py`, `src/channels/base.py`, `src/channels/message_validator.py`, `src/scheduler/engine.py`, `src/constants/security.py`

## Codebase References

- `src/bot/` — Bot sub-modules (context_building, response_delivery, react_loop)
- `src/message_queue.py` + `message_queue_buffer.py` — Queue decomposition
- `channels/whatsapp.py` — neonize integration
- `.workspace/` — Runtime file centralization

## Related Files

- `concepts/architecture.md` — How decisions shape the architecture
- `concepts/business-tech-bridge.md` — Business-technical trade-offs
- `errors/known-issues.md` — Open questions that may become decisions
