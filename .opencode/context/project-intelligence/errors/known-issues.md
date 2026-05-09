<!-- Context: project-intelligence/errors/known-issues | Priority: high | Version: 9.0 | Updated: 2026-05-07 -->

# Known Issues

> Active technical debt, open questions, and current issues. Review weekly.

## Technical Debt (from PLAN.md — 10 items remaining)

### Security (2 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Block high-confidence injection in `process_scheduled` | Scheduled tasks with high-confidence injection still reach LLM | High | Reject `confidence >= 0.8`, emit audit event |
| `InstructionLoader` file-size cap | Huge instruction files could exhaust memory | Medium | `MAX_INSTRUCTION_FILE_SIZE` 1 MiB limit |

### Testing (4 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| `HandleMessageMiddleware` outbound test | Verifies uniform dedup + event tracking | Medium | Test send path and `None` return path |
| `_compact_chats` marker atomicity test | Verifies crash-safe marker writes | Medium | Corrupted marker → verify atomic recovery |
| Error-reply rate limiting test | Verifies per-chat suppression | Medium | Rapid errors → verify suppression |
| `SlidingWindowTracker` Hypothesis tests | Edge-case coverage for rate calculations | Low | Adversarial timestamp sequences |

### Developer Experience (2 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| `make benchmark` target | Performance regression visibility | Low | Run `bench_*.py` with summary |
| Document `HandleMessageMiddleware` design | Architectural boundary clarity | Low | Docstring explaining send responsibility |

### Observability (2 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Rate-tracker memory usage in `get_metrics()` | Detect memory leaks from unknown events | Medium | `tracked_event_type_count` field |
| Config hot-reload per-component outcome log | Detect partial hot-reload failures | Medium | Per-applier status in event data |

## Insights & Lessons Learned

### What Works Well
- Message starvation detection catches silent WhatsApp disconnections
- Normalizing at channel boundary prevents unit mismatch propagation

### What Could Be Better
- Format conversion helpers should be integration-tested end-to-end
- External library field coverage should be verified against real client behavior

### Gotchas for Maintainers
- WhatsApp timestamps are milliseconds, not seconds — always normalize at the boundary
- neonize library may not populate all WhatsApp protocol fields — verify PTT fields separately

## Archive (Resolved Items)

### Resolved: Round 17 Architecture + Performance + Resilience + Security + Testing (12 items) (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: 12 PLAN.md Round 17 items completed:
  - Architecture: `HandleMessageMiddleware` uses `send_and_track()`, bounded `_rate_trackers` with LRU, `_write_marker()` helper extracted
  - Performance: Double JSON-encode eliminated in `_validate_data_serializable`, empty-container skip in `_measured_depth` BFS
  - Resilience: `_atomic_write` for compaction marker, `per_chat_timeout` error event, error-reply rate limiting in `ErrorHandlerMiddleware`
  - Security: ACL rejection `message_dropped` event with `reason="acl_rejected"`
  - Testing: `_run_with_retry` category-transition test, `send_and_track` send-failure test, config hot-reload semaphore test
- **Files**: `src/core/message_pipeline.py`, `src/core/event_bus.py`, `src/db/db.py`, `src/bot/_bot.py`

### Resolved: Round 16 Architecture + Performance + Resilience + Security (11 items) (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: 11 PLAN.md Round 16 items completed:
  - Architecture: DeprecationWarning on `is_outbound_duplicate()`, `validate_connection()` off event loop, tracked flush futures, `message_dropped` event, `send_and_track` guard, `BaseException` guard in scheduler
  - Security: `MAX_RAW_PAYLOAD_SIZE` 64 KB cap on raw payload, HMAC verification `error_occurred` event
  - Also completed: ToolExecutor skill-breaker states in health, Database changelog stats in health, MessageQueue flush latency tracking
- **Files**: `src/core/dedup.py`, `src/db/db.py`, `src/bot/_bot.py`, `src/channels/base.py`, `src/channels/message_validator.py`, `src/scheduler/engine.py`, `src/constants/security.py`, `src/health/prometheus.py`

### Resolved: Round 14 Technical Debt — Testing & Quality (3/3) + Developer Experience (4/4) + Observability (4/4) (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: All 11 remaining PLAN.md Technical Debt items completed:
  - Testing: PerformanceMetrics lifecycle test, VectorMemory cache eviction test, mixed error category transition test
  - DX: `make test-quick`, mypy strict for `src.core.*`, `make coverage-push`, BotDeps docstring
  - Observability: OTel span for `_guarded_write` retries, DedupService Prometheus output, per-chat latency percentiles, error-rate alerting thresholds
- **Files**: `src/monitoring/performance.py`, `src/health/prometheus.py`, `src/db/db.py`, `src/bot/_bot.py`, `Makefile`

### Resolved: Round 14 Performance + Resilience + Security (12 items) (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: 12 PLAN.md Round 14 items completed:
  - Performance: Batch inbound dedup lookups, iterative BFS for `_measured_depth()`, sqlcipher-compatible `connection_factory`
  - Resilience: Fail fast on persistence failure in `_prepare_turn()`, retry budget recovery Prometheus gauge, suppress group-chat rate-limit responses
  - Security: Skills declare `expensive: bool` on BaseSkill, `RATE_LIMIT_EFFECTIVE_MAX` advisory ceiling, `sender_name` length cap with `MAX_SENDER_NAME_LENGTH`
  - Testing: Semaphore N+1 concurrency test, retry budget recovery test, ConfigWatcher multi-component reconfiguration test
- **Files**: `src/core/dedup.py`, `src/core/tool_executor.py`, `src/db/sqlite_pool.py`, `src/db/sqlite_utils.py`, `src/bot/_bot.py`, `src/health/prometheus.py`, `src/skills/base.py`, `src/rate_limiter.py`, `src/constants/security.py`, `src/channels/message_validator.py`

### Resolved: Round 14 Architecture (5/5) + Performance (1/4) (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: 6 PLAN.md items completed: (1) Lift RateLimiter/ToolExecutor/ContextAssembler into BotDeps, (2) Deduplicate `_RETRYABLE_LLM_ERROR_CODES` to error classifier, (3) Per-check timeout in `HealthCheckRegistry.run_all()`, (4) LRU eviction for `RateLimiter._skill_limiters`, (5) Reset `_retry_budget_spent` on circuit breaker recovery, (6) Incremental chats persistence (O(dirty) changelog with periodic compaction)
- **Files**: `src/bot/_bot.py`, `src/builder.py`, `src/llm/_error_classifier.py`, `src/health/registry.py`, `src/rate_limiter.py`, `src/db/db.py`

### Resolved: Performance & Resilience Batch (2026-05-07)
- **Resolved**: 2026-05-07
- **Resolution**: 8 PLAN.md items completed: (1) Request dedup for concurrent LLM calls, (2) WAL mode on all SQLite connections, (3) Structured config diff logging, (4) `format_skill_error()` response length bounding, (5) Cross-operation retry budget for `_guarded_write`, (6) EventBus backpressure via bounded semaphore, (7) Per-skill circuit breaker in ToolExecutor, (8) Embedding HTTP connection pool limits configurable
- **Files**: `src/core/dedup.py`, `src/core/event_bus.py`, `src/core/tool_executor.py`, `src/vector_memory/__init__.py`, `src/db/db.py`, `src/constants/messaging.py`

### Resolved: StructuredContextFilter + MessageValidator (2026-05-06)
- **Resolved**: 2026-05-06
- **Resolution**: Two PLAN.md Architecture items completed: (1) `StructuredContextFilter` auto-injects correlation_id, chat_id, app_phase, session_id into every LogRecord via `logging.Filter`, (2) `MessageValidator` extracted from `channels/base.py` into cohesive class with single `validate(raw: dict) -> IncomingMessage` entry point
- **Files**: `src/logging/logging_config.py`, `src/channels/message_validator.py`

### Resolved: HealthCheckRegistry + NullMemoryMonitor (2026-05-06)
- **Resolved**: 2026-05-06
- **Resolution**: Two PLAN.md Architecture items completed: (1) `HealthCheckRegistry` centralizes all health checks (DB, vector_memory, LLM, scheduler) into discoverable registry with standardized signatures, (2) `NullMemoryMonitor` NullObject eliminates all downstream None-checks
- **Files**: `src/health/registry.py`, `src/health/server.py`, `src/monitoring/memory.py`, `src/bot/_bot.py`

### Resolved: Round 3 Technical Debt (15 items — ALL completed 2026-05-04)
- **Resolved**: 2026-05-04
- **Resolution**: All 15 remaining items from PLAN.md Round 3 completed across Rounds 4-9
- **Items**: Concurrency semaphore, executor shutdown, embedding detection, connection pooling, _from_dict error raising, TOCTOU-safe seeding, scheduler mutation guard, __all__ exports, duplicate test removal, config hot-reload test, property-based config test, shared Bot fixture, Config.__repr__ redaction, IncomingMessage validation, Dockerfile pinning

### Resolved: Round 9 Technical Debt (11 items — ALL completed 2026-05-04)
- **Resolved**: 2026-05-04
- **Resolution**: All 11 remaining items from PLAN.md Round 9 completed
- **Items**: Atomic file writes in TaskScheduler, stdin read timeout, _classify_main_loop_error test, timeout path queue state test, hot-reload denylist test, Application._transition() rollback test, retry sleep cap in RoutingEngine, task validation in TaskScheduler._load(), config.example.json CI sync, Docker BuildKit caching, coverage regression gate

### Resolved: msgpack queue persistence (2026-05-06)
- **Resolved**: 2026-05-06
- **Resolution**: `message_queue_persistence.py` switched from JSON to msgpack+base64 serialization with JSON read fallback. Also added WAL-protected writes for crash safety.
- **Files**: `src/message_queue_persistence.py`

### Resolved: DB shutdown flush + executor deadlock + JSONL auto-repair (2026-05-06)
- **Resolved**: 2026-05-06
- **Resolution**: Three shutdown/startup bugs fixed: (1) DB `close()` now has sync fallback when executor is shut down, (2) executor shutdown catches "cannot join current thread" deadlock, (3) corrupt JSONL last lines auto-repaired on startup
- **Files**: `src/db/db.py`, `src/lifecycle.py`, `src/workspace_integrity.py`

### Resolved: Diagnostic errors — dependency checker, embeddings, config schema, sender_id (2026-05-06)
- **Resolved**: 2026-05-06
- **Resolution**: Six diagnostic bugs fixed: package name normalization, encoding_format for embeddings, config schema field sync, sender_id AttributeError (212 occurrences), embedding probe for non-OpenAI providers
- **Files**: `src/dependency_check.py`, `src/vector_memory/__init__.py`, `src/config/config_schema_defs.py`, `src/bot/`, `src/diagnose.py`

## Codebase References

- `src/` — Core application modules
- `channels/` — Communication channels
- `src/channels/whatsapp.py` — Timestamp normalization boundary
- `src/channels/neonize_backend.py` — Voice note PTT fields
- `.workspace/logs/` — Log files for issue diagnosis

## Harvested From

- Session snapshots (3 files) — 2026-05-04

## Related Files

- `errors/bug-fixes.md` — Past bugs and fixes (Fixes 8-10)
- `project/lookup/plan-progress.md` — Full PLAN.md progress tracker (Round 10 remaining)
- `concepts/architecture.md` — Technical context for current state
