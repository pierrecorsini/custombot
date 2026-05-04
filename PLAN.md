# PLAN.md — Improvement Tasks

> Auto-generated from senior code review on 2026-05-04.
> All 219 previous items completed. This is a fresh slate of new improvements.

---

## Architecture & Code Quality

- [x] Extract `_load_instruction()` helper from `Bot` into `InstructionLoader` — currently the bot has a private `_load_instruction()` method that duplicates concerns already owned by `InstructionLoader`; consolidate so the loader is the single source of truth for instruction file resolution and caching.
- [ ] Remove `RoutingRule` frozen-dataclass workarounds — `RoutingRule` uses `object.__setattr__()` in `__post_init__` and `_ensure_compiled()` to mutate a frozen dataclass; refactor to a regular dataclass or a builder pattern that computes compiled patterns at construction time, eliminating the fragile `object.__setattr__` calls.
- [ ] Eliminate lazy `SkillAuditLogger` initialization in `ToolExecutor` — the `_audit_logger` field is a `Path | SkillAuditLogger | None` tri-state that lazily initializes on first audit call; replace with explicit initialization in `__init__` or a factory method to simplify the type and avoid runtime type-checking on every audit call.
- [ ] Consolidate duplicate error-emission boilerplate — `Application._on_message`, `Bot.handle_message`, and `Bot._deliver_response` all contain nearly identical `try/except` blocks for emitting `EVENT_ERROR_OCCURRED` events; extract a shared `emit_error_event()` helper to reduce duplication.
- [ ] Type-annotate `Database` return types consistently — `save_message` and `save_messages_batch` return `str`/`list[str]` but callers like `_deliver_response` ignore the return; make the API contract explicit with ` -> str` annotations and ensure callers handle or intentionally discard returns.

## Performance Optimization

- [ ] Avoid redundant `asyncio.to_thread()` for `_seed_instruction_templates` — `Database.connect()` always offloads seeding to a thread even when the template directory is empty; add an early `is_dir()` check before creating the thread task to avoid event-loop overhead on every startup.
- [ ] Pre-compute `_match_impl` wildcard shortcut for single-rule routing — when only one routing rule exists (common for simple deployments), the channel-index lookup, cache check, and priority merge are all unnecessary overhead; add a fast path that directly evaluates the single rule.
- [ ] Batch `save_message` calls in `_prepare_turn` — `Bot._prepare_turn()` makes separate `upsert_chat()` and `save_message()` calls that each acquire locks and debounce; combine into a single write transaction to reduce lock contention and I/O syscalls.
- [ ] Replace `perf_counter()` calls with monotonic clock in hot paths — `_handle_message_inner` and `_react_iteration` both call `time.perf_counter()` multiple times; cache the start time in a context variable and derive elapsed from it to reduce syscall overhead.
- [ ] Add `__slots__` to `DeduplicationService` — the service already declares `__slots__` but uses `_outbound_buffer` as a plain list that grows unbounded during burst sends; add a max-length cap with overflow logging to prevent memory spikes.

## Error Handling & Resilience

- [ ] Handle `BaseException` (not just `Exception`) in `_shutdown_cleanup` — `Application._shutdown_cleanup()` catches `Exception` from `perform_shutdown()` but `asyncio.TimeoutError` in Python 3.11+ is a subclass of `BaseException` in some edge cases; ensure the timeout wrapper propagates cleanly.
- [ ] Add structured retry for `_save_chats` on `OSError` — `Database._save_chats()` delegates to `_guarded_write` which retries on `OSError`, but the debounced save in `upsert_chat` can silently lose writes if the process crashes between debounce intervals; add a final flush on `close()` (already present, but verify it handles partial writes).
- [ ] Protect `TaskScheduler` main loop against `BaseException` — the scheduler loop (line ~938) catches `Exception` but not `BaseException` (e.g. `KeyboardInterrupt`, `SystemExit`); wrap the entire loop in a `try/finally` that sets `_running = False` unconditionally.
- [ ] Add generation-conflict recovery for `_deliver_response` — when a generation conflict is detected, the current code logs and proceeds but notes that "tool-log entries may interleave"; implement a re-read + merge strategy to guarantee consistent JSONL order.
- [ ] Emit `message_dropped` event for rate-limited messages — `Bot.handle_message()` returns `None` silently when rate-limited; emit a `message_dropped` event with `reason="rate_limited"` for observability parity with other rejection paths (no routing match, too long, etc.).

## Test Coverage & Quality

- [ ] Add property-based tests for `RoutingEngine._match_impl` — the routing matching logic has complex interactions between sender/recipient/channel/content patterns, `fromMe`/`toMe` flags, and priority ordering; use `hypothesis` to generate random rule sets and message contexts to verify match correctness.
- [ ] Add integration tests for config hot-reload destructive-field warnings — `ConfigWatcher` classifies fields as safe vs destructive but no test verifies that changing `llm.model` logs a warning without applying the change; add a test that mutates destructive fields and asserts warning logs.
- [ ] Test `Bot.process_scheduled` HMAC verification failure path — the HMAC verification in `process_scheduled` has two branches (missing HMAC, invalid HMAC) but existing tests may only cover the happy path; add explicit tests for both rejection branches.
- [ ] Add chaos test for concurrent `DeduplicationService` operations — the dedup service uses `BoundedOrderedDict` with batch flushing but has no concurrent-access test; verify that interleaved `check_and_record_outbound` / `flush_outbound_batch` calls don't lose entries.
- [ ] Add test for `Database.warm_file_handles` — the file-handle pre-warming method has no test coverage; verify that it opens handles for existing JSONL files and gracefully skips non-existent ones.
- [ ] Increase mypy strict coverage beyond `src/bot/` — currently only `src/bot/` is under strict mypy; extend to `src/core/` and `src/config/` to catch type errors in the middleware pipeline and config validation paths.
- [ ] Add regression tests for `PERF401` violations — there are 16 existing `PERF401` (manual-list-append) violations suppressed in `pyproject.toml`; add tests that verify the list-append patterns produce correct results, then refactor them to list comprehensions.

## Security Hardening

- [ ] Add `message_dropped` event emission for ACL-rejected messages — `Bot.handle_message()` silently returns `None` when `acl_passed` is False without emitting any event; add event emission for security auditing of rejected messages.
- [ ] Rate-limit `_send_error_reply` to prevent error-message amplification — `ErrorHandlerMiddleware._send_error_reply()` sends a message for every caught exception; if an attacker triggers repeated errors, the bot amplifies traffic; add per-chat rate limiting for error replies.
- [ ] Validate scheduler task `prompt` content for injection — `Bot.process_scheduled()` applies `sanitize_user_input()` and `detect_injection()` but only logs a warning when injection is detected; the prompt is still sent to the LLM; reject or truncate high-confidence injection detections instead of just logging.
- [ ] Add file-size cap for instruction file loading — `InstructionLoader` reads instruction `.md` files without size validation; a compromised or accidentally huge instruction file could exhaust memory; add a max-file-size check before reading.

## Observability & Monitoring

- [ ] Add Prometheus histogram for routing-match latency — the routing engine has sophisticated caching and channel indexing but no metric for how long matching takes; add a histogram to detect performance regressions as rule sets grow.
- [ ] Track per-skill error rate in `PerformanceMetrics` — `track_skill_error()` increments a counter but doesn't expose error-rate-per-skill as a Prometheus gauge; add a gauge so operators can identify flaky skills without parsing logs.
- [ ] Add structured `startup_completed` event with config hash — the startup event includes component count and duration but not a config hash; add a hash of the effective config to detect config drift across restarts.
- [ ] Log outbound dedup stats periodically — `DedupStats` are exposed via the health endpoint but never logged; add periodic logging (every N messages or T seconds) so dedup effectiveness is visible in log aggregation.

## Developer Experience

- [ ] Remove `from src.llm import LLMClient` backward-compat re-exports — the `src/llm/__init__.py` re-exports `LLMClient` for backward compatibility, but all internal imports should now use `from src.llm import LLMProvider`; audit and remove stale re-exports.
- [ ] Add `--dry-run` flag to config validation — `main.py diagnose` validates config but doesn't support a dry-run that shows what would change on hot-reload; add a flag that prints the diff between current and new config without applying it.
- [ ] Consolidate `BoundedOrderedDict` TTL handling — `BoundedOrderedDict` is imported from `src/utils/__init__.py` but the implementation lives in a separate module; verify the TTL expiry behavior is consistent across all consumers (dedup cache, routing match cache, inbound dedup cache).
- [ ] Document the generation-counter write-conflict protocol — `_deliver_response` uses a generation counter for optimistic concurrency but the protocol is only documented in inline comments; add a docstring section to `Database` explaining the counter lifecycle and invariants.
