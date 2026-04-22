# PLAN.md — CustomBot Improvement Roadmap

Generated from a senior codebase review on 2026-04-18.

---

## Phase 9 — Senior Review (2026-04-21)

Generated from a ninth-pass codebase audit covering runtime correctness,
architectural debt, observability gaps, and production hardening not
addressed in Phases 1–8.

---

### Refactoring

- [x] **Extract `_assemble_context()` into its own `ContextAssembler` class** — `_assemble_context()` in `bot.py` orchestrates 4 async reads (memory, agents_md, project_context, topic_cache) then calls `build_context()`. Both `_build_turn_context()` and `process_scheduled()` call it. However, the assembled context has no typed return — it returns `list[ChatMessage]` which doesn't carry the routing rule, instruction content, or channel prompt that were also resolved. Introduce a `ContextResult` dataclass (messages, instruction_used, rule_id, channel_prompt) returned by a stateless `ContextAssembler` service. This makes the context-assembly phase fully testable in isolation and gives downstream code (metrics, logging, audit) access to the full resolution metadata without re-deriving it. (`src/bot.py:752-778`, new file `src/core/context_assembler.py`)

- [x] **Move `_handle_topic_meta()` and `TopicCache` interaction into the `ContextAssembler`** — `_handle_topic_meta()` is called from both `_process()` and `process_scheduled()` with identical logic (check `topic_changed`, write summary, log). By moving this into `ContextAssembler`, the topic lifecycle (read-before-assembly, write-after-response) is encapsulated in one place. The bot would call `assembler.finalize_turn(chat_id, raw_response)` instead of manually calling `_handle_topic_meta()` and `parse_meta()`. (`src/bot.py:883-886, 698-701, 1172-1181`)

- [x] **Extract `PromptSkill` LLM injection into a builder pattern** — `_build_bot()` iterates all skills to find `PromptSkill` instances and injects the LLM client via `set_llm()`. This is fragile: any new skill type that needs the LLM client must be manually added to this loop. Replace with a `SkillWiring` protocol (`needs_llm()`, `wire_llm(client)`) or a post-init hook on `BaseSkill` that the registry calls automatically during `load_builtins()`, so skill authors don't need builder-level changes. (`src/builder.py:236-240`, `src/skills/base.py`)

- [x] **Consolidate `OutboundDedupCache` and the per-chat dedup in `handle_message()` into a single dedup strategy** — There are now two dedup mechanisms: (1) `Database.message_exists()` in `handle_message()` for inbound dedup, and (2) `OutboundDedupCache` in `TaskScheduler` for outbound scheduled-task dedup. They operate independently, use different key schemes (message_id vs SHA-256 hash), and have different TTL semantics. Consolidate into a unified `DeduplicationService` that supports both inbound message-id and outbound content-hash strategies, with configurable TTLs, and expose dedup stats in `/metrics`. (`src/bot.py:468-479`, `src/scheduler.py:44-111`)

### Performance Optimization

- [x] **Parallelize the 4 async reads in `_assemble_context()`** — `_assemble_context()` calls `read_memory()`, `read_agents_md()`, `_get_project_context()`, and `_topic_cache.read()` sequentially (4 sequential await points). These are independent reads from different data sources (filesystem, SQLite, filesystem, filesystem). Use `asyncio.gather()` to execute all 4 concurrently, reducing context-assembly latency from sum(read_times) to max(read_times) — potentially a 3-4x speedup for context-heavy chats. (`src/bot.py:764-768`)

- [x] **Add conversation-history compression for long-running chats** — `build_context()` loads up to `DEFAULT_MEMORY_MAX_HISTORY` messages and trims by token budget. For a very active chat with thousands of messages, the JSONL file grows unbounded and `_read_file_lines()` does a reverse-seek that reads increasing amounts. When the topic cache has a summary, only `_REDUCED_HISTORY_FRACTION` messages are fetched, but the full JSONL still exists on disk. Add an automatic compression step: when a chat's JSONL exceeds a threshold (e.g., 5000 lines), summarize the oldest N messages into a single system message and truncate the file, keeping the last M messages intact. This reduces disk I/O and context-build latency for long-lived conversations. (`src/core/context_builder.py`, `src/db/db.py`)

- [x] **Lazy-load tool definitions instead of building them on every ReAct iteration** — `_react_loop()` calls `self._skills.tool_definitions` at the top, which rebuilds the OpenAI tool schema list from all registered skills. For a bot with 15+ skills, this rebuilds 15 Pydantic models on every iteration (up to `max_tool_iterations` times per message). Cache the tool definitions on the `SkillRegistry` and invalidate only when skills are added/removed (which only happens at startup and config reload). (`src/bot.py:871, 663`, `src/skills/__init__.py`)

### Error Handling & Resilience

- [x] **Add `ChatMessage` validation in `_process_tool_calls()` buffered_persist** — `_process_tool_calls()` appends dicts to `buffered_persist` with hardcoded keys like `{"role": "tool", "content": content, "name": tool_entry.name}`. If a skill returns a very long result (e.g., `file_read` on a 100KB file), the entire result is persisted to the JSONL conversation history, bloating disk usage and slowing future context builds. Add a `MAX_TOOL_RESULT_PERSIST_LENGTH` constant (e.g., 10_000 chars) and truncate results in the buffered_persist dict with a `[truncated, full length: N]` suffix. The full result is still available in the in-memory `messages` list for the current ReAct iteration. (`src/bot.py:1107-1109`)

- [x] **Handle `chat_stream()` partial failure leaving buffered text undelivered** — In `chat_stream()`, if the stream breaks mid-way (network failure, provider error), the accumulated `buffered_chunk` may contain text that was never flushed to `on_chunk`. The except block catches and classifies the error, but the partial text is silently lost. Add a `finally` block that flushes any remaining `buffered_chunk` via a best-effort `on_chunk` call (wrapped in its own try/except) so the user sees the partial response rather than nothing. (`src/llm.py:475-477, 532-545`)

- [x] **Add database write conflict detection for concurrent scheduled and user messages** — `process_scheduled()` and `handle_message()` both write to the same chat's JSONL file, serialized only by the per-chat lock. If both produce responses within the same lock acquisition window (e.g., a scheduled task finishes while the user sends a new message), the conversation history can have interleaved user/assistant turns that confuse the LLM. Add a generation counter to each chat's in-memory state: when `save_messages_batch()` is called, verify that the chat's generation hasn't changed since the context was built. If it has, re-read the latest history and rebuild context before persisting. (`src/bot.py:527-593, 629-748`, `src/db/db.py`)

- [x] **Guard `_process_tool_calls()` against `TaskGroup` exception propagating tool-call ordering issues** — `_process_tool_calls()` uses `asyncio.TaskGroup` which, by design, cancels all sibling tasks if any raises `BaseException` (not just `Exception`). If one tool call triggers a `KeyboardInterrupt` or `SystemExit`, all other in-flight tool executions are cancelled, and their results are lost. Wrap the `TaskGroup` in a try/except that catches `BaseException` and returns whatever partial results are available (from completed tasks) rather than losing them entirely. (`src/bot.py:1089-1096`)

### Security

- [x] **Add prompt-injection detection for scheduled task prompts** — `process_scheduled()` accepts a `prompt` string from `tasks.json` and injects it directly into the LLM context without any injection detection. If an attacker gains write access to `tasks.json` (or if a compromised LLM creates a malicious scheduled task via the `task_scheduler` skill), the prompt could contain injection attempts that bypass the normal message pipeline's safeguards. Run `sanitize_user_input()` on scheduled prompts before appending them to the message list, consistent with how incoming messages are sanitized. (`src/bot.py:652`, `src/security/prompt_injection.py`)

- [x] **Add `workspace/` path traversal guard for skill `workspace_dir` parameter** — Skills receive `workspace_dir` as an argument from `_execute_tool_call()`. While individual skills like `shell.py` have their own path sanitization, the `workspace_dir` itself is constructed from `self._memory.ensure_workspace(chat_id)`. A malicious `chat_id` (e.g., `../../etc`) that bypasses sanitization would propagate to all skill executions. Add a defensive assertion in `_execute_tool_call()` that verifies `workspace_dir.resolve().is_relative_to(WORKSPACE_DIR.resolve())` before executing any skill, as a belt-and-suspenders guard. (`src/bot.py:1134-1138`, `src/core/tool_executor.py:214-217`)

- [x] **Enforce HMAC timing-safe comparison in health server authentication** — The health server's HMAC verification in `_verify_hmac()` likely uses `hmac.compare_digest()` (constant-time), but if the secret or timestamp parsing has edge cases (empty signature, malformed timestamp), the error path may leak timing information about the expected format. Audit all comparison paths to ensure they are constant-time and add explicit length-normalization of the compared values before the comparison. (`src/health/server.py`)

### Observability & Monitoring

- [x] **Add per-chat token usage tracking and cost estimation** — `TokenUsage` accumulates global token counts (prompt, completion, total) but has no per-chat breakdown. For a multi-tenant bot serving different users, operators cannot identify which chats consume the most tokens/cost. Add a bounded LRU per-chat token accumulator (`LRUDict` keyed by chat_id, tracking prompt/completion/total per chat) and expose `custombot_chat_prompt_tokens` and `custombot_chat_completion_tokens` as top-N Prometheus metrics. (`src/llm.py:132-158`, `src/monitoring/performance.py`)

- [x] **Track and expose LLM response latency percentiles in Prometheus metrics** — `PerformanceMetrics` tracks `_llm_latencies` and exposes `custombot_llm_latency_milliseconds` as a simple counter/average. Prometheus histograms are the standard way to express latency distributions (p50, p95, p99). Replace the simple average with a Prometheus histogram bucket approach (even in the custom text format) so operators can set alerts on p95 latency degradation. (`src/monitoring/performance.py`, `src/health/server.py`)

- [x] **Add structured event emission to the EventBus from core components** — The `EventBus` is implemented and wired but no core components actually emit events. `Bot._process()`, `ToolExecutor.execute()`, and `Application._on_message()` should emit events (`message_received`, `skill_executed`, `response_sent`) so that plugins and extensions can subscribe without modifying core classes. This was the stated purpose of the EventBus but remains unused. (`src/bot.py`, `src/core/tool_executor.py`, `src/core/event_bus.py`)

- [x] **Add startup banner with QR-code URL for remote headless deployment** — When the bot starts in a Docker container or headless environment, the QR code is printed to stdout but operators SSH'd into the machine may not see it. Add a structured log line (or `/health` field) with the QR code data as a base64-encoded `data:image/png` URL that monitoring tools or dashboards can display. Also log the connection status (waiting-for-QR / connected / disconnected) in the `/ready` endpoint. (`src/channels/neonize_backend.py`, `src/health/server.py`)

### Test Coverage

- [x] **Add test for `_assemble_context()` parallel-read correctness** — Once the 4 async reads are parallelized via `asyncio.gather()`, add a test that verifies: (a) all 4 data sources are correctly read, (b) results are identical to sequential execution, (c) a failure in one read doesn't cancel the others (use `return_exceptions=True`), (d) the order of returned results matches the expected (memory, agents_md, project_context, topic_summary). (`tests/unit/test_bot.py`)

- [x] **Add test for conversation-history compression preserving recent messages** — When the JSONL compression feature is implemented, add a test verifying: (a) messages beyond the threshold are summarized, (b) the most recent N messages are preserved verbatim, (c) the summary is injected as a system message with correct metadata, (d) the compressed JSONL file is valid and parseable. (`tests/unit/test_context_builder.py`)

- [x] **Add test for `EventBus` handler error isolation** — The EventBus uses `_safe_call()` to isolate handler errors, but there is no test verifying: (a) a failing handler doesn't prevent other handlers from executing, (b) a failing handler's exception is logged with the correct event metadata, (c) `emit()` returns normally even when all handlers fail. Add a test with multiple handlers where some raise exceptions. (`tests/unit/test_event_bus.py`, new or existing)

- [x] **Add test for `ToolExecutor` result truncation in buffered_persist** — Verify that when a skill returns a result exceeding `MAX_TOOL_RESULT_PERSIST_LENGTH`, the buffered_persist dict contains a truncated version with the correct suffix, while the in-memory `messages` list retains the full result for the current ReAct iteration. (`tests/unit/test_tool_executor.py`)

- [x] **Add test for `chat_stream()` partial delivery on stream failure** — Simulate a stream that delivers 3 chunks then raises a network error. Verify that: (a) the `on_chunk` callback received the chunks that were successfully delivered before the error, (b) the error is classified and raised as an `LLMError`, (c) any buffered text is flushed in the finally block. (`tests/unit/test_llm.py`)

- [x] **Add test for scheduled task prompt injection detection** — Create a scheduled task with a prompt containing common injection patterns (e.g., "Ignore all previous instructions..."). Verify that `process_scheduled()` sanitizes or flags the prompt before passing it to the LLM, consistent with how incoming messages are handled. (`tests/unit/test_bot.py`)

- [x] **Add test for `Database.save_messages_batch()` atomicity** — `save_messages_batch()` writes multiple messages to the JSONL file. Add a test that verifies: (a) if the write fails mid-way, no partial messages are persisted, (b) the message index is updated only after the full batch succeeds, (c) concurrent calls to `save_messages_batch()` for the same chat are serialized correctly. (`tests/unit/test_db.py`)

---

## Phase 10 — Senior Review (2026-04-22)

Generated from a tenth-pass codebase audit focusing on critical bugs
introduced during Phases 8–9 refactoring, dead code paths, data integrity
gaps, and operational resilience not addressed in Phases 1–9.

---

### Critical Bugs

- [x] **Fix indentation bug in `Bot.handle_message()` — main processing path is dead code** — Lines 548–616 in `bot.py` (the `async with self._chat_locks.acquire` block, message queue enqueue, `_process()` call, try/except/finally) are indented inside the `if not rate_result.allowed:` block at line 532. This means the entire processing pipeline (workspace creation, context assembly, ReAct loop, message persistence) only executes when a rate-limited message is sent AND the channel send at line 541 returns — which it doesn't because it's followed by `clear_correlation_id()` and `return None`. Normal, non-rate-limited messages never enter this block and fall through to the end of the method with no return value. De-indent lines 548–616 so they execute after the rate-limit check passes, restoring the normal message processing flow. (`src/bot.py:526-616`)

- [x] **Fix duplicate `RateLimiter()` instantiation in `Bot.__init__`** — `self._rate_limiter` is assigned twice on consecutive lines (189 and 190), creating an orphaned `RateLimiter` instance that is immediately garbage-collected. Remove the duplicate line. (`src/bot.py:189-190`)

- [x] **Fix `generation` variable undefined in `Bot._process()`** — `generation` is captured at line 554 via `self._db.get_generation(msg.chat_id)` inside the incorrectly-indented block. When the indentation bug is fixed, `generation` must be moved to `_process()` or `_build_turn_context()` so it is available at line 927 where `check_generation()` is called. Without this, `NameError` will be raised at runtime, or the write-conflict detection is silently skipped. (`src/bot.py:554, 927`)

### Error Handling & Resilience

- [x] **Fix potential `UnboundLocalError` in `LLMClient.chat_stream()` finally block** — If an exception occurs very early in the `try` block (e.g., `self._client.chat.completions.create` raises before `buffered_chunk` is assigned), the `finally` block at line 598 references `buffered_chunk` which may be undefined. Initialize `buffered_chunk = ""` before the `try` block (it is currently only initialized inside the `try` at line 465). (`src/llm.py:462-603`)

- [x] **Guard `SkillRegistry.wire_llm_clients()` against per-skill wiring failures** — If a single skill's `wire_llm()` raises an exception, the entire loop terminates and all subsequent skills remain unwired. Wrap each `skill.wire_llm(llm)` call in a try/except that logs the error and continues, so one broken skill doesn't prevent others from receiving the LLM client. (`src/skills/__init__.py:83-91`)

- [x] **Add graceful degradation for `DeduplicationService` when DB is unavailable** — `is_inbound_duplicate()` awaits `self._db.message_exists()` which can raise `DatabaseError` on disk I/O failure. Currently this propagates to `handle_message()` and crashes message processing. Catch the exception, log a warning, and return `False` (allow the message through) so the bot remains functional during transient DB outages. (`src/core/dedup.py:82-93`)

### Refactoring

- [x] **Extract message processing pipeline from `handle_message()` into a dedicated method** — After the indentation fix, `handle_message()` will be ~160 lines with deep nesting (validation → dedup → rate limit → lock → enqueue → try/except/finally). Extract the core processing block (acquire lock → enqueue → process → track metrics → complete queue) into `_handle_message_inner()` to keep `handle_message()` as a thin validation + orchestration wrapper. This makes the processing path easier to test in isolation. (`src/bot.py:453-616`)

- [x] **Move `import asyncio` to module-level in `llm.py`** — `import asyncio` appears inside the `try` block of `chat_stream()` at line 471 as a late addition. Move it to the module-level imports (which already has other stdlib imports) for consistency and minor import-caching performance. (`src/llm.py:471`)

- [x] **Eliminate double emission of per-chat token metrics in `HealthServer._handle_metrics`** — `_build_prometheus_output()` already receives and emits `per_chat_tokens` (lines 349–369). Then `_handle_metrics` additionally iterates `self._token_usage.get_top_chats()` and emits the same metrics again (lines 1182–1201). Remove the duplicate loop in `_handle_metrics` so each per-chat metric appears once. (`src/health/server.py:1182-1201`)

### Performance Optimization

- [x] **Add TTL-based cleanup for `_chat_generations` dict in `Database`** — `self._chat_generations` grows without bound as new chat IDs are encountered. For long-running bots with thousands of chats, this dict consumes increasing memory. Add a periodic sweep (e.g., evict entries older than 24 hours since last write) or cap the dict size with LRU eviction similar to `_message_id_index`. (`src/db/db.py:285`)

- [x] **Replace `scheduler.py` synchronous `_persist_sync()` with async alternative** — `_persist_sync()` performs blocking file I/O (`path.write_text()`) which can stall the event loop when called from `add_task()`. The sync variant exists for backward compatibility but skills execute in an async context. Replace all call sites with `_persist_async()` or wrap `_persist_sync()` in `asyncio.to_thread()`. (`src/scheduler.py:235-242`)

### Security

- [x] **Add `name` field sanitization in `Database._build_message_record()`** — The `name` parameter (sender name or tool name) is persisted directly to JSONL without sanitization. While not as dangerous as user content, a malicious or malformed sender name could contain control characters or excessively long strings. Truncate `name` to a reasonable length (e.g., 200 chars) and strip control characters before persisting. (`src/db/db.py:871-916`)

- [x] **Add `HEALTH_HMAC_SECRET` masking in health check logs** — When HMAC authentication fails, the `Authorization` header value is logged at DEBUG level by aiohttp. If the secret is accidentally sent in the wrong field, it could appear in logs. Add a log filter or explicit header stripping in the middleware to ensure the HMAC token is never logged in full. (`src/health/server.py:218-240`)

### Observability & Monitoring

- [x] **Add structured event emission from `Application._on_message()` with correlation ID propagation** — The message pipeline (`_on_message` → `pipeline.execute()`) does not emit an `error_occurred` event when the pipeline raises. Add a try/except wrapper that emits `Event(name="error_occurred")` with the exception details and correlation ID, so error-monitoring subscribers are notified of pipeline failures. (`src/app.py:401-409`)

- [x] **Add per-skill timeout histogram to Prometheus metrics** — Each skill declares a `timeout_seconds` attribute, but there is no metric tracking how close skill executions get to their timeout. Add a gauge metric (`custombot_skill_timeout_ratio`) that tracks the ratio of actual execution time to declared timeout, so operators can identify skills that are consistently near their timeout limit and need either optimization or a higher timeout. (`src/monitoring/performance.py`, `src/health/server.py`)

### Test Coverage

- [x] **Add regression test for `handle_message()` indentation — verify normal messages reach `_process()`** — Create a test that sends a valid, non-rate-limited message through `handle_message()` and verifies that: (a) `_process()` is called, (b) the chat lock is acquired and released, (c) the message queue is updated, (d) metrics are tracked. This guards against future re-introduction of the indentation bug. (`tests/unit/test_bot.py`)

- [x] **Add test for `chat_stream()` early-exception handling** — Simulate a stream that raises immediately on `create()` (before any chunks). Verify that: (a) no `UnboundLocalError` is raised from the `finally` block, (b) the error is classified and raised as an `LLMError`, (c) the circuit breaker records a failure. (`tests/unit/test_llm.py`)

- [x] **Add test for `wire_llm_clients()` resilience — one failing skill doesn't break others** — Register 3 skills where the middle one's `wire_llm()` raises an exception. Verify that: (a) the other 2 skills still receive the LLM client, (b) the error is logged, (c) no exception propagates from `wire_llm_clients()`. (`tests/unit/test_builder.py`)

- [x] **Add test for `DeduplicationService` graceful degradation on DB failure** — Mock the database to raise `DatabaseError` on `message_exists()`. Verify that: (a) `is_inbound_duplicate()` returns `False` (allowing the message through), (b) a warning is logged, (c) no exception propagates. (`tests/unit/test_dedup.py`)

- [x] **Add test for `_chat_generations` bounded growth** — Add test that verifies: (a) `get_generation()` returns 0 for unknown chat IDs, (b) `_bump_generation()` increments correctly, (c) after exceeding a maximum size, oldest entries are evicted. This prevents silent memory leak in long-running bots. (`tests/unit/test_db.py`)

- [x] **Add integration test for end-to-end scheduled task with write-conflict detection** — Send a user message and trigger a scheduled task for the same chat concurrently. Verify that: (a) both responses are persisted without corruption, (b) the generation check logs a warning when a conflict is detected, (c) the conversation history remains valid and parseable. (`tests/integration/test_scheduled_pipeline.py`)

---

## Phase 11 — Senior Review (2026-04-22)

Generated from an eleventh-pass codebase audit focusing on architectural
debt, resilience gaps, security hardening, and test coverage not
addressed in Phases 1–10.

---

### Refactoring

- [x] **Extract `Application._startup()` into a declarative component registry** — `_startup()` is ~80 lines with 10 sequential initialization blocks following the same pattern (`_log_component_init` → create → `_log_component_ready` → `progress.advance`). Extract into a `StartupOrchestrator` that accepts a list of `ComponentSpec(name, factory, depends_on)` and runs them in dependency order. This makes the startup graph explicit, testable, and easily extensible without modifying `_startup()` directly. (`src/app.py:191-272`, new file `src/core/startup.py`)

- [x] **Replace `isinstance` channel-type checks with protocol-based dispatch** — `_init_config_watcher()` uses `isinstance(self._channel, WhatsAppChannel)` to select the config applier. Adding a new channel type requires modifying `Application`. Replace with a `SupportsConfigHotReload` protocol or a `get_config_applier()` method on `BaseChannel`, so the dispatch is driven by the channel itself rather than the application. (`src/app.py:294-317`, `src/channels/base.py`)

- [x] **Consolidate the three `RateLimiter` instances in `Bot` and `ToolExecutor`** — `Bot.__init__` creates `self._rate_limiter` (skill execution) and `self._chat_rate_limiter` (message rate) separately, while `ToolExecutor` also receives a `rate_limiter`. Document why separate instances are needed (different configs? different eviction?) or unify into a single `RateLimiter` with namespaced configs, reducing the total objects and making rate-limit policy visible in one place. (`src/bot.py:189-191`, `src/core/tool_executor.py`)

- [x] **Extract `_react_loop` max-iteration message formatting into a helper** — The max-iteration warning block (lines 1053-1064 in `bot.py`) builds an informative message with tool summary formatting inline. Extract into `_format_max_iterations_message(iterations, tool_log)` to keep `_react_loop` focused on loop control and make the formatting independently testable. (`src/bot.py:1045-1065`)

- [x] **Move `_wire_scheduler` double-set of `on_trigger` into a single authoritative wiring point** — `Application._wire_scheduler()` calls `scheduler.set_on_trigger()` at line 369, but `_init_scheduler()` already set it at line 283 during construction. The second call at line 369 adds `channel=channel` to the lambda — verify this isn't a race and consolidate into one wiring call. (`src/app.py:283, 358-372`)

### Performance Optimization

- [x] **Configure HTTPx connection pooling on the OpenAI client** — `LLMClient` creates an `AsyncOpenAI` client but doesn't set `max_connections` or `max_keepalive_connections` on the underlying `httpx.AsyncClient`. Under high concurrency (many concurrent chats hitting the LLM), connection establishment overhead adds latency. Configure explicit pool limits (e.g., `max_connections=20, max_keepalive_connections=10`) to reuse TCP connections. (`src/llm.py`)

- [x] **Implement invalidation-based tool-definitions cache on `SkillRegistry`** — `_react_loop()` calls `self._skills.tool_definitions` on every iteration, which rebuilds OpenAI tool schemas from all registered skills (15+ Pydantic model serializations per call). Add a cached property on `SkillRegistry` that invalidates only when skills are added/removed (which only happens at startup and config reload). (`src/bot.py:893, 704`, `src/skills/__init__.py`)

- [x] **Enrich compressed conversation summaries with vector embeddings** — When `compress_chat_history()` archives old messages, the summary is a static string like "1500 messages archived". Optionally embed the summary in `VectorMemory` so the `memory_recall` skill can semantically retrieve archived conversations instead of losing them to time-based truncation. This is a low-priority enhancement — the summary text is already informative, but vector-enrichment would enable semantic search over archived history. (`src/db/db.py:1285-1292`, `src/vector_memory.py`)

### Error Handling & Resilience

- [x] **Handle individual `asyncio.gather` failures in `ContextAssembler.assemble()`** — The 5 concurrent reads in `assemble()` (memory, agents_md, project_ctx, topic_cache, compressed_summary) use `asyncio.gather()` without `return_exceptions=True`. A single disk I/O failure (e.g., corrupted AGENTS.md) crashes the entire context assembly. Wrap with `return_exceptions=True` and handle each result individually: log the failure, substitute a sensible default, and proceed with the remaining context. (`src/core/context_assembler.py:101-113`)

- [ ] **Add circuit-breaker pattern for database write operations** — When the filesystem is degraded (disk full, NFS dropout), every DB write individually times out after `DEFAULT_DB_TIMEOUT` (10s). Under sustained failure, this creates a backlog of blocked coroutines starving the event loop. Add a lightweight write-circuit-breaker on `Database` that fast-fails writes when the last N consecutive writes all failed, preventing thundering-herd timeouts. (`src/db/db.py`, `src/utils/circuit_breaker.py`)

- [ ] **Handle `read_agents_md()` `FileNotFoundError` in `ContextAssembler`** — `Memory.read_agents_md()` raises `FileNotFoundError` if AGENTS.md doesn't exist (e.g., `ensure_workspace()` wasn't called yet for a new scheduled task). `ContextAssembler.assemble()` doesn't catch this — it propagates up and aborts the scheduled task. Catch and substitute the default agents content in the assembler. (`src/core/context_assembler.py:108`, `src/memory.py:395-417`)

- [ ] **Add retry with exponential backoff for transient LLM errors in `_react_loop`** — `_react_loop()` catches `LLMError` for circuit-breaker-open but re-raises all other LLM errors (rate-limit, timeout, server error). These are often transient. Add a limited retry (1-2 attempts with backoff) for retryable error codes before propagating, reducing message-processing failures from temporary provider issues. (`src/bot.py:984-1010`)

### Security

- [ ] **Add per-turn tool-call count limit** — `_process_tool_calls()` executes all requested tool calls in parallel with no cap on how many tools the LLM can request in a single turn. A confused or prompt-injected LLM could request 50+ concurrent tool calls, exhausting system resources (file handles, thread pool, memory). Add a `MAX_TOOL_CALLS_PER_TURN` constant (e.g., 10) and truncate/reject excessive requests with a warning to the LLM. (`src/bot.py:1067-1179`, `src/constants.py`)

- [ ] **Centralize `chat_id` validation at the `IncomingMessage` boundary** — While `db.py` has `_validate_chat_id()` and `sanitize_path_component()`, `chat_id` flows through `memory.py`, `workspace_integrity.py`, and `scheduler.py` with varying levels of sanitization. Add validation in `IncomingMessage.__post_init__()` or at the top of `handle_message()` to catch malicious chat IDs before they reach any filesystem operation, as a defense-in-depth layer. (`src/channels/base.py:38-88`, `src/bot.py:452-547`)

- [ ] **Add request size limits to health server middleware** — The health server has IP-based rate limiting but no request body or URL length limits. An attacker could send oversized requests to consume server memory. Add middleware to reject requests with bodies > 1KB or URL paths > 2KB — health endpoints only serve short GET requests. (`src/health/server.py`)

- [ ] **Audit and restrict environment variable reading in `RateLimiter.from_env()`** — `RateLimitConfig.from_env()` reads `RATE_LIMIT_CHAT_PER_MINUTES` and `RATE_LIMIT_EXPENSIVE_PER_MINUTES` from env vars without validating the range. A misconfigured env var (e.g., `RATE_LIMIT_CHAT_PER_MINUTES=999999`) effectively disables rate limiting. Add sensible bounds (min=1, max=100) and log the effective values at startup. (`src/rate_limiter.py:83-95`)

### Observability & Monitoring

- [ ] **Propagate correlation IDs through database operations** — Database operations in `db.py` log `chat_id` but don't include the correlation ID that the message pipeline sets via contextvars. Propagate the current correlation ID through to DB log statements for end-to-end request tracing in production logs. (`src/db/db.py`, `src/logging/logging_config.py`)

- [ ] **Add fixed-bucket histogram for LLM latency percentiles** — The `/metrics` endpoint exposes average LLM latency, but operators need percentiles (p50, p95, p99) for alerting. Implement a fixed-bucket histogram approach (e.g., buckets at 0.5s, 1s, 2s, 5s, 10s, 30s, 60s, 120s) in `PerformanceMetrics` and expose `custombot_llm_latency_bucket` in the Prometheus output. (`src/monitoring/performance.py`, `src/health/server.py`)

- [ ] **Add workspace disk-usage growth rate metric** — `WorkspaceMonitor` tracks total size but doesn't compute the derivative (MB/hour). Add a growth-rate computation (current_size - previous_size / elapsed_time) and expose it as `custombot_workspace_growth_mb_per_hour` so operators can detect sudden spikes (e.g., a runaway skill generating large files). (`src/monitoring/workspace_monitor.py`, `src/health/server.py`)

- [ ] **Add per-skill execution count and error-rate metrics** — `PerformanceMetrics` tracks per-skill latency but not execution count or error rate. Add counters (`custombot_skill_executions_total`, `custombot_skill_errors_total`) so operators can identify which skills are used most and which fail most often. (`src/monitoring/performance.py`, `src/core/tool_executor.py`)

### Test Coverage

- [ ] **Add integration test for config hot-reload applying changes to a running bot** — `ConfigWatcher` polls for changes and `ConfigChangeApplier` applies them, but there's no integration test verifying that a config change (e.g., changing `max_tool_iterations` from 10 to 5) takes effect on the next message without restart. (`tests/integration/test_application_lifecycle.py`)

- [ ] **Add chaos test for concurrent `save_messages_batch` and `compress_chat_history`** — Both operations acquire per-chat locks, but `compress_chat_history` is triggered asynchronously after writes (in `save_message` and `save_messages_batch`). There's a window where a write triggers compression, which then rewrites the file while another write is pending. Add a test that exercises this race: write a batch, then immediately write another batch, and verify no data loss. (`tests/unit/test_db.py`)

- [ ] **Add test for `Memory` mtime cache consistency after external file modification** — `Memory.read_memory()` uses mtime-based caching. If an external process (e.g., a skill modifying MEMORY.md) changes the file between reads, the cache should be invalidated. Add a test that modifies the file's content and mtime between reads and verifies the second read returns the updated content. (`tests/unit/test_memory.py`)

- [ ] **Add test for `TaskScheduler` DST transition handling** — The scheduler caches the local UTC offset and refreshes it hourly. DST transitions (e.g., spring forward) could cause scheduled daily tasks to fire an hour early or skip entirely. Add a test with mocked `datetime.now()` to verify correct behavior across DST boundaries. (`tests/unit/test_scheduler.py`)

- [ ] **Add test for `_process_tool_calls` salvage on `BaseException`** — Phase 9 added a `BaseException` handler in `_process_tool_calls()` that salvages partial results from completed tasks. Add a test that simulates a `KeyboardInterrupt` during TaskGroup execution and verifies that already-completed tool results are returned rather than lost. (`tests/unit/test_bot.py`)

- [ ] **Add regression test for `handle_message` returning `None` on oversized messages** — `handle_message()` rejects messages exceeding `MAX_MESSAGE_LENGTH` and returns `None`. Add a test verifying the exact boundary behavior: a message at `MAX_MESSAGE_LENGTH - 1` is processed, and a message at `MAX_MESSAGE_LENGTH + 1` is rejected with `None`. (`tests/unit/test_bot.py`)

### DevOps / Infrastructure

- [ ] **Pin Dockerfile base image to a specific patch release** — `python:3.11-slim` floats to the latest 3.11.x. A Docker Hub patch release could introduce subtle runtime changes. Pin to a specific version (e.g., `python:3.11.12-slim-bookworm`) and update deliberately during scheduled maintenance. (`Dockerfile:20, 44`)

- [ ] **Expand `.dockerignore` to exclude all development artifacts** — The `.dockerignore` may not exclude `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.hypothesis/`, `.tmp/`, `.opencode/`, `.agents/`, `.claude/`, and `*.pyc` files. These inflate the build context sent to the Docker daemon, slowing builds. Audit and add all dev-only paths. (`.dockerignore`)

- [ ] **Add `--cov-fail-under` threshold increase roadmap to CI** — Current CI requires 60% coverage (`--cov-fail-under=60`). Add a plan to incrementally raise this threshold (60 → 65 → 70 → 75) over subsequent phases, ensuring each phase contributes test coverage improvements. Document the target timeline. (`.github/workflows/ci.yml:79`, `PLAN.md`)

- [ ] **Add `pip-audit` or `safety` scan to CI pipeline** — Dependencies are pinned to major versions (e.g., `openai~=2.29.0`) but there's no automated vulnerability scanning. Add a `pip-audit` step to CI to catch known CVEs in transitive dependencies before they reach production. (`.github/workflows/ci.yml`)
