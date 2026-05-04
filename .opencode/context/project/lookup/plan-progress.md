<!-- Context: project/lookup/plan-progress | Priority: high | Version: 13.0 | Updated: 2026-05-04 -->

# Lookup: PLAN.md Progress Tracker

**Purpose**: Quick-reference status of all improvement plan items across 10 rounds
**Source**: `PLAN.md` (308 lines) — Round 10 senior technical review

---

## Summary

| Round | Total | Done | Remaining |
|-------|-------|------|-----------|
| Round 1 | 20 | 20 | 0 |
| Round 2 | 20 | 20 | 0 |
| Round 3 | 20 | 20 | 0 |
| Round 4 | 25 | 25 | 0 |
| Round 5 | 22 | 22 | 0 |
| Round 6 | 25 | 25 | 0 |
| Round 7 | 20 | 20 | 0 |
| Round 8 | 20 | 20 | 0 |
| Round 9 | 20 | 20 | 0 |
| Round 10 | 27 | 27 | 0 |
| **Total** | **219** | **219** | **0** |

---

## Round 10 — ALL COMPLETE (27/27)

*Senior technical review (2026-05-04). Source: `PLAN.md`*

### Architecture & Refactoring (5/5 ✅)

- Extract `Bot._process()` turn-preparation into `_prepare_turn()`
- Replace `log_noncritical()` string category with enum
- Move `src/llm_error_classifier.py` into `src/llm/` package
- Add `__slots__` to `QueuedMessage` dataclass
- Extract `MessagePipeline.execute()` middleware into `MiddlewareChain`

### Performance & Scalability (4/4 ✅)

- Pre-warm `FileHandlePool` for active chats at startup
- Avoid re-serializing tool call arguments in `execute_tool_call()`
- Batch `DeduplicationService.record_outbound()` writes
- Use `msgpack` for `MessageQueue` persistence instead of JSON

### Error Handling & Resilience (4/4 ✅)

- `finally` block in `_step_vector_memory()` closes `embed_http` on any exception
- `generation_conflict` event emitted when `_deliver_response()` encounters write conflict
- `OSError/DatabaseError` handled gracefully in `_deliver_response()` during `save_messages_batch()`
- `EVENT_STARTUP_COMPLETED` emitted via EventBus after Application startup completes

### Testing & Quality (5/5 ✅)

- Test for `Bot._send_to_chat()` with/without channel
- Test for `Application._swap_config()` atomicity guarantee
- Test for `_step_vector_memory()` with dedicated embedding URL and probe failure
- Property-based test for `outbound_key()` hash consistency
- Integration test for full `_on_message` → pipeline → `_handle_message_inner` timeout path

### Security (3/3 ✅)

- Sanitize `sender_name` in validation layer
- `Content-Length` header validation added to HealthServer
- Validate `ToolLogEntry.name` length before audit log write

### DevOps & CI (5/5 ✅)

- Add `Ruff` `PERF` ruleset to lint config (non-blocking)
- Add `pip-audit` SARIF output upload to GitHub Security tab
- Pin `ruff==0.15.12` in `pyproject.toml` dev dependencies
- Add `pytest-timeout` to dev dependencies and CI (`--timeout=120`)
- Add CI step to validate `PLAN.md` checkbox syntax

---

## Rounds 4-9 Completed (132/132)

### Round 4 (25/25) — 2026-05-02
- Config split (785→3 modules), ShutdownContext, `build_bot()` public, __all__ exports
- Concurrency semaphore, executor shutdown, embedding change detection, connection pooling
- _from_dict error raising, TOCTOU-safe seeding, scheduler mutation guard
- Config.__repr__ redaction, Dockerfile supply-chain pinning, IncomingMessage validation
- pip-compile generation, pre-commit ruff, mypy --strict, neonize/sqlite-vec pinning

### Round 5 (22/22) — 2026-05-02
- `LLMProvider/Bot/ContextAssembler.update_config()` public methods
- `_deliver_response()` extracted from monolithic handle_message()
- Memory chat dir caching, single-pass hash dedup, streaming JSONL queue
- HealthServer localhost-only + rate limiting, atomic config swap
- `.env.example` (14 vars), pip-compile CI sync

### Round 6 (25/25) — 2026-05-02
- `BotDeps` dataclass (15→1 param), async recovery logging, ContextAssembler.update_config()
- Per-chat timeout, BoundedOrderedDict TTL dedup, scheduler cache, batched JSONL migration
- finish_reason="length" handling, structured error categorization, vector memory degradation fix
- Zero-rule graceful degradation, symlink rejection in routing
- HMAC signing test, recovery event test, TokenUsage leaderboard, concurrent workspace test
- .gitattributes, ruff PL ruleset, pytest-xdist parallel

### Round 7 (20/20) — 2026-05-03
- RoutingEngine.close() in shutdown, `_send_to_chat()` helper, generation-conflict fix
- Swap-buffers MessageQueue flush, reverse index TokenUsage leaderboard, routing short-circuit
- `message_dropped` events, CancelledError handling, sender_name validation
- Instruction loader path traversal validation, context var reset, health path validation
- `.gitattributes` eol=lf, ruff PL ruleset, pytest-xdist, benchmark regression test
- RoutingEngine watchdog test, ContextAssembler degradation test, concurrent queue flush test

### Round 8 (20/20) — 2026-05-03
- ErrorHandlerMiddleware `_send_error_reply()` helper
- `ReactIterationContext` dataclass (18→6 params), unified `_target_utc_time()`
- Lazy ToolExecutor audit logger, avoid list() copy in MessageQueue
- Pre-computed MatchingContext + cache key in `_build_turn_context()`
- Disk-full handling in MessageQueue flush, structured shutdown timeout logging
- ConfigChangeApplier destructive field preservation test, content_filter finish_reason test
- Correlation_id format validation, tool name sanitization, HSTS header
- ruff TCH strict, mypy --strict for src/bot/, routing latency benchmark

### Round 9 (20/20) — 2026-05-04
- RoutingEngine non-blocking (async retry), VectorMemory decoupled from LLMClient internals
- Parallel shutdown pre-steps, `to_shutdown_context()` factory method
- Inbound LRU cache for dedup, cached last_run datetimes, orjson for scheduler writes
- Timeout queue completion fix, fail-open dedup on DB errors, atomic task file writes, stdin timeout
- `_classify_main_loop_error` test, timeout path queue test, hot-reload denylist test, _transition rollback test
- Retry sleep cap in RoutingEngine, task validation in TaskScheduler._load()
- config.example.json CI sync, Docker BuildKit layer caching, coverage regression gate

### Round 10 (27/27) — 2026-05-04
- `_prepare_turn()` extracted from `_process()` (turn preparation vs ReAct orchestration)
- `NonCriticalCategory` made pure Enum (type-safe, exhaustive enforcement)
- LLM subsystem moved to `src/llm/` package (backward-compatible re-exports)
- `__slots__` added to `QueuedMessage` dataclass
- `MessagePipeline.execute()` middleware unwinding → reusable `MiddlewareChain`
- FileHandlePool pre-warmed for active chats at startup
- Tool call arguments stored as raw JSON (lazy parsing in ToolLogEntry)
- Outbound dedup batched during burst delivery
- msgpack persistence for MessageQueue (JSON fallback for crash recovery)
- `embed_http.aclose()` in `finally` block (any exception path)
- `generation_conflict` event emitted on write conflict in `_deliver_response()`
- `OSError/DatabaseError` handled gracefully during `save_messages_batch()`
- `EVENT_STARTUP_COMPLETED` event emitted after Application startup
- Tests for `Bot._send_to_chat()` with/without channel
- Tests for `Application._swap_config()` atomicity guarantee
- Test for `_step_vector_memory()` dedicated URL + probe failure
- Property-based test for `outbound_key()` hash consistency
- Integration test for full `_on_message` → pipeline → timeout path
- `IncomingMessage.sender_name` sanitized in validation layer
- `Content-Length` header validation added to HealthServer
- `ToolLogEntry.name` length validated before audit log write
- Ruff PERF ruleset added to lint config (non-blocking)
- pip-audit SARIF output uploaded to GitHub Security tab
- ruff==0.15.12 pinned in pyproject.toml (local+CI parity)
- pytest-timeout added to dev deps + CI (120s limit)
- `scripts/check_plan_syntax.py` validates PLAN.md checkbox format in CI

---

## Codebase

- `PLAN.md` — Full improvement plan (source of truth for checkboxes)

## Harvested From

- Session snapshots (3 files) — 2026-05-04
- Source code changes (10 commits) — 2026-05-04
- Source code changes (20 commits) — 2026-05-04
- PLAN.md full sync (all 219/219 complete) — 2026-05-04

## Related

- `lookup/improvement-roadmap.md` — 10 task category objectives and status
- `lookup/implemented-modules.md` — What modules already exist
