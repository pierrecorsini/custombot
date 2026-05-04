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

_Round 7 — Senior technical review (2026-05-03). 20 items across 6 categories._

## Architecture & Refactoring

- [x] Close `RoutingEngine` watchdog observer during `perform_shutdown()` — `RoutingEngine.close()` exists and stops the OS-native observer thread, but `lifecycle.py` never calls it. The watchdog thread continues running until process exit, which can produce spurious dirty-flag writes and prevents clean teardown in environments that reuse the process (e.g. hot-reload development servers).
- [x] Extract `_send_to_chat(chat_id, text, channel)` helper in `Bot` — `_handle_message_inner` sends a rate-limit warning via `channel.send_message()` directly, bypassing outbound dedup recording and event emission. A shared `_send_to_chat()` method would centralize send + dedup + `response_sent` event, used by both the rate-limit path and `_deliver_response`, ensuring all outbound messages are tracked consistently.
- [x] Fix misleading generation-conflict handling in `Bot._deliver_response()` — `check_generation()` returns `False` when the generation counter changed during processing, but the comment says "Re-reading latest history before persist" and no re-reading actually occurs. The code proceeds to `save_messages_batch` unconditionally, which can overwrite newer messages from a concurrent turn. Either implement the re-read (read latest messages, append response, write back) or update the comment to accurately describe the current "log-and-proceed" behavior and document the data-loss risk.

## Performance & Scalability

- [x] Use swap-buffers pattern in `MessageQueue._flush_loop` to avoid blocking enqueue/complete during disk writes — the flush loop acquires `self._lock` before calling `_flush_write_buffer()`, which holds the lock for the entire fsync duration. Under burst traffic, enqueued messages queue behind the flush. Swap the write buffer atomically (pointer swap under lock), then flush the detached buffer without the lock.
- [x] Build reverse index for `TokenUsage._leaderboard` to avoid O(n) `_purge_chat_from_leaderboard` scans — the purge function iterates the entire leaderboard list for every new `chat_id` insertion. For deployments with hundreds of chats and high turnover, maintain a `dict[chat_id, list[int]]` reverse index so purging is O(k) where k is that chat's entries, not O(n) for all entries.
- [x] Short-circuit `RoutingEngine.match_with_rule()` before cache key computation when no rules are loaded — `match_with_rule()` first calls `_is_stale()` and `load_rules()`, then constructs a `MatchingContext` and computes an `xxhash.xxh64` cache key. When `_rules_list` is empty (common during initial startup before instruction files are added), the expensive xxhash computation is wasted. Check `has_rules` early and return `(None, None)` immediately.

## Error Handling & Resilience

- [x] Emit `message_dropped` event when `_build_turn_context()` produces no match — the method silently returns `None` in two cases: routing engine has no rules loaded, and no rule matched the message. Emit a structured event (e.g. `message_dropped` with `reason="no_rules"` or `reason="no_match"`) so monitoring subscribers can track silently-dropped messages without parsing log lines.
- [x] Handle `asyncio.CancelledError` explicitly in `Bot._handle_message_inner()` — the `except Exception` catch in the processing try block does not distinguish cancellation (expected during shutdown via `per_chat_timeout` or `wait_for`) from real errors. CancelledError should not increment error metrics, emit `error_occurred` events, or trigger `record_exception_safe` on the span. Add an explicit `except asyncio.CancelledError` before the generic `except Exception`.
- [x] Validate and truncate `IncomingMessage.sender_name` before first use — `sender_name` is used directly in `log.info()` calls and passed as `name=` to `save_message()` without length or character validation. A very long sender_name (>10 KB) or one containing control characters (ANSI escapes, null bytes) can pollute structured log entries and downstream JSON consumers. Truncate to 200 characters and strip non-printable characters in `handle_message()`.

## Testing & Quality

- [x] Add test for `Bot._deliver_response()` with generation conflict — mock `check_generation()` to return `False`, verify that the warning is logged and `save_messages_batch()` is still called (current behavior). Documents the existing design choice that generation conflicts are logged but not retried.
- [x] Add test for `RoutingEngine.close()` stopping the watchdog observer — construct a `RoutingEngine` with `use_watchdog=True`, call `load_rules()` to start the observer, then call `close()` and verify `_observer` is `None` and the observer thread has stopped (`observer.is_alive() == False`).
- [x] Add test for `ContextAssembler` graceful degradation when one of the four async reads fails — mock `memory.read_memory()` to raise `OSError`, verify `assemble()` returns a valid `ContextResult` with the default value (`None`) substituted for the failed read and the other three reads unaffected.
- [x] Add integration test for `MessageQueue` concurrent flush and enqueue — start the `_flush_loop`, enqueue messages in parallel from multiple coroutines, verify all messages are persisted to disk after the flush cycle completes and no data is lost or corrupted.
- [x] Add test for `Bot._deliver_response()` with outbound dedup suppression — mock `check_outbound_duplicate()` to return `True`, verify the method returns `None` without calling `save_messages_batch()` or `record_outbound()`, confirming that dedup-suppressed responses don't create phantom DB entries.

## Security

- [x] Reject symlinks in `RoutingEngine` instruction file scanning — `load_rules()` and `_scan_file_mtimes()` iterate `.md` files via `glob()` and `os.scandir()` without checking for symlinks. A symlink within the instructions directory pointing outside the workspace could cause the engine to parse arbitrary files as routing rules. Add `os.path.islink()` checks when iterating instruction files.
- [x] Validate `schedule.weekdays` range (0-6) in `TaskScheduler._validate_task()` — the cron schedule type accepts a `weekdays` list but doesn't validate that values are integers in the range 0-6. A malformed `tasks.json` with `weekdays: [7, 8]` would silently match no days, causing the task to never execute. Add range validation with a clear error message.
- [x] Add `Content-Security-Policy: default-src 'none'` and `X-Content-Type-Options: nosniff` headers to `HealthServer` responses — the health endpoint already has path validation and rate limiting, but adding security headers hardens it against content-type sniffing and script injection if the endpoint is inadvertently exposed to browsers (e.g. via an internal dashboard iframe).

## DevOps & CI

- [x] Add `.gitattributes` with `* text=auto eol=lf` for cross-platform line ending normalization — the project is developed on Windows but deployed to Linux (Dockerfile). Without `.gitattributes`, line endings drift between CRLF (Windows editors) and LF (Docker/CI), causing spurious diffs and potential script failures (e.g. `h24loop.sh` with Windows line endings).
- [x] Add `ruff` `PL` (pylint) ruleset to lint config — currently `E, W, F, I, UP, B, SIM, TCH` are selected. Adding `PL` catches additional patterns: `PLR0913` (too many arguments — useful for future constructors), `PLR2004` (magic value comparison), and `PLW2901` (redefined loop variable). Run as non-blocking initially to avoid disrupting existing code.
- [x] Add `pytest-xdist -n auto` to CI for parallel test execution — `pytest-xdist` is in dev dependencies but may not be used with `-n auto` in CI. The test suite has 55+ test files; parallel execution can reduce CI feedback time by 2-4× on multi-core runners without any test changes (all tests use `asyncio_mode = "auto"` and independent temp directories).

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
- [x] Close the dedicated embedding `httpx.AsyncClient` when `_step_vector_memory` degrades — the builder step creates a separate `embed_http` client for dedicated embedding URLs, but on probe failure the client is never closed. Add explicit `await embed_http.aclose()` in the degradation path to prevent connection leaks.
- [x] Add graceful degradation when `RoutingEngine.load_rules()` produces zero rules after a reload — currently if all instruction files are temporarily empty during a hot-reload (e.g. user editing in an editor that saves empty first), all messages are silently ignored. Log a WARNING and retain the previous rule set instead of replacing with an empty list, similar to the ConfigWatcher pattern of keeping old config on failure.

## Testing & Quality

- [x] Add end-to-end test for `Bot.process_scheduled` with HMAC signing — verify that a signed scheduled prompt passes HMAC verification in `Bot.process_scheduled`, that an unsigned prompt is rejected when `SCHEDULER_HMAC_SECRET` is set, and that a tampered prompt is rejected. Currently no test covers this critical security path.
- [x] Add test for `Memory.log_recovery_event` file I/O — verify the method creates `RECOVERY.md` on first call, appends on subsequent calls, handles missing directories, and limits error entries to 5. The method has complex string formatting and file I/O with no test coverage.
- [x] Add test for `TokenUsage` leaderboard correctness after LRU eviction — `_make_per_chat_map` creates a `BoundedOrderedDict(max_size=1000, eviction="half")`. When the per-chat map evicts entries, stale leaderboard entries must be purged by `_purge_chat_from_leaderboard`. Add a test that inserts >1000 chats, triggers eviction, and verifies `get_top_chats()` returns only live entries.
- [x] Add test for concurrent `Memory.ensure_workspace` with the same chat_id — the `_atomic_seed` method uses `os.O_EXCL` for file creation safety, but `ensure_workspace` itself calls `_ensure_chat_dir` + `_atomic_seed` twice (for `AGENTS.md` and `.chat_id`). Two concurrent calls for the same chat_id could race. Verify only one writer wins and the other completes without error.
- [x] Add test for `finish_reason="length"` handling in `react_loop` — mock an LLM response with `finish_reason="length"` and non-empty content, verify the loop returns the actual response text rather than the empty-response fallback message.
- [x] Add integration test for `Database.validate_connection` corruption detection — create a workspace with a corrupted `chats.json` (invalid JSON), a truncated JSONL file, and a checksum-mismatch message entry. Verify `validate_connection` reports errors and warnings for each case with correct field paths.

## Security

- [x] Validate instruction file paths in `InstructionLoader.load()` — the method receives a filename and joins it with the instructions directory, but doesn't validate against path traversal (e.g. `../../etc/passwd`). Add a check that the resolved path stays within the instructions directory, matching the path validation pattern used in `Memory._validate_path` and `TaskScheduler._resolve_tasks_path`.
- [x] Reset `_routing_show_errors_var` context variable in all error paths — if an exception occurs in `_process` after `_routing_show_errors_var.set(True)` but before the `finally` block clears the correlation ID, the context var leaks to the next message processed on the same coroutine. Explicitly reset it to its default in the `finally` block of `_handle_message_inner`.
- [x] Add request path validation to `HealthServer` — the HTTP handler doesn't validate the request path, meaning `GET /any-path` returns a 200 health response. Restrict valid paths to a known set (`/health`, `/metrics`, `/`) and return 404 for anything else, preventing cache-poisoning and log-noise from arbitrary URL probes.

## DevOps & CI

- [x] Add weekly scheduled CI run — the pipeline only triggers on push/PR. Add a `schedule: cron` trigger (e.g. weekly on Monday) to catch dependency rot, base image CVEs, and flaky test regressions that accumulate silently when no PRs are open.
- [x] Add CI job to validate `.env.example` matches actual env var usage — grep the source tree for `os.environ.get` and `os.getenv` calls and verify each variable is documented in `.env.example`. Prevents new env vars from being silently introduced without documentation, matching the `requirements.txt` sync check pattern.

---

_Round 8 — Senior technical review (2026-05-03). 20 items across 6 categories._

## Architecture & Refactoring

- [x] Extract `ErrorHandlerMiddleware` error reply into `_send_error_reply()` helper — the middleware directly calls `self._channel.send_message(ctx.msg.chat_id, error_msg)` without recording outbound dedup or emitting a `response_sent` event, unlike every other outbound path (rate-limit warnings, `_deliver_response`, scheduled replies). A shared `_send_error_reply()` method would centralize send → dedup → event for error responses, making error-channel traffic observable in metrics and dedup logs.
- [x] Reduce `_react_iteration()` parameter count from 16 to a `ReactIterationContext` dataclass — the function takes `iteration, max_tool_iterations, chat_id, llm, metrics, messages, tools, stream_callback, stream_response, max_retries, initial_delay, retryable_codes, tool_executor, workspace_dir, channel, tool_log, buffered_persist, span` (18 parameters). Grouping the invariant parameters (llm, metrics, tool_executor, retryable_codes, etc.) into a frozen dataclass would improve readability and allow `react_loop()` to construct it once instead of threading 18 arguments per iteration.
- [x] Unify duplicate `_utc_offset_hours` computation between `TaskScheduler._is_due()` and `_time_to_next_due()` — both methods independently compute `local_total_min`, `utc_total_min`, `utc_hour`, and `utc_minute` for daily/cron schedules. Extract a `_target_utc_time(schedule, local_offset)` helper returning `(utc_hour, utc_minute)` so the conversion logic is defined once and both call sites stay in sync.

## Performance & Scalability

- [x] Lazy-initialize `ToolExecutor._audit_logger` on first call instead of checking `self._audit_log_dir is None` on every invocation — `_audit()` creates the `SkillAuditLogger` lazily, but the `if self._audit_logger is None: if self._audit_log_dir is None: return` double-check runs on every tool call even after initialization. Set `_audit_log_dir = None` in `__init__` and skip the inner branch once `_audit_logger` is populated (currently works, but the `self._audit_log_dir is None` path is dead after first init since `_audit_log_dir` is released).
- [x] Avoid `list(self._pending.values())` copy in `MessageQueue._persist_pending()` — `messages = list(self._pending.values())` creates a full list copy on every persist call (triggered every `_compact_threshold` completions and on close). Since the pending dict is only mutated under `_lock` and the persist is called within that lock, pass a view or use `self._pending.values()` directly in the persistence layer, which only iterates the values.
- [x] Pre-compute `MatchingContext` and cache key in `Bot._build_turn_context()` before calling `match_with_rule()` — `_build_turn_context` calls `self._routing.match_with_rule(msg)` which internally creates a `MatchingContext` and computes an `xxhash.xxh64` cache key. If the match cache hits, this is wasted work on the next call. Consider passing a pre-built `MatchingContext` to `match_with_rule()` to allow the caller to reuse it for the `_cache_key` lookup.

## Error Handling & Resilience

- [x] Emit `message_dropped` event when `Bot.handle_message()` rejects due to `msg.text` exceeding `MAX_MESSAGE_LENGTH` — currently the length check logs a warning and returns `None` silently. For monitoring, emitting a `message_dropped` event with `reason="message_too_long"` allows subscribers to track oversized-message rejections without parsing log lines, matching the pattern used for routing-related drops in `_build_turn_context()`.
- [x] Handle `QueuePersistence.flush_buffer()` disk-full errors gracefully in `MessageQueue._flush_write_buffer()` — the flush call raises on I/O failure but the error propagates up through `_maybe_flush_buffer()` to `enqueue()`, potentially causing message loss. Catch the exception, log a warning, and buffer the line for retry on the next flush cycle instead of losing the queued message entirely.
- [x] Add structured logging for `Application._shutdown_cleanup()` timeout paths — when `config_watcher.stop()` or `perform_shutdown()` times out, only a generic warning is logged. Include the step name, timeout duration, and which components were affected in the log data dict so that monitoring dashboards can alert on slow shutdowns and identify the bottleneck component.

## Testing & Quality

- [x] Add test for `ConfigChangeApplier._apply_llm_config` preserving destructive fields — verify that a hot-reload with a new `llm.model` and `llm.temperature` only applies the temperature change to the live LLM provider, and that the provider's underlying `_cfg.model` remains unchanged. This is a critical safety invariant of the hot-reload system that currently lacks dedicated test coverage.
- [x] Add test for `react_loop` with `content_filter` finish_reason — some LLM providers return `finish_reason="content_filter"` when the response is blocked. Verify the loop returns the empty-response fallback rather than crashing on an unhandled finish_reason, documenting the current behavior.
- [x] Add test for `process_tool_calls` with `MAX_TOOL_CALLS_PER_TURN` rejection — mock an LLM response with more tool calls than the limit, verify that excess calls receive the rejection message, that the messages list is still well-formed (assistant + tool messages pair correctly), and that the returned tool_log only contains executed (non-rejected) calls.
- [x] Add test for `Application._transition()` rejecting invalid phase transitions — construct an `Application` in `CREATED` phase and verify that attempting `_transition(AppPhase.STOPPED)` raises `RuntimeError` with a clear message. Also verify the valid CREATED→STARTING→RUNNING→SHUTTING_DOWN→STOPPED sequence completes without error.
- [x] Add test for `TokenUsage.add_for_chat` concurrent access from multiple threads — spawn several threads that simultaneously call `add_for_chat` on a shared `TokenUsage` instance, verify that `total_tokens` equals the sum of all individual increments and that no entries are lost or corrupted, validating the `ThreadLock` guard.

## Security

- [x] Validate `IncomingMessage.correlation_id` format in `Bot.handle_message()` — the correlation ID is propagated to logging context, OTel spans, and event bus events. A malicious or corrupted correlation ID containing control characters (newlines, ANSI escapes) could inject false log entries or corrupt structured log consumers. Truncate to a reasonable length and strip non-printable characters.
- [x] Sanitize `tool_call.function.name` before using in log entries and audit trail — `ToolExecutor.execute()` uses the skill name directly in structured log `extra` dicts and audit log entries. A malicious LLM response could inject a name containing log-forging characters (newlines, JSON-breaking quotes). Strip or replace dangerous characters before the first log/audit use.
- [x] Add `Strict-Transport-Security` header to `HealthServer` responses when accessed over HTTPS — the health server already sets `Content-Security-Policy` and `X-Content-Type-Options` from Round 7, but lacks HSTS. If the server is deployed behind a TLS-terminating proxy, the header instructs browsers to only use HTTPS for future requests, hardening against protocol downgrade attacks on internal dashboards.

## DevOps & CI

- [x] Add `ruff` `TCH` strict enforcement for TYPE_CHECKING block violations — the `TCH` (flake8-type-checking) ruleset is selected but violations may not block CI. Strict enforcement would ensure all TYPE_CHECKING imports are actually only used for type hints, preventing accidental runtime imports from being hidden behind the guard, which can cause circular imports or import-time side effects.
- [x] Add `mypy --strict` to `src/bot/` package specifically — `src/bot/` is the most complex module (4 files, ~1700 lines total) with the highest interaction surface. Enabling strict type checking on this package first catches type errors in the critical message-processing path while avoiding the disruption of enabling it globally. Mark as non-blocking initially.
- [x] Add benchmark regression test for `RoutingEngine.match_with_rule()` — with pre-computed channel indexes and TTL-bounded caching, the routing match should complete in <1ms for typical rule sets. Add a `bench_regression.py` entry that fails if match latency regresses beyond a threshold, catching performance regressions from future index changes.

---

_Round 9 — Senior technical review (2026-05-04). 20 items across 6 categories._

## Architecture & Refactoring

- [x] Make `RoutingEngine.load_rules()` non-blocking — the retry path uses `time.sleep(0.1)` (synchronous) on the event loop when an instruction file fails to parse. Since `match_with_rule()` is called from the async hot path (`_build_turn_context` → `match_with_rule` → `_is_stale` → `load_rules`), a parse failure blocks the entire event loop for 100ms. Convert the retry to `asyncio.sleep()` and wrap the synchronous `parse_file()` call in `asyncio.to_thread()`, or make the reload fully async and defer it to a background task.
- [x] Decouple `VectorMemory` from `LLMClient` internals — `_step_vector_memory()` in `builder.py` accesses `ctx.llm.openai_client` (a private attribute of `LLMClient`) to share the OpenAI client for embeddings when no dedicated embedding URL is configured. If `LLMClient` changes its internal client structure, VectorMemory silently breaks. Add a public `LLMProvider.openai_client` property (read-only, documented as shared-resource) to encapsulate the access, matching the pattern used for `circuit_breaker` and `update_config()`.
- [x] Parallelize independent shutdown pre-steps in `Application._shutdown_cleanup()` — `config_watcher.stop()` and `workspace_monitor.stop()` are awaited sequentially (lines 561–598 of `app.py`) before `perform_shutdown()`, but they are independent services with no ordering dependency. Run them concurrently via `asyncio.gather()` to reduce shutdown latency, mirroring the parallel pattern already used in `perform_shutdown()` step 5.
- [x] Extract `AppComponents.to_shutdown_context()` factory method — `_shutdown_cleanup()` manually constructs a 14-field `ShutdownContext` from `self._state` fields (lines 601–619 of `app.py`), tightly coupling `Application` to `ShutdownContext`'s field list. Every new component added to `AppComponents` requires updating both `ShutdownContext` and `_shutdown_cleanup()`. A factory method on `AppComponents` (or `ShutdownContext.from_components()`) centralizes the mapping and makes it discoverable via type errors when fields drift.

## Performance & Scalability

- [x] Add in-memory LRU cache for `DeduplicationService.is_inbound_duplicate()` — every inbound message hits the database via `self._storage.has_message(message_id)`. Under high throughput (burst of messages from active groups), this creates a DB query per message. A bounded in-memory LRU (e.g. 10K entries, 5-minute TTL) of recently-seen message IDs would eliminate most DB queries — true duplicates arrive within seconds, and unique IDs never need re-checking after the first miss ages out.
- [x] Cache parsed `last_run` datetimes in `TaskScheduler` task dicts — `_is_due()` calls `datetime.fromisoformat(task["last_run"])` on every scheduler tick for every task. With 50 tasks and 30-second ticks, this is ~144K string-to-datetime parses per day. Store the parsed datetime object alongside the ISO string (e.g. `task["_last_run_dt"]`) and invalidate it when `last_run` is updated in `_execute_task()`, eliminating redundant parsing.
- [x] Use `orjson` for `TaskScheduler._write_tasks_file()` JSON serialization — `_write_tasks_file()` uses `json.dumps(data, indent=2)` for tasks.json persistence. `orjson` (already a project dependency) is 2–3× faster for JSON serialization with a 2-space-indent option (`orjson.OPT_INDENT_2`). Since this runs in a thread pool during `asyncio.to_thread()`, faster serialization reduces thread-pool occupancy and frees the thread for other work (DB writes, vector memory batches).

## Error Handling & Resilience

- [x] Fix `Bot._handle_message_inner` timeout not completing queue message — when the per-chat timeout fires (`asyncio.TimeoutError` at line 562), the message is never marked as completed in the queue (`_message_queue.complete()` is only called on the success path at line 539). The timed-out message remains PENDING in the JSONL file, causing crash recovery on the next restart to re-process it, producing a duplicate response. Call `_message_queue.complete()` (best-effort, non-blocking) in the `except asyncio.TimeoutError` handler.
- [x] Add fail-open behavior to `DeduplicationService.is_inbound_duplicate()` on DB errors — the current implementation propagates database exceptions to the caller (`Bot.handle_message`), which then rejects the message. During transient DB failures (locked SQLite, disk I/O spike), legitimate messages are silently dropped. Catch `DatabaseError` in `is_inbound_duplicate()`, log a warning, and return `False` (allow the message through) — a dedup miss is preferable to message loss.
- [x] Use atomic file writes in `TaskScheduler._write_tasks_file()` — the method writes `tasks.json` directly via `path.write_text(content)`. A crash or power loss mid-write truncates the file, losing all task definitions for that chat. Use the write-to-temp-then-rename pattern already established in `Memory._atomic_seed()` and `QueuePersistence.flush_buffer()` to ensure atomic replacement.
- [x] Add stdin read timeout in `BaseChannel._confirm_send()` — `_confirm_send()` calls `await asyncio.to_thread(input, ...)` with no timeout. If stdin is a pipe that never sends data (misconfigured Docker/systemd, or a CI environment where `sys.stdin.isatty()` returns True unexpectedly), the coroutine blocks forever, preventing message processing and blocking graceful shutdown. Wrap the input read in `asyncio.wait_for()` with a configurable timeout (default 60s).

## Testing & Quality

- [x] Add unit test for `_classify_main_loop_error()` category mapping — the function maps exception types to monitoring categories (LLM_TRANSIENT, LLM_PERMANENT, CHANNEL_DISCONNECT, FILESYSTEM, CONFIGURATION, UNKNOWN), but the mapping is untested. Verify: `LLMError(ErrorCode.LLM_RATE_LIMITED)` → LLM_TRANSIENT, `LLMError(ErrorCode.LLM_AUTH_FAILED)` → LLM_PERMANENT, `BridgeError` → CHANNEL_DISCONNECT, `DatabaseError` → FILESYSTEM, `ConfigurationError` → CONFIGURATION, generic `RuntimeError` → UNKNOWN.
- [x] Add test for `Bot._handle_message_inner` timeout path queue state — mock the per-chat timeout to fire during `_process()`, then verify: (a) the message is NOT completed in the queue (current behavior — documents the known issue), (b) the timeout error is logged with the correct attributes, and (c) the chat lock is released. If the fix from the Error Handling section is applied, update the test to verify completion IS called.
- [x] Add integration test for hot-reloaded shell denylist enforcement — create a config with `shell.command_denylist: []`, trigger a config hot-reload that adds a denied command (e.g. `"rm"`) to the denylist, then verify that a subsequent shell skill execution with `rm -rf /` is rejected. Currently no test covers the hot-reload → skill-behavior-change path.
- [x] Add test for `Application._transition()` rollback on startup failure — construct an `Application` in CREATED phase, begin startup (transition to STARTING), then simulate a step failure. Verify the phase transitions correctly (STARTING → SHUTTING_DOWN → STOPPED) and that partially-initialised components are cleaned up without AttributeError.

## Security

- [x] Cap cumulative retry sleep in `RoutingEngine.load_rules()` — each instruction file that fails to parse triggers a 100ms `time.sleep(0.1)` retry. With 20 corrupted files, this blocks the event loop for 2+ seconds. Add a cumulative retry budget (e.g. 1 second total across all files) and skip remaining retries once exhausted. This prevents a denial-of-service attack via crafted instruction files that exploit the retry logic.
- [x] Validate loaded tasks in `TaskScheduler._load()` post-deserialization — `_load()` parses `tasks.json` via `json.loads(raw)` and stores the result directly in `self._tasks` without re-running `_validate_task()`. A corrupted or tampered file with an oversized `prompt`, an invalid `schedule.type`, or `weekdays` outside 0–6 would cause runtime errors later. Run `_validate_task()` on each loaded entry and skip invalid tasks with a warning, similar to how `RoutingEngine.load_rules()` skips malformed rules.

## DevOps & CI

- [x] Add CI step to verify `config.example.json` matches the current `Config` dataclass schema — the `.env.example` sync check already catches undocumented env vars, but there is no equivalent gate for config keys. A new job should parse `config.example.json`, extract all field paths (recursively), and verify each matches a field in the `Config` dataclass hierarchy. Prevents new config fields from silently appearing without documentation.
- [x] Add Docker BuildKit layer caching to CI — the `docker-smoke` and `docker-scan` jobs each build the full Docker image from scratch (~60–90s). Using `docker/build-push-action` with `cache-from: type=gha` and `cache-to: type=gha,mode=max` enables GitHub Actions-native layer caching, reducing rebuild time by ~50% when only the final layers change (typical for source-only PRs).
- [x] Add coverage regression gate to CI — the test job uploads `coverage.xml` as an artifact but never compares it against a baseline. Add a step that extracts the coverage percentage from the XML report, compares it against a stored threshold file (e.g. `.coverage-floor`), and fails if current coverage drops below the threshold. Update the threshold automatically on main-branch merges when coverage increases. Prevents silent coverage erosion across PRs.

---

_Round 10 — Senior technical review (2026-05-04). 25 items across 6 categories._

## Architecture & Refactoring

- [x] Extract `Bot._process()` message-persistence + context-assembly into `_prepare_turn()` — `_process()` currently does: emit event → `upsert_chat` → `save_message` → `ensure_workspace` → `_build_turn_context` → `_react_loop` → `_deliver_response`. The first four steps (emit, upsert, save user message, workspace seed) are turn-*preparation* concerns that should be in a separate method. This would make `_process()` a simple orchestrator: `_prepare_turn()` → `_react_loop()` → `_deliver_response()`, enabling independent testing of turn preparation (user message persistence, workspace readiness) without mocking the full ReAct loop.
- [x] Replace `log_noncritical()` string category parameter with enum — `NonCriticalCategory` exists as a string enum, but `log_noncritical()` accepts raw strings via `NonCriticalCategory` (a `StrEnum`). Several call sites pass `NonCriticalCategory.SHUTDOWN` while others pass plain strings. Standardize all call sites to use the enum and add a type hint enforcement so that new call sites cannot pass arbitrary strings without a lint error. This makes non-critical error categorization exhaustive and auditable.
- [x] Move `src/llm_error_classifier.py` into `src/llm/` package (new) — the LLM subsystem is currently split across `llm.py`, `llm_provider.py`, and `llm_error_classifier.py` as sibling files in `src/`. Grouping them into `src/llm/` (with `__init__.py` re-exporting `LLMClient` for backward compat) would reduce top-level clutter (40 entries in `src/`), match the pattern already used for `src/bot/`, `src/config/`, `src/vector_memory/`, and make the LLM subsystem's boundaries explicit.
- [x] Add `__slots__` to `QueuedMessage` dataclass — the dataclass has 10 fields and is instantiated for every queued message. Adding `slots=True` (matching `TurnContext`, `BotConfig`, `BotDeps`, `ReactIterationContext`) reduces per-instance memory overhead by ~40% and prevents accidental attribute creation, consistent with the codebase's established pattern of slotting high-frequency dataclasses.
- [x] Extract `MessagePipeline.execute()` middleware unwinding into a reusable `MiddlewareChain` — `MessagePipeline.__init__()` wraps middleware callables into a chain via nested closures, which is correct but hard to debug (the closure stack doesn't appear in tracebacks). Extract the chain-building logic into a named `MiddlewareChain` class with `__repr__` showing the middleware names, so that errors during pipeline execution show which middleware failed in the traceback.

## Performance & Scalability

- [x] Pre-warm the `FileHandlePool` for active chats at startup — the `Database.connect()` step opens the JSONL files for all known chats, but the `FileHandlePool` starts empty. The first write to each chat file incurs an `open()` syscall. During crash recovery, this means N `open()` calls for N recovered chats, serialized by the lock. After `db.connect()`, warm the pool by calling `get_or_open()` for each known chat file in a single thread hop.
- [x] Avoid re-serializing tool call arguments in `execute_tool_call()` — the function calls `json.loads(tool_call.function.arguments)` to build the `ToolLogEntry`, but `tool_call.function.arguments` is already a JSON string from the LLM response. The parsed dict is only used for the log entry. For large argument payloads (e.g. base64-encoded file contents), this duplicates the string in memory. Store the raw JSON string in `ToolLogEntry` and parse lazily only when the log entry is actually rendered.
- [x] Batch `DeduplicationService.record_outbound()` writes — currently `record_outbound()` updates the `BoundedOrderedDict` synchronously on every outbound message. During burst delivery (e.g. scheduled task fan-out to many chats), this creates N individual dict operations. Collect outbound recordings in a buffer during burst mode and flush them in a single batch, reducing the number of times the `BoundedOrderedDict` eviction logic runs.
- [ ] Use `msgpack` for `MessageQueue` persistence instead of JSON — `msgpack` is already a project dependency (used for vector memory) and is ~3–5× faster than `json` for serialization of structured data with many string fields. The `QueuedMessage` objects written to `message_queue.jsonl` have 10 fields each. Switching to msgpack-binary JSONL (each line is a msgpack-packed blob) would reduce persistence latency under burst traffic. Keep JSON fallback for crash recovery readability.

## Error Handling & Resilience

- [ ] Add `finally` block to `_step_vector_memory()` that closes `embed_http` on *any* exception path — the builder step has a `try/except` that degrades gracefully, but if `vm.connect()` raises (after `embed_http` is created but before `vm` is assigned), the `embed_http` client leaks. Move the `embed_http.aclose()` into a `finally` block rather than the `except` handler, so it's closed regardless of which line fails.
- [ ] Add structured warning event when `Bot._deliver_response()` encounters a generation conflict — currently the generation conflict is only logged as a warning. Emit a `generation_conflict` event with `chat_id`, `expected_generation`, and `current_generation` so that monitoring subscribers can track write-conflict frequency and identify chats with concurrent processing issues (rare but data-corrupting when it occurs).
- [ ] Handle `OSError` (disk full / permission denied) in `_deliver_response()` during `save_messages_batch()` — the method calls `await self._db.save_messages_batch()` without catching filesystem errors. If the disk is full, the exception propagates to `_handle_message_inner()` where it's logged as a generic error and the response is lost. Catch `OSError`, emit a warning, and return the response text anyway so the user sees the answer even if persistence fails (the response was already generated at this point).
- [ ] Emit a startup health event via `EventBus` after `Application._startup()` completes — `Application.run()` transitions to `RUNNING` phase and logs startup completion, but there is no event for external subscribers (e.g. a monitoring dashboard, a Slack webhook) to detect successful startup. Emit a `startup_complete` event with component count, total duration, and per-component timing, mirroring the data already available in `component_durations`.

## Testing & Quality

- [ ] Add test for `Bot._send_to_chat()` with and without channel — the method is the shared send+dedup+event helper, but has no direct test. Verify: (a) with channel → `channel.send_message()` is called, dedup is recorded, `response_sent` event emitted; (b) without channel → no send call, dedup still recorded, event still emitted. Documents the design choice that dedup tracking is independent of channel delivery.
- [ ] Add test for `Application._swap_config()` atomicity guarantee — the method exists to enable atomic config replacement during hot-reload, but there's no test verifying that the swap is indeed a single attribute assignment. Add a test that inspects `_config` before and after a concurrent `_swap_config` call to verify the reference changes atomically (no partial state observable).
- [ ] Add test for `_step_vector_memory()` with dedicated embedding URL and probe failure — the builder step has a complex degradation path when `embedding_base_url` is set: it creates a dedicated `embed_http`, creates an `AsyncOpenAI` client, calls `vm.probe_embedding_model()`, and on failure must close `embed_http`. No test currently covers the dedicated-URL degradation path. Verify the dedicated client is properly closed.
- [ ] Add property-based test for `outbound_key()` hash consistency — verify that for any two `(chat_id, text)` pairs, `outbound_key(a, b) == outbound_key(a, b)` (determinism) and `outbound_key(a, b) != outbound_key(c, d)` when pairs differ (collision resistance for practical inputs). Use hypothesis with `st.text()` strategies to exercise edge cases (empty strings, Unicode, very long inputs).
- [ ] Add integration test for the full `_on_message` → pipeline → `_handle_message_inner` path with a timed-out message — construct an `Application` in `RUNNING` phase, inject a message that triggers a long `_process` call, verify that: (a) the semaphore is released, (b) the timeout error is logged with correct attributes, (c) subsequent messages are still processed. This is the most critical production path and currently only has unit-level coverage.

## Security

- [ ] Sanitize `IncomingMessage.sender_name` in the validation layer (`channels/base.py`) — the field is used throughout the bot in `log.info()` and `save_message()` but is only validated for format in `_validate_chat_id()` (which doesn't apply to `sender_name`). A sender name containing ANSI escape sequences (`\x1b[31m`) or control characters could corrupt structured log output or downstream JSON consumers. Add validation in `IncomingMessage.__post_init__()` that truncates to 200 characters and strips non-printable characters.
- [ ] Add `Content-Length` header validation to `HealthServer` request handling — the health server doesn't check `Content-Length` on incoming requests. A client sending an extremely large `Content-Length` header could cause the server to allocate memory before the path-validation check rejects the request. Add an early check that rejects requests with `Content-Length` > 0 on GET endpoints (health checks are GET-only).
- [ ] Validate `ToolLogEntry.name` length before audit log write — `ToolExecutor._audit()` writes `ToolLogEntry.name` to the audit log file without length validation. A malicious LLM response could inject a tool name longer than the filesystem path limit (4096 chars on Linux), potentially causing an `OSError` during audit log write. Truncate the tool name in the `ToolLogEntry` constructor to a reasonable maximum (200 chars, matching `_MAX_TOOL_NAME_LENGTH`).

## DevOps & CI

- [ ] Add `Ruff` `PERF` ruleset to lint config — currently `E, W, F, I, UP, B, SIM, TCH, PL` are selected. Adding `PERF` (perflint) catches performance anti-patterns: `PERF401` (list-comprehension instead of `for` loop + `append`), `PERF402` (list-comprehension instead of `for` loop + `list.extend`), and `PERF203` (`try`-`except` in loop body). These are common patterns in hot paths (routing, dedup, message processing). Run as non-blocking initially.
- [ ] Add `pip-audit` SARIF output upload to GitHub Security tab — the security job runs `pip-audit --desc` but only outputs to the job log. Adding `--format sarif --output pip-audit-results.sarif` and uploading via `github/codeql-action/upload-sarif` would surface dependency vulnerabilities in the GitHub Security tab alongside the Trivy Docker scan results, providing a unified vulnerability dashboard.
- [ ] Pin `ruff` version in `pyproject.toml` dev dependencies instead of CI-only install — the lint job installs `ruff==0.15.12` directly via `pip install` in CI, but `ruff>=0.15.0` is in `pyproject.toml` dev dependencies. This version mismatch means local `ruff` (from `pip install .[dev]`) may have different rules than CI. Pin `ruff==0.15.12` in `pyproject.toml` and remove the separate CI install step, ensuring local and CI linting are identical.
- [ ] Add `pytest-timeout` to dev dependencies and CI — long-running or hung tests (e.g. async tests waiting on a mock that never resolves) can stall CI indefinitely. `pytest-timeout` adds a per-test timeout (configurable, e.g. 60s) that fails the test if it exceeds the limit. Add to dev deps, configure in `pyproject.toml` with `timeout = 120`, and add `--timeout=120` to CI pytest invocation. This catches deadlocks and event-loop stalls that `per_chat_timeout` only handles at the bot level.
- [ ] Add CI step to validate `PLAN.md` checkbox syntax — the plan file has grown to 280+ items across 10 rounds. A simple CI check that counts `- [x]` vs `- [ ]` items and verifies no malformed checkbox lines (e.g. `- [X]`, `- [x ]`, `- [ x]`) would catch syntax errors that make items invisible to progress tracking scripts. Add a minimal `scripts/check_plan_syntax.py` that parses `PLAN.md` and validates each round's checkbox format.