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

- [x] **Add circuit-breaker pattern for database write operations** — When the filesystem is degraded (disk full, NFS dropout), every DB write individually times out after `DEFAULT_DB_TIMEOUT` (10s). Under sustained failure, this creates a backlog of blocked coroutines starving the event loop. Add a lightweight write-circuit-breaker on `Database` that fast-fails writes when the last N consecutive writes all failed, preventing thundering-herd timeouts. (`src/db/db.py`, `src/utils/circuit_breaker.py`)

- [x] **Handle `read_agents_md()` `FileNotFoundError` in `ContextAssembler`** — `Memory.read_agents_md()` raises `FileNotFoundError` if AGENTS.md doesn't exist (e.g., `ensure_workspace()` wasn't called yet for a new scheduled task). `ContextAssembler.assemble()` doesn't catch this — it propagates up and aborts the scheduled task. Catch and substitute the default agents content in the assembler. (`src/core/context_assembler.py:108`, `src/memory.py:395-417`)

- [x] **Add retry with exponential backoff for transient LLM errors in `_react_loop`** — `_react_loop()` catches `LLMError` for circuit-breaker-open but re-raises all other LLM errors (rate-limit, timeout, server error). These are often transient. Add a limited retry (1-2 attempts with backoff) for retryable error codes before propagating, reducing message-processing failures from temporary provider issues. (`src/bot.py:984-1010`)

### Security

- [x] **Add per-turn tool-call count limit** — `_process_tool_calls()` executes all requested tool calls in parallel with no cap on how many tools the LLM can request in a single turn. A confused or prompt-injected LLM could request 50+ concurrent tool calls, exhausting system resources (file handles, thread pool, memory). Add a `MAX_TOOL_CALLS_PER_TURN` constant (e.g., 10) and truncate/reject excessive requests with a warning to the LLM. (`src/bot.py:1067-1179`, `src/constants.py`)

- [x] **Centralize `chat_id` validation at the `IncomingMessage` boundary** — While `db.py` has `_validate_chat_id()` and `sanitize_path_component()`, `chat_id` flows through `memory.py`, `workspace_integrity.py`, and `scheduler.py` with varying levels of sanitization. Add validation in `IncomingMessage.__post_init__()` or at the top of `handle_message()` to catch malicious chat IDs before they reach any filesystem operation, as a defense-in-depth layer. (`src/channels/base.py:38-88`, `src/bot.py:452-547`)

- [x] **Add request size limits to health server middleware** — The health server has IP-based rate limiting but no request body or URL length limits. An attacker could send oversized requests to consume server memory. Add middleware to reject requests with bodies > 1KB or URL paths > 2KB — health endpoints only serve short GET requests. (`src/health/server.py`)

- [x] **Audit and restrict environment variable reading in `RateLimiter.from_env()`** — `RateLimitConfig.from_env()` reads `RATE_LIMIT_CHAT_PER_MINUTES` and `RATE_LIMIT_EXPENSIVE_PER_MINUTES` from env vars without validating the range. A misconfigured env var (e.g., `RATE_LIMIT_CHAT_PER_MINUTES=999999`) effectively disables rate limiting. Add sensible bounds (min=1, max=100) and log the effective values at startup. (`src/rate_limiter.py:83-95`)

### Observability & Monitoring

- [x] **Propagate correlation IDs through database operations** — Database operations in `db.py` log `chat_id` but don't include the correlation ID that the message pipeline sets via contextvars. Propagate the current correlation ID through to DB log statements for end-to-end request tracing in production logs. (`src/db/db.py`, `src/logging/logging_config.py`)

- [x] **Add fixed-bucket histogram for LLM latency percentiles** — The `/metrics` endpoint exposes average LLM latency, but operators need percentiles (p50, p95, p99) for alerting. Implement a fixed-bucket histogram approach (e.g., buckets at 0.5s, 1s, 2s, 5s, 10s, 30s, 60s, 120s) in `PerformanceMetrics` and expose `custombot_llm_latency_bucket` in the Prometheus output. (`src/monitoring/performance.py`, `src/health/server.py`)

- [x] **Add workspace disk-usage growth rate metric** — `WorkspaceMonitor` tracks total size but doesn't compute the derivative (MB/hour). Add a growth-rate computation (current_size - previous_size / elapsed_time) and expose it as `custombot_workspace_growth_mb_per_hour` so operators can detect sudden spikes (e.g., a runaway skill generating large files). (`src/monitoring/workspace_monitor.py`, `src/health/server.py`)

- [x] **Add per-skill execution count and error-rate metrics** — `PerformanceMetrics` tracks per-skill latency but not execution count or error rate. Add counters (`custombot_skill_executions_total`, `custombot_skill_errors_total`) so operators can identify which skills are used most and which fail most often. (`src/monitoring/performance.py`, `src/core/tool_executor.py`)

### Test Coverage

- [x] **Add integration test for config hot-reload applying changes to a running bot** — `ConfigWatcher` polls for changes and `ConfigChangeApplier` applies them, but there's no integration test verifying that a config change (e.g., changing `max_tool_iterations` from 10 to 5) takes effect on the next message without restart. (`tests/integration/test_application_lifecycle.py`)

- [x] **Add chaos test for concurrent `save_messages_batch` and `compress_chat_history`** — Both operations acquire per-chat locks, but `compress_chat_history` is triggered asynchronously after writes (in `save_message` and `save_messages_batch`). There's a window where a write triggers compression, which then rewrites the file while another write is pending. Add a test that exercises this race: write a batch, then immediately write another batch, and verify no data loss. (`tests/unit/test_db.py`)

- [x] **Add test for `Memory` mtime cache consistency after external file modification** — `Memory.read_memory()` uses mtime-based caching. If an external process (e.g., a skill modifying MEMORY.md) changes the file between reads, the cache should be invalidated. Add a test that modifies the file's content and mtime between reads and verifies the second read returns the updated content. (`tests/unit/test_memory.py`)

- [x] **Add test for `TaskScheduler` DST transition handling** — The scheduler caches the local UTC offset and refreshes it hourly. DST transitions (e.g., spring forward) could cause scheduled daily tasks to fire an hour early or skip entirely. Add a test with mocked `datetime.now()` to verify correct behavior across DST boundaries. (`tests/unit/test_scheduler.py`)

- [x] **Add test for `_process_tool_calls` salvage on `BaseException`** — Phase 9 added a `BaseException` handler in `_process_tool_calls()` that salvages partial results from completed tasks. Add a test that simulates a `KeyboardInterrupt` during TaskGroup execution and verifies that already-completed tool results are returned rather than lost. (`tests/unit/test_bot.py`)

- [x] **Add regression test for `handle_message` returning `None` on oversized messages** — `handle_message()` rejects messages exceeding `MAX_MESSAGE_LENGTH` and returns `None`. Add a test verifying the exact boundary behavior: a message at `MAX_MESSAGE_LENGTH - 1` is processed, and a message at `MAX_MESSAGE_LENGTH + 1` is rejected with `None`. (`tests/unit/test_bot.py`)

### DevOps / Infrastructure

- [x] **Pin Dockerfile base image to a specific patch release** — `python:3.11-slim` floats to the latest 3.11.x. A Docker Hub patch release could introduce subtle runtime changes. Pin to a specific version (e.g., `python:3.11.12-slim-bookworm`) and update deliberately during scheduled maintenance. (`Dockerfile:20, 44`)

- [x] **Expand `.dockerignore` to exclude all development artifacts** — The `.dockerignore` may not exclude `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.hypothesis/`, `.tmp/`, `.opencode/`, `.agents/`, `.claude/`, and `*.pyc` files. These inflate the build context sent to the Docker daemon, slowing builds. Audit and add all dev-only paths. (`.dockerignore`)

- [x] **Add `--cov-fail-under` threshold increase roadmap to CI** — Current CI requires 60% coverage (`--cov-fail-under=60`). Add a plan to incrementally raise this threshold (60 → 65 → 70 → 75) over subsequent phases, ensuring each phase contributes test coverage improvements. Document the target timeline. (`.github/workflows/ci.yml:79`, `PLAN.md`)

- [x] **Add `pip-audit` or `safety` scan to CI pipeline** — Dependencies are pinned to major versions (e.g., `openai~=2.29.0`) but there's no automated vulnerability scanning. Add a `pip-audit` step to CI to catch known CVEs in transitive dependencies before they reach production. (`.github/workflows/ci.yml`)

---

## Phase 12 — Senior Review (2026-04-23)

Generated from a twelfth-pass codebase audit focusing on architectural
complexity reduction, data-integrity hardening, performance tuning,
and test-coverage gaps not addressed in Phases 1–11.

---

### Refactoring

- [x] **Extract `_process()` persistence + event emission into a `_finalize_response()` helper** — `_process()` in `bot.py` is ~100 lines that does 7 distinct things: persist user turn, ensure workspace, build context, run ReAct loop, finalize topic, filter content, append tool summary, persist batch, and emit events. Extract the post-ReAct steps (steps 5–7 in `_process()`: topic finalization, content filtering, tool-summary formatting, generation-check batch write, event emission) into a `_finalize_response(chat_id, raw_response, tool_log, buffered_persist, generation, verbose)` method. This makes `_process()` a straight-line pipeline and makes the finalization independently testable. (`src/bot.py:926-966`)

- [x] **Replace `channel_type` string dispatch with an enum** — `IncomingMessage.channel_type` is a free-form string validated only by regex. Multiple modules check `channel_type == "whatsapp"` or `channel_type == "cli"` in ad-hoc `if` statements. Introduce a `ChannelType` enum (`WHATSAPP`, `CLI`, etc.) and use it in `IncomingMessage`, `BaseChannel`, logging, and metrics. This eliminates the risk of typo-based mismatches and makes channel-type coverage auditable via IDE find-references. (`src/channels/base.py:33-36, 117`)

- [x] **Consolidate `_chat_dir()` and `_ensure_chat_dir()` in `Memory` to avoid path duplication** — Both methods construct the same path (`self._root / "whatsapp_data" / sanitize_path_component(chat_id)`) and both call `self._validate_path()`. Extract the path construction into a `_resolve_chat_path()` that returns the validated `Path`, then have `_chat_dir()` call it directly and `_ensure_chat_dir()` call it + `mkdir`. Removes the duplicated `_validate_path` call and the hardcoded `"whatsapp_data"` segment appearing in two places. (`src/memory.py:111-122`)

- [x] **Move `_outbound_key()` hash computation from `DeduplicationService` to a standalone function** — The SHA-256 key derivation is a pure function with no `self` dependency. Extract it to module level so it can be unit-tested independently and reused if the outbound dedup logic is ever split into its own module. Minor readability win. (`src/core/dedup.py:110-113`)

- [x] **Extract `_NoOpApplier` from `_step_config_watcher` to module level** — `_NoOpApplier` is defined inside the startup step closure, meaning a new class object is created every time the step runs. Move it to module level in `startup.py` (or to `config_watcher.py`) so it's defined once. Minor, but avoids per-startup class creation and makes the fallback applier discoverable for tests. (`src/core/startup.py:278-281`)

### Performance Optimization

- [x] **Cache `_message_file()` path resolution to avoid repeated `sanitize_path_component` + `_validate_chat_id` on every DB operation** — `_message_file()` is called on every `save_message`, `save_messages_batch`, `get_recent_messages`, and `compress_chat_history` call. Each invocation runs sanitize + validate + Path construction. For a high-volume bot processing dozens of messages per second across a handful of active chats, these are redundant. Add an LRU cache (`functools.lru_cache` or `LRUDict`) on `_message_file()` keyed by `chat_id`, invalidated only when a new chat_id is first seen. The cache size should match `MAX_LRU_CACHE_SIZE`. (`src/db/db.py:857-863`)

- [x] **Batch `_persist()` calls when multiple scheduler tasks fire in the same tick** — `_loop()` in `scheduler.py` executes all due tasks concurrently via `asyncio.gather`, but each `_execute_task()` calls `_persist(chat_id)` independently. If 5 tasks fire for the same chat in one tick, `_persist` writes `tasks.json` 5 times sequentially. Collect the set of dirty `chat_id`s during a tick and persist each once after all tasks complete. Reduces file I/O from N tasks × M duplicates to N unique chats. (`src/scheduler.py:441-474`)

- [x] **Add `__slots__` to `DeduplicationService` for memory efficiency** — The class already declares `__slots__` correctly. However, `DedupStats` is a `@dataclass` without `slots=True`. Add `slots=True` to `DedupStats` for consistency and to reduce per-instance memory (4 int fields). Minor but follows the project's pattern for frozen data containers. (`src/core/dedup.py:39-54`)

- [x] **Pre-compute `tool_definitions` as a cached property invalidated by skill registration changes** — Phase 11 added this as a task but verify it's truly cached. If `SkillRegistry.tool_definitions` still rebuilds Pydantic models on every property access, add a `_tool_defs_cache: list | None` field that's set to `None` on `register()` / `unregister()` and lazily rebuilt on access. Confirm with a benchmark test that 100 consecutive accesses don't trigger 100 rebuilds. (`src/skills/__init__.py`)

### Error Handling & Resilience

- [x] **Handle `read_text` failure in `_compress_chat_history_sync` gracefully** — If the JSONL file is deleted between the `stat()` check and the `read_text()` call (race with `repair_message_file` or manual deletion), the `OSError` is caught and returns `{"compressed": False}`, but the `FileNotFoundError` subclass isn't explicitly handled. Add explicit `FileNotFoundError` handling to distinguish "file disappeared" (log + skip) from "I/O error" (log warning), improving debuggability. (`src/db/db.py:1280-1284`)

- [x] **Guard `_build_message_record()` against `None` content** — `_build_message_record()` is called with `content` from various sources. If a skill returns `None` (instead of an empty string) and it propagates through `str(result)` → `None`, the `calculate_checksum(content, ...)` call would fail with `TypeError`. Add an early `content = content or ""` guard to ensure content is always a string before checksum calculation. (`src/db/db.py:976-1021`)

- [x] **Add timeout to `_execute_task()` in the scheduler** — `_execute_task()` calls `_trigger_with_retry()` which can retry multiple times with backoff, but the total execution time is unbounded. If the bot is stuck (e.g., LLM provider down, each retry waiting 30s), a single task could block the scheduler for minutes. Wrap the task execution in `asyncio.wait_for()` with a maximum total timeout (e.g., 300 seconds = 5 minutes) so a stuck task doesn't indefinitely delay co-scheduled tasks. (`src/scheduler.py:363-437`)

- [x] **Handle `asyncio.to_thread` exceptions in `Memory.read_memory()` / `read_agents_md()`** — Both methods call `await asyncio.to_thread(self._stat_and_read, ...)`. If the thread pool is exhausted or the function raises an unexpected exception, it propagates as a `RuntimeError` to `ContextAssembler`, which handles it via `return_exceptions=True`. However, the error message is opaque. Add an explicit `try/except Exception` around the `await asyncio.to_thread()` call in both methods to log the failure with the chat_id and raise a more descriptive error. (`src/memory.py:191-193, 404-406`)

### Security

- [x] **Audit and restrict file paths in `_write_tasks_file()` and `_read_tasks_file()`** — The scheduler's `_persist()` constructs `workspace / chat_id / SCHEDULER_DIR / TASKS_FILE` but doesn't validate that `chat_id` doesn't contain path traversal characters before constructing the path. While `_sanitize_chat_id_for_path` and `_validate_chat_id` exist in `db.py`, the scheduler doesn't call them. Add path validation in `_persist()` and `_load()` to ensure the resolved path stays within `workspace/`. (`src/scheduler.py:222-253`)

- [x] **Strip sensitive query parameters from `base_url` in LLM client logs** — `LLMClient.__init__` receives `cfg.base_url` which may contain API keys as query parameters (e.g., `http://localhost:11434/v1?key=secret`). While the OpenAI SDK uses `api_key` for auth, some providers embed credentials in the URL. Audit all log statements that reference `cfg.base_url` or `cfg.model` and ensure no credential material leaks. Add URL sanitization that strips query parameters before logging. (`src/llm.py:210-237`, `src/builder.py:115`)

- [x] **Add rate limiting to `_confirm_send()` in safe mode** — The safe-mode `_confirm_send()` uses `input()` in a loop with no exit limit. A misconfigured or automated input source could send unlimited characters to `input()`. Add a maximum retry count (e.g., 3 attempts) after which the send is automatically rejected, preventing an accidental infinite prompt loop. (`src/channels/base.py:326-335`)

### Observability & Monitoring

- [x] **Add `custombot_react_loop_iterations_total` Prometheus counter** — `PerformanceMetrics` tracks `track_react_iterations()` but the Prometheus output in `/metrics` should expose a counter (`custombot_react_iterations_total`) so operators can set alerts on high iteration counts (indicating the LLM is stuck in tool-call loops). Currently this metric is only logged to the session summary, not exposed via `/metrics`. (`src/monitoring/performance.py`, `src/health/server.py`)

- [x] **Track and expose conversation-context token budget utilization** — `build_context()` trims messages to fit within a token budget, but the actual vs. budget ratio isn't tracked. Add a metric (`custombot_context_budget_utilization`) that records the ratio of used tokens to the max budget on each context build. This helps operators identify chats that consistently hit the budget ceiling (indicating they need compression or higher limits). (`src/core/context_builder.py`)

- [x] **Add structured startup-duration breakdown to `/health` endpoint** — The health endpoint returns component status but not startup timing. `component_durations` is tracked in `StartupContext` but not exposed. Add a `startup_durations` field to the `/health` response (behind HMAC auth) so operators can identify slow-starting components (e.g., embedding model probe, workspace integrity check). (`src/health/server.py`, `src/core/startup.py:80`)

- [x] **Log skill argument size distribution for capacity planning** — When skills receive oversized arguments (>100KB), `ToolExecutor` rejects them silently. Add a metric counter (`custombot_skill_args_oversized_total`) and log the skill name + arg size when the limit is hit, so operators can identify which skills are being abused or misconfigured. (`src/core/tool_executor.py:114-128`)

### Test Coverage

- [x] **Add test for `_finalize_response()` extraction — verify topic finalization, content filtering, and batch persist in isolation** — Once `_finalize_response()` is extracted, test that: (a) META blocks are parsed and topic cache updated, (b) `filter_response_content` is called and sanitized output is used, (c) tool summary formatting is applied for `verbose="summary"`, (d) generation check logs warning on conflict, (e) `save_messages_batch` is called with correct buffered_persist + assistant message. (`tests/unit/test_bot.py`)

- [x] **Add property-based test for `_read_file_lines()` reverse-seek correctness** — Use `hypothesis` (already a dev dependency) to generate files of varying sizes and line counts, then verify that `_read_file_lines(path, limit)` returns exactly the last `limit` non-empty lines in chronological order. Covers edge cases: file smaller than limit, file exactly at limit, file with trailing newline, file with no newlines (corrupted), empty file. (`tests/unit/test_db.py`)

- [x] **Add test for scheduler task-execution timeout** — Create a scheduled task where `_trigger_with_retry` hangs beyond the maximum timeout. Verify that: (a) the timeout fires and the task is marked as failed, (b) other co-scheduled tasks in the same tick are not blocked, (c) the scheduler loop continues to the next tick. (`tests/unit/test_scheduler.py`)

- [x] **Add test for `_message_file()` path-cache correctness** — Verify that: (a) repeated calls with the same `chat_id` return the same `Path` object (or equivalent), (b) invalid `chat_id` values raise `ValueError` before caching, (c) the cache doesn't grow beyond `MAX_LRU_CACHE_SIZE`. (`tests/unit/test_db.py`)

- [x] **Add test for config hot-reload applying `BotConfig` changes without restart** — Specifically verify that changing `max_tool_iterations` from 10 to 5 in `config.json` causes the bot's next ReAct loop to use the new limit. This is a regression test for the config-watcher → BotConfig → ReAct-loop data flow. (`tests/integration/test_application_lifecycle.py`)

- [x] **Add test for `_chat_generations` TTL/eviction under sustained writes** — Simulate a bot running for weeks by generating writes for more than `MAX_CHAT_GENERATIONS` unique chat IDs. Verify: (a) the dict never exceeds the cap, (b) recently-written chats are preserved (LRU eviction), (c) `get_generation()` returns 0 for evicted entries without error. (`tests/unit/test_db.py`)

- [x] **Add integration test for concurrent compression and read** — Start a `compress_chat_history()` operation and concurrently call `get_recent_messages()` for the same chat. Verify that: (a) no data corruption occurs, (b) `get_recent_messages()` returns either the pre-compression or post-compression view (never a partial/truncated view), (c) no exceptions are raised. (`tests/integration/test_concurrent_load.py`)

### DevOps / Infrastructure

- [x] **Add Python 3.13 to CI test matrix** — Python 3.13 is the latest stable release. Adding it to the matrix alongside 3.11 and 3.12 ensures forward-compatibility and catches deprecation warnings early. If 3.13-specific failures occur, document them as known issues rather than blocking the build. (`.github/workflows/ci.yml:57`)

- [x] **Add `--tb=short` to pytest invocation in CI for cleaner failure output** — Default tracebacks in CI can be verbose, especially with asyncio. Adding `--tb=short` reduces CI log noise while preserving the essential failure information. (`.github/workflows/ci.yml:81`)

- [x] **Bump `--cov-fail-under` from 60 to 65 in CI** — Phase 12 adds 7 new tests (above). If coverage exceeds 65% after these additions, update the threshold to lock in the improvement, following the roadmap documented in Phase 11. (`.github/workflows/ci.yml:86`)

---

## Phase 13 — Senior Review (2026-04-24)

Generated from a thirteenth-pass codebase audit focusing on type safety,
operational robustness, architectural debt, test infrastructure, and
production readiness gaps not addressed in Phases 1–12.

---

### Refactoring

- [x] **Replace `Any` type annotations in `StartupContext` with protocol references** — `StartupContext` uses `Any` for `app`, `components`, `scheduler`, `channel`, `pipeline`, `workspace_monitor`, `config_watcher`, and `health_server` fields. This defeats mypy's ability to catch attribute-access errors in startup steps (e.g., calling a nonexistent method on `ctx.channel` would only fail at runtime). Replace with `TYPE_CHECKING`-guarded protocol or concrete types (e.g., `from src.channels.base import BaseChannel`, `from src.scheduler import TaskScheduler`) to get compile-time safety. (`src/core/startup.py:56-80`)

- [x] **Extract `MockChatCompletion` from `conftest.py` into a shared test helper module** — `MockChatCompletion` is a reusable test fixture that constructs OpenAI-compatible response objects. It's currently in `tests/conftest.py` but its usage pattern (customizing content, tool_calls, finish_reason) is needed across multiple test files. Extract into `tests/helpers/llm_mocks.py` alongside builder helpers (e.g., `make_tool_call_response(tool_calls=[...])`, `make_streaming_response(chunks=[...])`) so test authors don't reinvent mock responses. (`tests/conftest.py:89-113`, new file `tests/helpers/llm_mocks.py`)

- [x] **Consolidate `LRUDict` and `LRULockCache` eviction strategies into a single generic `BoundedDict`** — `LRUDict` (in `src/utils/__init__.py`) evicts by popping the oldest half when full. `TokenUsage._per_chat` (in `src/llm.py`) uses a plain `dict` with manual oldest-half eviction that duplicates the same pattern. `DeduplicationService._outbound_cache` is another `OrderedDict` with TTL + eviction. Consolidate into a single `BoundedOrderedDict[K, V]` class with configurable eviction policy (LRU count, TTL, or both) to eliminate three independent eviction implementations. (`src/utils/__init__.py`, `src/llm.py:166-188`, `src/core/dedup.py`)

- [x] **Add `__slots__` to `TokenUsage` dataclass for memory consistency** — `TokenUsage` is a `@dataclass` without `slots=True`, unlike `DedupStats` and `DeduplicationService` which were upgraded in Phase 12. It holds `_per_chat` (a dict that can grow to 1000 entries) and a `threading.Lock`. Adding `slots=True` reduces per-instance memory overhead and is consistent with the project's pattern for data containers. (`src/llm.py:136-146`)

- [x] **Move `_classify_llm_error()` from `llm.py` to a dedicated `src/llm_error_classifier.py` module** — `_classify_llm_error()` is a 70-line function that maps OpenAI SDK exceptions to domain `LLMError` instances. It imports 7 exception classes from `openai` at call time (inside the function body for lazy imports). Moving it to its own module (1) keeps `llm.py` focused on the client, (2) allows the classifier to be unit-tested in isolation without instantiating an `LLMClient`, and (3) makes it reusable if a future multi-provider architecture needs different classifiers per provider. (`src/llm.py:50-132`)

### Performance Optimization

- [x] **Pre-warm httpx connection pool during startup** — `LLMClient.__init__` creates an `httpx.AsyncClient` but the first LLM call pays the TCP + TLS handshake cost. For providers with high-latency handshakes (e.g., self-hosted proxies, Ollama over VPN), this adds 1-3 seconds to the first message response. Add an optional `_warmup()` call during `_step_bot_components` that sends a lightweight request (e.g., models list) to pre-establish the connection before the first user message arrives. (`src/llm.py:213-227`, `src/core/startup.py:159-170`)

- [x] **Use `orjson` for JSONL message serialization in `Database`** — The project lists `orjson~=3.10.0` as a dependency ("Fast JSON serialization — hot-path acceleration") but `Database` uses stdlib `json.dumps`/`json.loads` for JSONL read/write operations. On a bot processing dozens of messages per second, the JSONL serialization in `_write_messages_sync`, `_read_file_lines`, and `save_messages_batch` is a hot path. Switch to `orjson` (already imported as `json_dumps`/`json_loads` in `src/utils/__init__.py`) for measurable latency reduction on large conversation histories. (`src/db/db.py` — all `json.dumps`/`json.loads` call sites)

- [x] **Add `mmap`-based reverse-seek for large JSONL files in `_read_file_lines()`** — `_read_file_lines()` does a reverse byte-seek to read the last N lines from a JSONL file. For very large files (5000+ lines), this involves reading potentially hundreds of KB sequentially. Using `mmap.mmap()` for the seek operation avoids loading the entire file into Python memory and allows the OS to manage page-level access. This is a targeted optimization for the compression-threshold scenario. (`src/db/db.py` — `_read_file_lines` method)

- [x] **Batch `save_message` calls in `process_scheduled()` into a single `save_messages_batch`** — `process_scheduled()` calls `upsert_chat`, then `save_message` for the user turn, then `save_message` for the assistant turn (3 separate file writes). Each `save_message` acquires the per-chat lock, opens the JSONL file, appends, and closes. Combine all 3 writes into a single `save_messages_batch` call to reduce file I/O from 3 round-trips to 1, consistent with how `_finalize_response` already batches writes. (`src/bot.py:777-789`)

### Error Handling & Resilience

- [x] **Handle `orjson.JSONDecodeError` alongside `json.JSONDecodeError` in `_read_file_lines()`** — If the migration to `orjson` is implemented, `safe_json_parse()` will throw `orjson.JSONDecodeError` instead of `json.JSONDecodeError`. The current error handlers in `db.py` catch `json.JSONDecodeError` explicitly. Audit all JSON parse error handlers to catch both exception types (or use a common base class) to prevent `orjson` decode errors from crashing the DB layer. (`src/db/db.py` — all `except json.JSONDecodeError` blocks)

- [x] **Add retry-with-backoff for database file write failures in `save_messages_batch()`** — `save_messages_batch()` writes to the JSONL file once. If the write fails (transient disk I/O error, NFS hiccup), the entire message batch is lost and the user sees an error. Add a lightweight retry (1-2 attempts with short backoff) for write failures in `save_messages_batch`, similar to how `_react_loop` retries transient LLM errors. This protects against the most common transient failure mode. (`src/db/db.py` — `save_messages_batch` method)

- [x] **Guard `chat_stream()` against `usage_data` being `None` when computing token counts** — In `chat_stream()` lines 536-545, `prompt_tokens` and `completion_tokens` are only initialized inside the `if usage_data:` block, but `self._token_usage.add(prompt_tokens, completion_tokens)` is called unconditionally outside it. If a provider doesn't return `usage` data, `NameError` will be raised. Add an `else` branch that sets both to 0, or move the `add` call inside the `if` block. (`src/llm.py:536-545`)

- [x] **Add structured error event for `_execute_tool_call()` path traversal detection** — When the workspace path traversal guard fires in `_execute_tool_call()`, the incident is logged at ERROR level but no event is emitted on the EventBus. Security events like path traversal attempts should emit an `error_occurred` event so monitoring subscribers can trigger alerts (e.g., notify the operator that a potential attack was detected). (`src/bot.py:1340-1358`)

- [x] **Sanitize correlation ID in `set_correlation_id()` to prevent log injection** — `set_correlation_id()` accepts any string and it eventually appears in log output via structured extra fields. A malicious `IncomingMessage.correlation_id` containing newline characters or ANSI escape sequences could inject fake log lines or manipulate terminal output. Add validation in `set_correlation_id()` to strip control characters and truncate to a reasonable length (e.g., 64 chars). (`src/logging/logging_config.py` — `set_correlation_id` function)

### Security

- [x] **Add `Content-Security-Policy` and security headers to health server responses** — The health server's aiohttp handler returns JSON responses but doesn't set security headers. While the health endpoint is typically internal, defense-in-depth requires `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Type: application/json`, and `Cache-Control: no-store` headers on all responses to prevent content-type sniffing, clickjacking, and caching of sensitive metrics data. (`src/health/server.py` — all response handlers)

- [x] **Add request method validation to health server — reject non-GET/HEAD/OPTIONS** — The health server accepts any HTTP method. While it only serves GET endpoints, a POST/PUT/DELETE request still invokes the handler logic (path matching, HMAC verification). Add middleware that rejects non-GET/HEAD/OPTIONS requests with 405 Method Not Allowed before reaching the handler, reducing the attack surface. (`src/health/server.py`)

- [x] **Enforce `allowed_numbers` ACL at the channel level before `handle_message()`** — Currently, `allowed_numbers` filtering happens in the WhatsApp channel's message callback before calling `_on_message`. But `IncomingMessage` doesn't carry a "passed ACL" flag — any code that calls `bot.handle_message()` directly (e.g., crash recovery, CLI channel) bypasses the ACL. Add an `acl_passed: bool` field to `IncomingMessage` and validate it in `handle_message()`, or document that ACL enforcement is the channel's responsibility and add a guard in recovery code. (`src/channels/base.py:38-88`, `src/bot.py:332-438`)

- [x] **Add TTL for `SkillAuditLogger` log files to prevent unbounded disk growth** — `SkillAuditLogger` writes to `workspace/logs/skill_audit/` without rotation or cleanup. Unlike LLM logs (which have `LLM_LOG_MAX_FILES` and `LLM_LOG_MAX_AGE_DAYS`), audit logs grow without bound. Add a `AUDIT_LOG_MAX_FILES` constant and periodic cleanup integrated with the `WorkspaceMonitor`'s cleanup cycle. (`src/security/audit.py`, `src/monitoring/workspace_monitor.py`)

### Observability & Monitoring

- [x] **Add `custombot_db_write_latency` Prometheus metric** — `PerformanceMetrics` tracks `track_db_latency()` but the `/metrics` endpoint doesn't expose a `custombot_db_write_latency_milliseconds` counter or histogram. Operators cannot set alerts on slow database writes. Add a DB latency metric to the Prometheus output alongside the existing LLM latency metric. (`src/monitoring/performance.py`, `src/health/server.py`)

- [x] **Track and expose `EventBus` subscriber counts and emission counts** — The EventBus tracks handler subscriptions but doesn't expose usage metrics. Add counters for total emissions per event name and total handler invocations, and expose them via `/metrics` as `custombot_event_emitted_total{event="..."}` and `custombot_event_handler_invocations_total{event="..."}`. This helps operators verify that plugins are actually receiving events and detect stuck or slow handlers. (`src/core/event_bus.py`, `src/health/server.py`)

- [x] **Add structured log correlation between `_on_message` pipeline stages** — The message pipeline's middleware chain processes a message through 6+ stages (operation tracker, metrics, logging, preflight, typing, handle message). Each stage logs independently but without a shared pipeline-stage identifier. Add a `pipeline_stage` field to the structured log extra dict so that operators can trace which stage a message reached when debugging pipeline failures. (`src/core/message_pipeline.py`)

- [x] **Add per-provider LLM error classification histogram to metrics** — `_classify_llm_error()` produces rich error codes but they're only logged, not aggregated. Add a counter metric `custombot_llm_errors_total{code="..."}` that increments per error code, so operators can set targeted alerts (e.g., "alert on LLM_API_KEY_INVALID within 5 minutes"). (`src/llm.py:50-132`, `src/monitoring/performance.py`)

### Test Coverage

- [x] **Add E2E test directory with at least one smoke test** — The `tests/e2e/` directory exists but is empty (no Python files found). The coverage roadmap targets 75% by Phase 14, but E2E tests validate the full integration path. Add at least one smoke test that instantiates the full `Application` lifecycle with a mock channel and verifies that a message flows through the pipeline to a response. (`tests/e2e/`, new file `tests/e2e/test_smoke.py`)

- [x] **Add parametrized test for `_classify_llm_error()` covering all OpenAI exception types** — `_classify_llm_error()` handles 7 exception types (AuthenticationError, PermissionDeniedError, RateLimitError, APITimeoutError, NotFoundError, APIConnectionError, BadRequestError) plus a generic fallback. There's no dedicated test verifying each mapping. Add a parametrized test that passes each exception type and verifies the returned `LLMError.error_code` and `LLMError.suggestion` are correct. (`tests/unit/test_llm.py`)

- [x] **Add test for `chat_stream()` missing `usage_data` edge case** — Verify that when a streaming provider doesn't return `usage` data in the stream events, `chat_stream()` doesn't raise `NameError` or `UnboundLocalError` and token tracking is skipped gracefully. (`tests/unit/test_llm.py`)

- [x] **Add test for `process_scheduled()` injection detection with confidence thresholds** — Verify that a scheduled prompt with injection patterns (e.g., "Ignore all previous instructions") is flagged by `detect_injection()` and that the sanitized version is used. Test both high-confidence (blocked) and low-confidence (logged but allowed) scenarios. (`tests/unit/test_bot.py`)

- [x] **Add property-based test for `outbound_key()` hash collision resistance** — Use `hypothesis` to generate pairs of distinct `(chat_id, text)` inputs and verify that `outbound_key()` produces different SHA-256 hashes. While collisions are astronomically unlikely for SHA-256, the test also validates that the null-byte separator prevents prefix collisions (e.g., `("a", "bc")` vs `("ab", "c")`). (`tests/unit/test_dedup.py`)

- [x] **Add test for `StartupOrchestrator._resolve_order()` circular dependency detection** — `_resolve_order()` raises `ValueError` on circular dependencies but this is untested. Add a test with a cycle (A depends on B, B depends on A) and verify the error message is informative. Also test missing dependency detection. (`tests/unit/test_startup.py`)

- [x] **Add regression test for `TokenUsage` LRU eviction correctness** — `TokenUsage._per_chat` evicts the oldest half when `_per_chat_max` is reached. Verify that: (a) eviction happens exactly when the cap is exceeded, (b) the evicted entries are the oldest (first-inserted), (c) recent entries are preserved, (d) total token counts are not affected by eviction (they're tracked globally). (`tests/unit/test_llm.py`)

### DevOps / Infrastructure

- [x] **Add `mypy --strict` opt-in CI job for progressive type safety** — The current mypy config has `disallow_untyped_defs = false` and `ignore_missing_imports = true`. While appropriate for the current codebase, adding a separate CI job that runs `mypy --strict` on a curated subset of files (e.g., `src/core/*.py`, `src/bot.py`) would catch regressions in the most critical modules without blocking the main build. Mark as `continue-on-error: true` initially. (`.github/workflows/ci.yml`)

- [x] **Add `pytest-xdist` for parallel test execution in CI** — The test suite has 38 unit tests and 5 integration tests. Running them sequentially on a single core is acceptable now but won't scale. Add `pytest-xdist` to dev dependencies and run `pytest -n auto` in CI to utilize all available cores, reducing CI feedback time by 2-3x. (`requirements-dev.txt`, `.github/workflows/ci.yml`)

- [x] **Add Docker health check with configurable timeout via build arg** — The Dockerfile's `HEALTHCHECK` has hardcoded `--timeout=5s` and `--interval=30s`. In production, operators may need different intervals (e.g., longer timeout for slow networks). Add `ARG HEALTH_INTERVAL=30s` and `ARG HEALTH_TIMEOUT=5s` to the Dockerfile so they can be overridden at build time. (`Dockerfile:75-76`)

- [x] **Bump `--cov-fail-under` from 65 to 70 in CI** — Phase 13 adds 7 new tests (above). If coverage exceeds 70% after these additions, update the threshold to lock in the improvement, following the roadmap documented in Phase 12. (`.github/workflows/ci.yml:89`)

---

## Phase 14 — Senior Review (2026-04-25)

Generated from a fourteenth-pass codebase audit focusing on architectural
consolidation, runtime correctness, lock-model mismatches, observability
gaps, PII exposure, test-coverage expansion, and CI pipeline efficiency
not addressed in Phases 1–13.

---

### Refactoring

- [x] **Extract `_build_bot()` component initialization into a declarative registry mirroring `StartupOrchestrator`** — `_build_bot()` in `builder.py` is ~235 lines with 10 sequential initialization blocks following the same pattern (log init → create component → time → log ready → progress.advance). `Application._startup()` was refactored into `StartupOrchestrator` in Phase 11 but the builder wasn't. Extract into a `BuilderOrchestrator` accepting `ComponentSpec` steps so component initialization is data-driven, testable, and extensible without modifying `_build_bot()` directly. (`src/builder.py:54-290`)

- [x] **Consolidate `VectorMemory._embed_cache` eviction into `BoundedOrderedDict`** — Phase 12 consolidated `LRUDict` / `LRULockCache` / `TokenUsage._per_chat` into `BoundedOrderedDict`, but `VectorMemory._embed_cache` (an `OrderedDict` at line 81 with manual `popitem(last=False)` eviction at lines 347–348 and 443–444) still duplicates this pattern. Replace with `BoundedOrderedDict(max_size=256, eviction="half")` to eliminate the third independent eviction implementation. (`src/vector_memory.py:81, 347-348, 443-444`)

- [x] **Consolidate `RoutingEngine._match_cache` TTL+LRU logic into `BoundedOrderedDict`** — `_match_cache` (an `OrderedDict` at line 246) implements its own TTL expiry (`_cache_get` lines 369–380) and LRU eviction (`_cache_put` lines 382–388) identical to what `BoundedOrderedDict` already provides. Replace with `BoundedOrderedDict(max_size=ROUTING_MATCH_CACHE_MAX_SIZE, eviction="half", ttl=ROUTING_MATCH_CACHE_TTL_SECONDS)` to eliminate the fourth independent cache implementation. (`src/routing.py:246, 368-388`)

- [x] **Consolidate `_IPLimiter._trackers` half-eviction into `BoundedOrderedDict`** — `_IPLimiter` in `health/server.py` (lines 76–97) implements yet another LRU-ordered dict with half-eviction (`popitem(last=False)` in a loop at lines 93–94). Replace with `BoundedOrderedDict(max_size=max_ips, eviction="half")` for consistency. (`src/health/server.py:76-97`)

- [x] **Extract `_build_message_record()` inline imports to module-level** — `_build_message_record()` in `db.py` imports `detect_injection` and `sanitize_user_input` from `src.security.prompt_injection` *inside the function body* at lines 1027–1031. This runs on every user-role message write, adding import overhead (module is cached, but the name lookup still occurs). Move to module-level imports behind `TYPE_CHECKING` or lazy-import at module init. (`src/db/db.py:1027-1031`)

- [ ] **Unify `Memory._memory_cache` and `Memory._agents_cache` into a single generic cache helper** — Both caches are `LRUDict` instances with identical mtime-based validation logic (`_stat_and_read` → check mtime → return cached or re-read). The only difference is the file name and cache dict. Extract into a generic `MtimeCache` helper that encapsulates the `(mtime, content)` tuple and hit/miss tracking, reducing `read_memory()` and `read_agents_md()` to one-liners. (`src/memory.py:88-89, 189-215, 408-436`)

### Performance Optimization

- [ ] **Eliminate double file-open in `_read_file_lines()` for large files** — `_read_file_lines()` opens the file twice for files ≥64KB: once in binary mode for the mmap reverse-seek (line 1710), then again in text mode to read the final region (line 1746). For large files, this creates two file handles and two seek operations. Refactor to decode the mmap region directly using `mm[pos:].decode("utf-8")` instead of re-opening in text mode, eliminating the second `open()` syscall and the associated OS overhead. (`src/db/db.py:1710-1753`)

- [ ] **Replace `RoutingEngine._scan_file_mtimes()` glob with `os.scandir()`** — `_scan_file_mtimes()` calls `self._instructions_dir.glob("*.md")` on every stale check (debounced to once per `ROUTING_WATCH_DEBOUNCE_SECONDS`). `glob()` internally lists all entries and filters by pattern. `os.scandir()` is faster because it returns `DirEntry` objects with cached `stat()` results and avoids the pattern-matching overhead. For directories with many instruction files, this reduces the stale-check cost by ~2x. (`src/routing.py:269-279`)

- [ ] **Add connection-pool warmup for `VectorMemory` SQLite reads** — `VectorMemory._get_read_connection()` creates a new SQLite connection on first access per thread. During startup, the first search query pays the sqlite-vec extension loading cost (~5ms). Pre-warm one read connection during `_step_bot_components()` by calling `_get_read_connection()` once after `connect()`, so the first user-facing query doesn't pay this latency. (`src/vector_memory.py:172-196`, `src/core/startup.py:168-184`)

- [ ] **Lazy-init `Bot._audit_logger` (`SkillAuditLogger`) only when skills are actually executed** — `Bot.__init__` creates a `SkillAuditLogger` instance at line 214, which opens the audit log directory on every bot startup even if no skills are ever invoked (e.g., a bot that only handles simple chat). Defer creation to first use inside `ToolExecutor.execute()` to avoid unnecessary filesystem I/O during startup. (`src/bot.py:214`)

### Error Handling & Resilience

- [ ] **Add `OSError` recovery in `_FileHandlePool.get_or_open()`** — `get_or_open()` calls `path.open("a", ...)` without catching `OSError`. If the OS file descriptor limit is reached (`EMFILE`) or permissions are denied, the exception propagates to `_append_to_file()` and ultimately crashes the DB write with an opaque error. Add a try/except around `path.open()` that invalidates stale handles, retries once after evicting all pooled handles, and raises a descriptive `DatabaseError` on persistent failure. (`src/db/db.py:236-260`)

- [ ] **Add `EventBus` emission to `Bot.process_scheduled()` for observability parity** — `_process()` emits `message_received` and `response_sent` events, but `process_scheduled()` emits no events at all. This means scheduled tasks are invisible to EventBus subscribers (monitoring, plugins). Add `scheduled_task_started` and `scheduled_task_completed` events so that monitoring dashboards can track scheduled task execution alongside user messages. (`src/bot.py:659-826`)

- [ ] **Handle `CircuitBreaker` lock-model mismatch for database writes** — `CircuitBreaker` uses `asyncio.Lock` (line 64) but `Database._guarded_write()` calls it from `asyncio.to_thread()` contexts via `_insert_entry`, `_append_to_file`, etc. When `_write_breaker.is_open()` is called from a thread, the `asyncio.Lock` raises `RuntimeError` because there's no running event loop in the thread. Audit all call sites where the DB write breaker is used from thread contexts and either switch to `threading.Lock` or wrap the calls with `asyncio.run_coroutine_threadsafe()`. (`src/utils/circuit_breaker.py:64`, `src/db/db.py:424-452`)

- [ ] **Add graceful handling for `_confirm_send()` when stdin is `/dev/null`** — When the bot runs as a systemd service, `stdin` may be `/dev/null`. `input()` immediately returns an empty string, which doesn't match `"y"` or `"n"` and burns through all `SAFE_MODE_MAX_CONFIRM_RETRIES` attempts before auto-rejecting. The user gets no feedback about why sends are being rejected. Detect `sys.stdin.isatty()` at the top of `_confirm_send()` and log a clear warning: "Safe mode requires an interactive terminal — send auto-rejected." (`src/channels/base.py:347-373`)

- [ ] **Downgrade `Memory.read_agents_md()` `FileNotFoundError` log level from ERROR to DEBUG** — When `ensure_workspace()` hasn't been called yet for a new scheduled task, `read_agents_md()` raises `FileNotFoundError`. The `ContextAssembler` handles this by substituting `DEFAULT_AGENTS_MD`, but the `read_agents_md()` exception handler at line 418 logs at ERROR level. For new chats this is expected behavior — downgrade to DEBUG to avoid false-positive alert fatigue. (`src/memory.py:418-421`)

### Security

- [ ] **Redact `chat_id` values (phone numbers) in `/metrics` per-chat token output** — `TokenUsage.get_top_chats()` returns raw `chat_id` values in the Prometheus metrics output. WhatsApp chat IDs are phone numbers (e.g., `1234567890@s.whatsapp.net`), which are PII. Exposing them in metrics endpoints (even HMAC-protected) violates data-minimization principles. Hash or truncate chat IDs in the Prometheus output (e.g., first 8 chars of SHA-256) while keeping the full mapping internal for operator queries. (`src/llm.py:109-120`, `src/health/server.py`)

- [ ] **Add `chat_id` validation in `TaskScheduler.add_task()` before path construction** — `add_task()` passes `chat_id` directly to `_prepare_task()` and `_persist()` without any validation. While `_resolve_tasks_path()` calls `sanitize_path_component()`, the in-memory `self._tasks[chat_id]` dict is keyed by the raw `chat_id`. A malformed `chat_id` (e.g., with path separators) would create an inconsistent state where the dict key doesn't match the sanitized filesystem path. Add `_validate_chat_id()` from `db.py` at the top of `add_task()`. (`src/scheduler.py:198-208`, `src/db/db.py:165-181`)

- [ ] **Add `prompt` length validation in `TaskScheduler._validate_task()`** — `_validate_task()` checks that `prompt` is a non-empty string but doesn't cap its length. A malicious or buggy skill could create a scheduled task with a multi-MB prompt that, when injected into the LLM context, exceeds token limits and wastes API credits. Add a `MAX_SCHEDULED_PROMPT_LENGTH` constant (e.g., 10_000 chars) and enforce it in `_validate_task()`. (`src/scheduler.py:150-165`, `src/constants.py`)

### Observability & Monitoring

- [ ] **Expose `Memory` cache hit/miss ratio as Prometheus metrics** — `Memory._cache_hits` and `_cache_misses` counters exist (lines 91–92) but are not exposed via the `/metrics` endpoint. For production operators, a low cache hit ratio indicates excessive filesystem reads that should be investigated. Add `custombot_memory_cache_hits_total` and `custombot_memory_cache_misses_total` counters to the Prometheus output. (`src/memory.py:91-92`, `src/monitoring/performance.py`, `src/health/server.py`)

- [ ] **Add ReAct loop iteration progress logging** — The ReAct loop (`_react_loop`) is silent between iterations — only the final result is logged. For debugging complex multi-step tool-call chains, operators have no visibility into which iteration the loop is on or what tool calls are being executed in real-time. Add a structured DEBUG log at the top of each iteration with `iteration=N, max_iterations=M, tool_count=K` so operators can trace stuck loops without modifying code. (`src/bot.py:1101-1166`)

- [ ] **Add `custombot_compression_summary_used_total` metric** — `_async_compressed_summary()` is called on every context assembly (line 131) but the result is only sometimes non-None (only when compression has occurred). Add a counter that increments when a compressed summary is actually used in context building, so operators can track compression effectiveness and identify chats that are hitting the compression threshold frequently. (`src/core/context_assembler.py:131`, `src/monitoring/performance.py`)

- [ ] **Track and expose `VectorMemory` embedding cache hit ratio** — `VectorMemory._embed_cache` has a max size of 256 but no hit/miss counters. When the cache is too small for the workload, repeated embedding API calls waste latency and credits. Add counters (`custombot_embed_cache_hits_total`, `custombot_embed_cache_misses_total`) and expose via `/metrics`. (`src/vector_memory.py:81-82`)

### Test Coverage

- [ ] **Add test for `VectorMemory._embed_batch()` deduplication and cache resolution** — `_embed_batch()` has complex logic: LRU cache check → in-flight dedup → API call → future resolution. There's no test covering: (a) a batch where some texts are cached and some aren't, (b) duplicate texts within the same batch, (c) the count validation when API returns fewer embeddings than requested. (`tests/unit/test_vector_memory.py`)

- [ ] **Add test for `_FileHandlePool` LRU eviction and stale-handle recovery** — `_FileHandlePool` manages a bounded pool of file handles with LRU eviction. No test verifies: (a) that handles are evicted when the pool exceeds `max_size`, (b) that a closed/stale handle is detected and reopened on next `get_or_open()`, (c) that `invalidate()` correctly removes a handle from the pool. (`tests/unit/test_db.py`)

- [ ] **Add test for `CircuitBreaker` state transitions under concurrent HALF_OPEN probes** — When the circuit breaker transitions to HALF_OPEN, multiple concurrent callers may pass `is_open()` before any records a result. Verify that: (a) only one success is needed to close, (b) a failure from any caller re-opens, (c) concurrent `record_success()` and `record_failure()` don't corrupt the state. (`tests/unit/test_llm.py` or new file)

- [ ] **Add test for `Memory.write_memory()` cache invalidation** — After `write_memory()` is called, the mtime cache for that chat should be invalidated so the next `read_memory()` re-reads from disk. Verify: (a) `_memory_cache.pop(chat_id)` is called, (b) the next read reflects the new content, (c) the cache miss counter increments. (`tests/unit/test_memory.py`)

- [ ] **Add test for `RoutingEngine._is_stale()` debounce behavior** — `_is_stale()` debounces mtime checks to avoid scanning on every match. Add a test verifying: (a) two calls within `ROUTING_WATCH_DEBOUNCE_SECONDS` only scan once, (b) a call after the debounce interval triggers a fresh scan, (c) rules are reloaded when an instruction file is modified. (`tests/unit/test_routing.py`)

- [ ] **Add integration test for `process_scheduled()` end-to-end with event emission** — Once event emission is added to `process_scheduled()`, verify: (a) `scheduled_task_started` event is emitted with correct `chat_id`, (b) `scheduled_task_completed` event is emitted after the response is persisted, (c) event data includes the response length. Subscribe a mock handler to verify emission. (`tests/integration/test_scheduled_pipeline.py`)

- [ ] **Add test for `_read_file_lines()` double-open elimination** — After consolidating the mmap path to avoid re-opening the file, verify: (a) the returned lines are identical to the previous implementation, (b) files at exactly the 64KB boundary are handled correctly, (c) an empty file returns an empty list, (d) a file with only a header line returns an empty list. (`tests/unit/test_db.py`)

- [ ] **Add test for `_IPLimiter` rate limiting with burst and cooldown** — Verify that: (a) requests within the limit are allowed, (b) requests exceeding the limit within the window are rejected with a `retry_after` value, (c) after the window expires, requests are allowed again, (d) LRU eviction works when `max_ips` is exceeded. (`tests/unit/test_health_security_headers.py` or new file)

### DevOps / Infrastructure

- [ ] **Add pip dependency caching to CI for faster builds** — The CI pipeline installs dependencies from scratch on every run (`pip install -r requirements.txt`). Add `actions/cache` with `~/.cache/pip` keyed on `requirements*.txt` hash to reduce install time from ~30s to ~5s on cache hit. (`.github/workflows/ci.yml`)

- [ ] **Add `dependabot.yml` for automated dependency update PRs** — Dependencies are pinned with `~=` version specifiers but there's no automated process to create PRs when new versions are published. Add a `.github/dependabot.yml` configured for pip and GitHub Actions with weekly review cadence and auto-assign to maintainers. (`.github/dependabot.yml`, new file)

- [ ] **Bump `--cov-fail-under` from 70 to 75 in CI** — Phase 14 adds 7+ new tests (above). If coverage exceeds 75% after these additions, update the threshold to lock in the improvement, following the roadmap documented in Phase 13 targeting 75% by 2026-06-15. (`.github/workflows/ci.yml:111`)
