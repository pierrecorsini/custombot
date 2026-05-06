# PLAN.md — Improvement Tasks

Senior codebase review (2026-05-06). These are NEW tasks beyond the 28 remaining
Round 11/12 items tracked in `.opencode/context/project/lookup/plan-progress.md`.

---

## Architecture & Refactoring

- [x] Extract `HealthCheckRegistry` — centralize all health checks (DB, vector_memory, LLM, scheduler) into a discoverable registry with standardized `HealthCheckResult` signatures, replacing the ad-hoc `validate_connection()` / `get_llm_status()` / `get_dedup_stats()` scattered accessors on `Bot` and `Database`
- [ ] Implement NullObject `MemoryMonitor` — replace the `None` + try/except `ImportError` pattern in `Bot.start_memory_monitoring()` with a `NullMemoryMonitor` that satisfies the `MemoryMonitor` protocol, eliminating downstream None-checks and simplifying test fixtures
- [ ] Add structured logging filter — create a `logging.Filter` that auto-injects `correlation_id`, `chat_id`, `app_phase`, and `session_id` into every `LogRecord`, eliminating the manual `extra={"chat_id": ..., "message_id": ...}` dict construction repeated across 50+ log call sites
- [ ] Refactor `IncomingMessage` boundary validation into `MessageValidator` — extract the 6 standalone `_validate_*()` functions from `channels/base.py` into a cohesive `MessageValidator` class with a single `validate(raw: dict) -> IncomingMessage` entry point, reducing the 90-line `__post_init__` validation surface

## Performance Optimization

- [ ] Add connection pooling to vector memory embedding HTTP calls — audit `src/vector_memory/` for per-request `httpx.AsyncClient` instantiation and replace with a shared, long-lived client with connection pooling and configurable concurrency limits
- [ ] Add TTL-based eviction for `LRULockCache` — the per-chat lock cache has max-size eviction but no time-based eviction; transient group chats accumulate stale `asyncio.Lock` objects indefinitely — add configurable TTL to reclaim idle locks
- [ ] Implement per-skill circuit breaker — wrap skill execution in `ToolExecutor` with a per-skill-name `CircuitBreaker` so that a broken or hanging skill (e.g., external API down) doesn't consume all ReAct loop iterations; the LLM client already has one, but skills don't
- [ ] Add EventBus backpressure on `emit()` — `asyncio.gather` with unlimited fan-out means a single event with many subscribers creates unbounded concurrent coroutines; add a bounded semaphore to cap concurrent handler invocations per emission

## Error Handling & Resilience

- [ ] Add cross-operation retry budget to `_guarded_write` — track cumulative retry delay across all active DB write operations so that multiple concurrent writes to a degraded filesystem don't each independently retry and amplify I/O pressure
- [ ] Add message queue persistence integrity checks — the msgpack-based `MessageQueue` persistence has no corruption detection on load; append CRC32 checksums to persisted payloads and verify on recovery to detect truncated/corrupted queue files from unclean shutdowns
- [ ] Bound `format_skill_error()` response length — `correlation_id` and `skill_name` are user-controlled inputs that currently pass through unbounded; cap total error response length to prevent unexpectedly large messages reaching the channel
- [ ] Add structured config diff logging during hot-reload — `ConfigChangeApplier.apply()` logs individual field changes but produces no structured summary; emit a `config_changed` event with the full diff dict for audit trailing and debugging

## Security Hardening

- [ ] Add request deduplication for concurrent LLM calls in same chat — the per-chat lock serializes processing, but if the same user double-sends with slightly different text (or a scheduled task fires while a message is processing), the dedup won't catch it; add a short-window content-hash dedup within the lock scope
- [ ] Verify WAL mode is enabled on all SQLite connections — `src/vector_memory/` and `src/db/sqlite_pool.py` both open SQLite databases; ensure `PRAGMA journal_mode=WAL` is set at connection creation time for all paths, not just the main DB
- [ ] Sanitize error responses to prevent information leakage — `format_skill_error()` includes `error_type` and `skill_name` directly in user-facing output; review all error paths to ensure internal exception class names and module paths never reach end users

## Test Coverage & Quality

- [ ] Add cache hit/miss ratio assertions for `RoutingEngine._match_cache` — the TTL-bounded LRU match cache has no test coverage for cache hit/miss behavior, eviction, or TTL expiry; add targeted tests to verify cache effectiveness
- [ ] Create `BaseChannelTestMixin` — shared test harness that exercises the `BaseChannel` contract (safe-mode confirmation, `send_and_track`, `mark_connected`/`wait_connected`, media NotImplementedError) so all current and future channel implementations are tested consistently
- [ ] Add stress test for `Database._guarded_write` retry budget exhaustion — simulate sustained `OSError` to verify the circuit breaker opens correctly and the retry budget is respected across concurrent operations
- [ ] Add integration test for message queue persistence corruption recovery — write a corrupted msgpack file, start the bot, and verify the queue recovers gracefully (skips corrupt entries, logs warnings, proceeds)
- [ ] Add Hypothesis property-based tests for `IncomingMessage` validation boundary conditions — generate adversarial `chat_id`, `sender_name`, `correlation_id`, and `timestamp` values to verify the validation layer rejects injection attempts without raising unexpected exceptions

## Developer Experience & Code Hygiene

- [ ] Add `make health` Makefile target — runs `ruff check`, `mypy`, `pytest --co`, and `pip-audit` in sequence to give a single-command pre-push health check
- [ ] Add `--version` flag to CLI — output the version from `src/__version__.py` and exit, following standard CLI conventions
- [ ] Add `make lint-fix` Makefile target — runs `ruff check --fix` followed by `ruff format` for one-command auto-formatting
- [ ] Add `make typecheck-strict` Makefile target — runs `mypy --strict src/` to preview full strict type-check results (non-blocking, informational)

## Observability & Monitoring

- [ ] Add `RoutingEngine` cache hit/miss counters to `PerformanceMetrics` — expose match-cache effectiveness via the health endpoint so cache sizing can be tuned without code changes
- [ ] Add EventBus emission rate tracking and event-storm detection — track per-event-type emission rates over sliding windows and log a warning when any event type exceeds a configurable threshold (e.g., 100 emissions/minute)
- [ ] Add per-component health status aggregation — combine DB write breaker, LLM circuit breaker, vector memory health, and scheduler status into a single `/health` response with an overall `healthy: bool` verdict and individual component states
- [ ] Add startup phase timing breakdown to health endpoint — expose per-component startup durations (already collected in `component_durations`) as a structured `/health/startup` sub-endpoint for performance regression monitoring
