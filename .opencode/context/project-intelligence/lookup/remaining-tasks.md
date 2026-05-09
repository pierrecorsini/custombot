<!-- Context: project-intelligence/lookup/remaining-tasks | Priority: medium | Version: 4.0 | Updated: 2026-05-08 -->

# Remaining Tasks

> Unchecked items from Round 18 PLAN.md — pending implementation roadmap.
> All Round 17 items completed. 7 of 17 Round 18 items completed; 8 remain (1 resilience, 2 security, 5 testing, 3 DX, 2 observability).

## Error Handling & Resilience (1 remaining)

- [ ] Emit `scheduled_task_failed` event when `process_scheduled()` encounters an exception — monitoring subscribers relying on event stream have a blind spot for failed scheduled tasks; emit event with `chat_id`, `error_type`, `error_message`

## Security (2 remaining)

- [ ] Validate `IncomingMessage.correlation_id` length and format — add `MAX_CORRELATION_ID_LENGTH` constant (e.g. 128 chars) and truncate with warning in message validator; prevents arbitrarily long strings in logs, event data, OTel spans
- [ ] Add audit log entry for low-confidence injection detections in `process_scheduled` — high-confidence already emits audit_log, but low-confidence only logs warning; add `audit_log("scheduled_prompt_injection_flagged", ...)` for observability parity

## Testing (5 remaining)

- [ ] Add test for `process_scheduled` lock-release on LLM timeout — simulate hung LLM call beyond `per_chat_timeout`, verify lock released, subsequent messages processed, `scheduled_task_failed` event emitted
- [ ] Add test for `_safe_call` with `BaseException`-raising handler — subscribe handler raising `SystemExit(1)`, verify bus does NOT crash; verify `KeyboardInterrupt` still propagates
- [ ] Add test for `EventBus._emission_counts` unbounded growth — emit N unique event names exceeding `max_tracked_event_names` cap, verify LRU eviction caps dict size
- [ ] Add integration test for `DeduplicationService` concurrent flush + check — N concurrent `record_outbound()` coroutines, then `check_outbound_duplicate()` verifies all recorded entries visible
- [ ] Add Hypothesis property-based tests for `SlidingWindowTracker.check_only` + `record` consistency — adversarial `(window_size, max_limit, timestamp_sequence)` tuples

## Developer Experience (3 remaining)

- [ ] Expand mypy strict coverage to `src.llm.*` — LLM client, error classifier, provider modules; follow non-blocking rollout pattern
- [ ] Add `make test-coverage` Makefile target — HTML coverage report (`--cov-report=html`) for visual gap exploration
- [ ] Incrementally reduce `PLR0913` violations — 63 remaining; extract typed dataclasses for 5+ argument signatures, prioritizing hot-path modules

## Observability (2 remaining)

- [ ] Track per-chat ReAct loop iteration count distribution — bounded top-N tracker (top 50 chats by iteration count) exposed in health endpoint
- [ ] Add `SkillRegistry` cache hit/miss metrics — expose cache invalidation count and current cache size in performance snapshot

## Completed Since Last Update (Round 18 — 9 items + Round 17 — 10 items)

### Round 18 (9 completed)
- [x] NullDedupService — eliminates 8+ `if self._dedup` guards across `_bot.py`
- [x] `correlation_id_scope()` context manager — auto clear on all exit paths
- [x] Bounded `_emission_counts` + `_handler_invocation_counts` — `max_tracked_event_names` LRU cap
- [x] `_safe_mode_lock` moved to `BaseChannel.__init__` instance — no module-level mutable state
- [x] Cached `SkillRegistry.tool_definitions` with invalidation on skill load
- [x] Pooled `xxhash.xxh64()` hasher instances via `reset()` in DeduplicationService
- [x] Single-hash `check_and_record_outbound()` replaces double-hash two-phase callers
- [x] `process_scheduled()` wrapped in `asyncio.wait_for(per_chat_timeout)` — stuck LLM no longer blocks chat
- [x] `_safe_call` hardened against `BaseException` from subscriber handlers

### Round 17 (10 completed)
- [x] Block high-confidence injection in `process_scheduled` — confidence >= 0.8 blocks execution
- [x] InstructionLoader file-size cap — `MAX_INSTRUCTION_FILE_SIZE` 1 MiB
- [x] HandleMessageMiddleware outbound tracking test
- [x] `_compact_chats` marker atomicity test
- [x] ErrorHandlerMiddleware error-reply rate limiting test
- [x] SlidingWindowTracker Hypothesis property-based tests
- [x] `make benchmark` Makefile target
- [x] HandleMessageMiddleware design decision documented in docstring
- [x] Rate-tracker memory usage exposed in `EventBus.get_metrics()`
- [x] Structured config hot-reload outcome log per component

## Codebase References

- `src/bot/_bot.py` — `handle_message`, `_handle_message_inner`, `process_scheduled`
- `src/core/event_bus.py` — `_safe_call`, `_emission_counts`, bounded trackers
- `src/core/dedup.py` — `NullDedupService`, `check_and_record_outbound`
- `src/channels/message_validator.py` — `correlation_id` validation target
- `src/monitoring/performance.py` — iteration count distribution target

## Related Files

- `lookup/completed-sessions.md` — Round 14–18 completed deliverables
- `lookup/decisions-log.md` — Architecture decisions for completed items
