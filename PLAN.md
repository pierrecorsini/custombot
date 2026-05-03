# PLAN.md — Improvement Plan

_Round 5 — Senior technical review (2026-05-02). All 25 Round 4 items completed._
_Round 4 archive: 25/25 items completed. Rounds 1-3: 45/60 completed._

---

## Architecture & Refactoring

- [x] Consolidate `requirements-dev.txt` into `pyproject.toml` `[project.optional-dependencies.dev]` — the two files have drifted (e.g. `pytest==9.0.3` in txt vs `pytest>=8.0.0` in toml) causing CI/local version mismatches. Remove requirements-dev.txt after migration.
- [x] Reduce `perform_shutdown()` parameter count from 12 positional args to a `ShutdownContext` dataclass — the current signature is a maintenance burden and every new component requires updating all callers.
- [x] Make `_build_bot()` in `src/builder.py` a public API (rename to `build_bot`) — the leading underscore suggests it's private, but it's called across module boundaries from `src/core/startup.py`.
- [x] Extract `src/config/config.py` (785 lines) into separate modules — split dataclass definitions (`config_schema_defs.py`), load/save logic (`config_loader.py`), and validation helpers (`config_validation.py`). The current file mixes data model, I/O, logging, and validation concerns.
- [x] Move the misplaced `from typing import Callable, Awaitable` import on line 29 of `src/llm.py` up to the main imports block for consistency with the rest of the codebase.

## Performance & Scalability

- [x] Add a bounded concurrency semaphore to `Application._on_message()` — under load, unlimited concurrent message processing can exhaust memory and LLM rate limits. A configurable `max_concurrent_messages` semaphore (default 10) would cap resource usage without blocking the event loop.
- [x] Close the `ThreadPoolExecutor` with `wait=True` and a timeout in `lifecycle.py` step 6 — `executor.shutdown(wait=False)` can orphan submitted work (e.g. pending DB writes, vector memory batches) leading to data loss on crash. Use `wait=True` with a short timeout instead.
- [x] Detect embedding model changes across restarts in `VectorMemory` — store the embedding model name in a metadata table on first write, and warn loudly (or offer re-indexing) when the configured model changes, since existing vectors become silently incompatible.
- [x] Add connection pooling abstraction for the three SQLite databases (main `.data/`, `vector_memory.db`, `projects.db`) — each creates its own connection independently. A shared `ConnectionPool` would reduce file handle usage and enable WAL-mode consistency across databases.

## Error Handling & Resilience

- [x] Fix silent error swallowing in `config.py` `_from_dict()` — when `data` is not a dict, it returns `cls()` (default-constructed) with no warning. This masks malformed config sections silently. Log a warning and raise `ConfigurationError` instead.
- [x] Make `_load_pending()` error handling in `message_queue.py` consistent — the method uses `log_errors=True` during normal load but `log_errors=False` during repair. Both paths should use the same logging level to ensure corruption is equally visible.
- [x] Add TOCTOU-safe workspace seeding in `Memory.ensure_workspace()` — the `if not agents_path.exists()` check followed by write is racy across concurrent coroutines. The tmp→rename pattern helps, but the initial exists check should use an atomic `os.O_EXCL` open or a lock.
- [x] Guard against in-place mutation of shared task dicts in `scheduler.py` `_execute_task()` — the method mutates `task["last_result"]`, `task["last_run"]` directly on the shared `_tasks` dict while the scheduler loop iterates over it. Snapshot the task or use a copy-on-write pattern.

## Testing & Quality

- [x] Add `__all__` exports to all public modules (currently only `exceptions.py` and `llm.py` define `__all__`) — makes the public API explicit, helps prevent accidental internal imports, and enables `from src.module import *` to work correctly.
- [x] Consolidate duplicate `test_routing.py` — exists in both `tests/` root and `tests/unit/`. This can cause double-discovery and conflicting test results. Remove the root-level file and keep the unit/ version.
- [x] Add integration test for config hot-reload via `ConfigWatcher` — verify that changing a config value on disk triggers the callback with the new value, and that malformed JSON doesn't crash the watcher loop.
- [x] Add property-based test for `_from_dict()` roundtrip in `config.py` — use hypothesis to generate random Config dicts, roundtrip through `_from_dict` → `asdict`, and verify equality. Catches missing field mappings early.
- [x] Add a `conftest.py` fixture for a fully-wired `Bot` instance with mocked LLM, DB, and Memory — currently each test file constructs its own partial mock. A shared fixture reduces duplication and ensures consistent test isolation.

## Security

- [x] Redact secrets in `Config.__repr__()` — while `_redact_secrets()` exists for logging, calling `repr(config)` directly (e.g. in error traces or debugger) leaks the API key via `LLMConfig.__repr__` which shows `api_key='sk-...'`. Override `Config.__repr__` to use redaction.
- [x] Add supply-chain pinning to `Dockerfile` — pin the base image by digest (`python:3.11.12-slim-bookworm@sha256:...`) instead of just tag, and add `pip install --require-hashes` support for production builds.
- [x] Validate `IncomingMessage` fields before use in `Bot.handle_message()` — currently only `msg.text` is checked for emptiness, but `msg.message_id`, `msg.chat_id`, and `msg.sender_id` are used without validation. Add basic format checks to prevent injection through crafted IDs.

## DevOps & CI

- [x] Add `pyproject.toml` target for `requirements.txt` generation — currently `requirements.txt` duplicates dependencies from `pyproject.toml`. Use `pip-compile` (pip-tools) to generate `requirements.txt` from `pyproject.toml` as the single source of truth.
- [x] Add pre-commit hook to run `ruff check --fix` and `ruff format` — the `.pre-commit-config.yaml` exists but should include ruff for consistent local enforcement matching CI.
- [x] Add `--strict` mode to `mypy` CI step for `src/` (non-blocking initially) — currently `disallow_untyped_defs` is False. Incrementally enabling strict checks on new files would improve type safety without breaking existing code.
- [x] Pin `neonize` and `sqlite-vec` versions in `requirements.txt` and add a `pip-audit` CI step — these native dependencies have frequent breaking changes and aren't covered by Dependabot (which only handles GitHub Actions currently).
- [x] Add smoke test to Dockerfile build in CI — verify the built image can start and respond to `--help` without crashing, catching dependency or import errors before deployment.

---

_Round 5 — Senior technical review (2026-05-02). 22 items across 6 categories._

## Architecture & Refactoring

- [x] Replace `type: ignore[arg-type]` assertions in `Application._build_state_from_ctx()` with proper Optional unwrapping — the method has 8 `type: ignore` comments because `StartupContext` fields are `Optional` but guaranteed populated after successful startup. Add a `_validate_populated()` method to `StartupContext` that narrows types via a TypedDict or returns a non-optional typed object, eliminating all ignore directives.
- [x] Add public `LLMProvider.update_config(new_cfg: LLMConfig)` method — `ConfigChangeApplier._apply_llm_config()` directly sets `self._llm._cfg = new_config.llm` reaching into the private attribute. A public method would encapsulate the update with validation (e.g. temperature bounds, non-empty model name) and allow the LLM client to react to config changes (e.g. adjusting timeout on the httpx client).
- [x] Add public `Bot.update_config(new_cfg: BotConfig)` method — `ConfigChangeApplier._apply_bot_config()` bypasses the frozen `BotConfig` dataclass via `object.__setattr__(self._bot, "_cfg", new_bot_cfg)`. If Bot's constructor adds validation later, this silent mutation would skip it. A public method centralizes config updates and makes the mutation traceable.
- [x] Extract response delivery and post-processing from `Bot.handle_message()` into `_deliver_response()` — the method is 947 lines with `handle_message` doing preflight, dedup, rate limiting, routing, ReAct loop, response delivery, crash recovery, and metrics all in one method. Response delivery (formatting, send, outbound dedup recording) is a distinct concern that should be a separate method for testability.
- [x] Consolidate overlapping schema modules in `src/config/` — `config_schema.py` (365 lines) and `config_schema_defs.py` both define config dataclass fields. The split from Round 4 left the schema spread across two files with unclear ownership. Merge `config_schema.py` into `config_schema_defs.py` so there is one canonical location for all config dataclass definitions.

## Performance & Scalability

- [x] Cache known-existing chat directories in `Memory._ensure_chat_dir()` — currently `mkdir(parents=True, exist_ok=True)` runs on every `write_memory()` and `write_memory_with_checksum()` call even when the directory already exists. A bounded set of known directories would eliminate the syscall overhead on the hot write path.
- [x] Deduplicate outbound hash computation in `TaskScheduler._execute_task()` — `DeduplicationService.check_outbound_duplicate()` computes `xxhash.xxh64` and then `record_outbound()` computes it again for the same text. Add a `check_and_record_outbound()` method that computes the hash once, reducing per-delivery CPU cost by ~50%.
- [x] Lazy-load `MessageQueue` pending messages instead of reading the full JSONL at startup — `_load_pending()` reads the entire file into memory. For high-throughput deployments the file can grow large. Use streaming JSONL parsing (read line-by-line) and stop after loading only PENDING entries, skipping completed entries without materializing them.

## Error Handling & Resilience

- [x] Log `best_effort_flush()` errors in `LLMClient.chat_stream()` instead of silently swallowing — the `finally` block has `except Exception: pass` which masks real errors during stream teardown. At minimum log at WARNING level so that partial-stream failures are observable in production logs.
- [x] Add validation to `ConfigChangeApplier._update_app_config()` before mutating — the method assigns `self._config.llm = new_config.llm` etc. directly without checking that the new config passed validation. If `_from_dict` validation is bypassed (e.g. by a future code path), invalid config could be applied to live components. Validate before mutation.
- [x] Add retry to `RoutingEngine.load_rules()` on transient parse failures — if an `.md` file is being written while `load_rules()` reads it (e.g. user editing over SMB/NFS), the YAML parse may fail and the rule is silently skipped. Retry once after a short delay to handle the concurrent-write window.

## Testing & Quality

- [x] Add integration test for `RoutingEngine` watchdog auto-reload — when a `.md` instruction file is modified on disk, `match_with_rule()` should detect the change via `_is_stale()` and reload rules automatically. No test currently covers this critical hot-reload path.
- [x] Add test for `EventBus` concurrent emit with a failing handler — verify that when one handler raises, other handlers in the same `emit()` call still execute and the error is logged rather than propagated. This is a core safety invariant of the event bus.
- [x] Add test for `VectorMemory` startup degradation path — `_step_vector_memory()` in `builder.py` has complex error handling (probe failure → close → set None → return degraded status). This path is currently untested but is critical for production resilience.
- [x] Add integration test for `MessageQueue` crash recovery with a partially-written JSONL file — create a queue, append entries, simulate a crash by writing a truncated last line, then verify `_load_pending()` recovers all valid entries and logs the corruption.
- [x] Add test for `ConfigChangeApplier` with destructive field changes — verify that destructive fields (e.g. `llm.model`, `llm.api_key`) are logged as warnings but NOT applied to live components, and that safe fields in the same change ARE applied.

## Security

- [x] Verify `HealthServer` binds to `127.0.0.1` (localhost only) by default — the `--health-port` flag creates an HTTP server that could expose operational metrics and component status. Confirm it doesn't bind to `0.0.0.0` which would be reachable from the network. Add a `--health-host` config option with safe default.
- [x] Add rate limiting to `HealthServer` endpoints — an unauthenticated health endpoint can be abused for DoS if exposed. Add a simple per-IP rate limit or request throttling to the health check handler.
- [x] Audit `ConfigChangeApplier` for race conditions during hot-reload — `_update_app_config()` mutates multiple fields on the live `Config` object non-atomically. Under concurrent message processing, a coroutine could observe a partially-updated config (e.g. new `llm.temperature` but old `llm.timeout`). Use a config swap pattern (replace the entire reference atomically).

## DevOps & CI

- [x] Add `.env.example` with all recognized environment variables — the codebase reads several env vars (`SCHEDULER_HMAC_SECRET`, `RATE_LIMIT_CHAT_PER_MINUTES`, `RATE_LIMIT_EXPENSIVE_PER_MINUTES`) but these are only documented in code comments. An `.env.example` file would serve as a single reference for all configurable env vars.
- [x] Add CI step to verify `requirements.txt` is generated from `pyproject.toml` — run `pip-compile pyproject.toml --dry-run` and diff against committed `requirements.txt`. This prevents hand-edits that cause the two files to drift apart.

---

_Round 6 — Senior technical review (2026-05-02). 25 items across 6 categories._

## Architecture & Refactoring

- [x] Reduce `Bot.__init__` parameter count from 15 positional args to a `BotDeps` dataclass — the constructor signature is unwieldy and every new component (e.g. a future cache service) requires updating all callers in `builder.py` and `conftest.py`. A single `BotDeps` parameter carrying all optional dependencies mirrors the `ShutdownContext` pattern established in Round 1 and keeps the surface area narrow.
- [x] Offload `Memory.log_recovery_event` synchronous file I/O to a thread — the method reads and writes `RECOVERY.md` with synchronous `path.read_text()` + `path.write_text()` calls directly on the event loop. Under concurrent recovery scenarios this blocks message processing. Wrap in `asyncio.to_thread()` consistent with the async patterns used elsewhere in `Memory`.
- [x] Eliminate cross-module private attribute access in `Bot.update_config` — the method sets `self._context_assembler._config = new_cfg` (line 997 of `_bot.py`) reaching into `ContextAssembler`'s private `_config`. Add a public `ContextAssembler.update_config(new_cfg: BotConfig)` method to encapsulate the update, matching the pattern used for `LLMProvider` and `Bot` themselves.
- [x] Split `message_queue.py` (1014 lines) into persistence and logic modules — the file mixes `QueuedMessage` dataclass definitions, JSONL file I/O, crash-recovery logic, and async queue operations. Extract persistence into `message_queue_persistence.py` and keep queue logic + recovery in the main module, mirroring the `db.py` → `message_store.py` split from Round 3.

## Performance & Scalability

- [x] Add per-chat timeout to `Bot._handle_message_inner` — the chat lock prevents concurrent processing per chat, but a stuck LLM call or tool execution holds the lock indefinitely, blocking all future messages for that chat. Wrap the `_process` call in `asyncio.wait_for()` with a configurable timeout (default 300s) that cancels the stuck turn and releases the lock, allowing subsequent messages to be processed.
- [x] Use `BoundedOrderedDict` with TTL for `DeduplicationService` outbound cache instead of manual timestamp eviction — the current implementation iterates the full outbound dict on every `record_outbound` call to prune expired entries (O(n) per write). `BoundedOrderedDict` already supports TTL-bounded eviction with lazy purge, eliminating the per-write scan overhead.
- [x] Cache the `_time_to_next_due` computation in `TaskScheduler` — `_compute_adaptive_sleep()` rebuilds the time-to-next-due heap from scratch on every loop iteration even when no tasks have changed. Cache the result and invalidate only when tasks are added, removed, or executed (via a `_tasks_dirty` flag), reducing CPU overhead from ~2880 heap rebuilds/day to a handful.
- [x] Batch `Database.connect()` JSONL schema migrations — `ensure_jsonl_schema` runs per-file with individual `asyncio.to_thread` calls. For workspaces with hundreds of chat files this creates hundreds of thread hops at startup. Collect all migration candidates synchronously, then batch them in a single `asyncio.to_thread` call to reduce startup latency.

## Error Handling & Resilience

- [x] Handle `finish_reason="length"` explicitly in `_react_iteration` — when the LLM hits the token limit, it returns `finish_reason="length"` but the current code falls through to the empty-response fallback, producing the confusing message "The assistant generated an empty response." Detect `"length"` and return a specific message like "⚠️ Response truncated due to length limit. Try asking a more specific question."
- [x] Add structured error categorization to `Application.run()` main loop — the catch-all `except Exception` in `run()` only increments an error counter. Classify the error (transient LLM failure, channel disconnect, filesystem error) and emit an `error_occurred` event with the category so that monitoring subscribers can trigger alerts or auto-recovery for specific failure modes.
- [ ] Close the dedicated embedding `httpx.AsyncClient` when `_step_vector_memory` degrades — the builder step creates a separate `embed_http` client for dedicated embedding URLs, but on probe failure the client is never closed. Add explicit `await embed_http.aclose()` in the degradation path to prevent connection leaks.
- [ ] Add graceful degradation when `RoutingEngine.load_rules()` produces zero rules after a reload — currently if all instruction files are temporarily empty during a hot-reload (e.g. user editing in an editor that saves empty first), all messages are silently ignored. Log a WARNING and retain the previous rule set instead of replacing with an empty list, similar to the ConfigWatcher pattern of keeping old config on failure.

## Testing & Quality

- [ ] Add end-to-end test for `Bot.process_scheduled` with HMAC signing — verify that a signed scheduled prompt passes HMAC verification in `Bot.process_scheduled`, that an unsigned prompt is rejected when `SCHEDULER_HMAC_SECRET` is set, and that a tampered prompt is rejected. Currently no test covers this critical security path.
- [ ] Add test for `Memory.log_recovery_event` file I/O — verify the method creates `RECOVERY.md` on first call, appends on subsequent calls, handles missing directories, and limits error entries to 5. The method has complex string formatting and file I/O with no test coverage.
- [ ] Add test for `TokenUsage` leaderboard correctness after LRU eviction — `_make_per_chat_map` creates a `BoundedOrderedDict(max_size=1000, eviction="half")`. When the per-chat map evicts entries, stale leaderboard entries must be purged by `_purge_chat_from_leaderboard`. Add a test that inserts >1000 chats, triggers eviction, and verifies `get_top_chats()` returns only live entries.
- [ ] Add test for concurrent `Memory.ensure_workspace` with the same chat_id — the `_atomic_seed` method uses `os.O_EXCL` for file creation safety, but `ensure_workspace` itself calls `_ensure_chat_dir` + `_atomic_seed` twice (for `AGENTS.md` and `.chat_id`). Two concurrent calls for the same chat_id could race. Verify only one writer wins and the other completes without error.
- [ ] Add test for `finish_reason="length"` handling in `react_loop` — mock an LLM response with `finish_reason="length"` and non-empty content, verify the loop returns the actual response text rather than the empty-response fallback message.
- [ ] Add integration test for `Database.validate_connection` corruption detection — create a workspace with a corrupted `chats.json` (invalid JSON), a truncated JSONL file, and a checksum-mismatch message entry. Verify `validate_connection` reports errors and warnings for each case with correct field paths.

## Security

- [ ] Validate instruction file paths in `InstructionLoader.load()` — the method receives a filename and joins it with the instructions directory, but doesn't validate against path traversal (e.g. `../../etc/passwd`). Add a check that the resolved path stays within the instructions directory, matching the path validation pattern used in `Memory._validate_path` and `TaskScheduler._resolve_tasks_path`.
- [ ] Reset `_routing_show_errors_var` context variable in all error paths — if an exception occurs in `_process` after `_routing_show_errors_var.set(True)` but before the `finally` block clears the correlation ID, the context var leaks to the next message processed on the same coroutine. Explicitly reset it to its default in the `finally` block of `_handle_message_inner`.
- [ ] Add request path validation to `HealthServer` — the HTTP handler doesn't validate the request path, meaning `GET /any-path` returns a 200 health response. Restrict valid paths to a known set (`/health`, `/metrics`, `/`) and return 404 for anything else, preventing cache-poisoning and log-noise from arbitrary URL probes.

## DevOps & CI

- [ ] Add weekly scheduled CI run — the pipeline only triggers on push/PR. Add a `schedule: cron` trigger (e.g. weekly on Monday) to catch dependency rot, base image CVEs, and flaky test regressions that accumulate silently when no PRs are open.
- [ ] Add CI job to validate `.env.example` matches actual env var usage — grep the source tree for `os.environ.get` and `os.getenv` calls and verify each variable is documented in `.env.example`. Prevents new env vars from being silently introduced without documentation, matching the `requirements.txt` sync check pattern.