# PLAN.md — CustomBot Improvement Roadmap

Generated from a senior codebase review on 2026-04-18.

---

## Refactoring

- [x] **Extract `_build_bot()` return type into a named dataclass** — `_build_bot()` returns an unnamed 4-tuple `(bot, db, vector_memory, project_store)` which is error-prone and hard to extend. Create a `BotComponents` dataclass so callers destructure by name, not position. (`src/builder.py`, `main.py`)

- [x] **Decouple Bot from concrete Memory/VectorMemory types via Protocol** — `Bot.__init__` accepts `Memory` (concrete class) and `ProjectStore` (Protocol). Make `Memory` a Protocol too so alternative implementations (e.g., Redis-backed) can be swapped in without touching `Bot`. (`src/bot.py`, `src/memory.py`, `src/utils/protocols.py`)

- [x] **Extract the `_process()` method's context-assembly stage into a standalone function** — `_process()` does 8 sequential steps in one 110-line method. Extract steps 3-5 (routing match, context build, memory reads) into a `_build_turn_context()` method so it can be unit-tested independently of the full ReAct loop. (`src/bot.py:594-706`)

- [x] **Centralize `message_exists` dedup logic** — Dedup is commented out in `handle_message()` ("redundant with preflight_check") but the comment and the skipped check create confusion. Either remove the comment and explicitly document the invariant, or keep a single authoritative dedup gate in one place. (`src/bot.py:395-398`)

- [x] **Unify config loading between CLI group and `start` command** — `cli()` group loads `load_config()` to read logging settings, then `start` command loads it again. Extract a `_load_config_once()` helper or cache the parsed Config on `ctx.obj`. (`main.py:392-417`, `main.py:494-498`)

- [x] **Move `_SessionMetrics` out of main.py into `src/monitoring/`** — `_SessionMetrics` is a metrics counter living in the CLI entry point. It belongs with `src/monitoring/performance.py` for cohesion and reuse. (`main.py:43-86`)

## Performance Optimization

- [x] **Add connection pooling or client reuse for VectorMemory SQLite** — `VectorMemory` opens a single SQLite connection via `SqliteHelper._open_connection()` but runs all writes through `asyncio.to_thread()` with a global `threading.Lock`. Under high concurrency this serializes all vector operations. Consider WAL mode (`PRAGMA journal_mode=WAL`) to allow concurrent reads while writes are serialized. (`src/vector_memory.py:70-78`)

- [x] **Batch embedding API calls in VectorMemory** — `save()` calls `_embed()` one text at a time. When multiple memories are saved in quick succession (e.g., a conversation with multiple `memory_save` skill calls), batch them into a single `embeddings.create(input=[...])` call to reduce API overhead and latency. (`src/vector_memory.py:161-174`)

- [x] **Implement request-level token budgeting in context builder** — `build_context()` loads history up to `DEFAULT_MEMORY_MAX_HISTORY` messages without checking token count. A 50-message conversation with long tool outputs can exceed the model's context window silently. Add an estimated token-count gate (e.g., `len(content) / 4` heuristic or tiktoken) to truncate intelligently. (`src/core/context_builder.py`)

- [x] **Add lazy loading for the routing engine** — `RoutingEngine.load_rules()` scans all `.md` files at startup. For large instruction directories, add file-watching (e.g., `watchdog`) to reload only when files change instead of requiring `refresh_rules()`. (`src/routing.py:231-277`)

## Error Handling & Resilience

- [x] **Add structured LLM error classification in `LLMClient.chat()`** — The `@retry_with_backoff` decorator retries all exceptions equally. Differentiate between retryable errors (rate limits, timeouts, 5xx) and non-retryable errors (auth failure, invalid model, context length exceeded). Parse the OpenAI error type and only retry on transient failures. (`src/llm.py:93-204`)

- [x] **Handle VectorMemory connection failures gracefully at startup** — `vector_memory.connect()` in `_build_bot()` will crash the entire startup if SQLite or the sqlite-vec extension fails to load. Wrap in a try/except that degrades to vector-memory-disabled mode with a clear warning, rather than a hard crash. (`src/builder.py:62-73`)

- [x] **Add a circuit breaker for repeated LLM failures** — If the LLM provider is down, every incoming message triggers a full timeout wait (up to `DEFAULT_LLM_TIMEOUT=120s`). Implement a circuit breaker pattern: after N consecutive failures, short-circuit for a cooldown period and return a "service temporarily unavailable" message immediately. (`src/llm.py`, `src/bot.py`)

- [x] **Validate disk space on VectorMemory writes** — `Database` checks disk space before writes but `VectorMemory` does not. An out-of-disk condition during SQLite writes can corrupt the vector database. Add the same `_check_disk_space_before_write()` guard. (`src/vector_memory.py`, `src/db/db.py:440-465`)

## Observability & Monitoring

- [x] **Add Prometheus-compatible `/metrics` endpoint alongside `/health`** — The health server (`src/health/`) exposes a JSON health check. Add a `/metrics` endpoint that exposes token usage, message latency percentiles, queue depth, and active chat count in Prometheus text format for external monitoring integration. (`src/health/server.py`)

- [x] **Track and expose per-skill execution metrics** — `PerformanceMetrics` tracks skill times but doesn't expose per-skill counters (calls, errors, avg time). Add a skill metrics registry so operators can identify which skills are slowest or most error-prone. (`src/monitoring/performance.py`)

- [x] **Add correlation-ID propagation to scheduled tasks** — `process_scheduled()` sets `correlation_id=f"sched_{chat_id}"` which is not unique per execution. Use `f"sched_{chat_id}_{uuid_hex}"` to enable tracing of individual scheduled task executions in logs. (`src/bot.py:510`)

## Test Coverage

- [x] **Add integration test for the full message pipeline** — There are unit tests for individual components and E2E tests for CLI commands, but no integration test that exercises: incoming message → preflight → routing match → LLM call → tool execution → response delivery, all with real (in-memory) components. Add a `tests/integration/test_message_pipeline.py`. (`tests/`)

- [x] **Add tests for `builder._build_bot()` wiring correctness** — `_build_bot()` wires 8+ components together but has zero test coverage. Add tests verifying that the returned components are correctly interconnected (e.g., Bot has the right DB, skills have the right vector_memory, routing rules are loaded). (`tests/unit/test_builder.py`)

- [x] **Add chaos/failure tests for crash recovery** — `recover_pending_messages()` has basic unit tests but no tests for edge cases like: partial recovery (some messages succeed, some fail), recovery with corrupted queue data, or recovery during active message processing. Add a dedicated test file. (`tests/unit/test_recovery_chaos.py`)

- [x] **Add property-based tests for routing rule matching** — Routing rules support regex patterns, priority ordering, fromMe/toMe filtering, and wildcard matching. The current tests are example-based. Use Hypothesis to generate random routing rule/message combinations and verify matching invariants (e.g., only one rule matches per message, disabled rules never match). (`tests/unit/test_routing.py`)

---

## Phase 2 — Senior Review (2026-04-19)

Generated from a second-pass codebase audit covering all `src/` modules,
`tests/`, and infrastructure files.

---

### Refactoring

- [x] **Replace bare `Dict[str, Any]` return types with typed dataclasses in `build_context()`** — `build_context()` returns `list[dict[str, Any]]`, forcing every caller to know the OpenAI message schema. Introduce a `ChatMessage` dataclass (role, content, optional name) and have `build_context()` return `list[ChatMessage]` with a `.to_api_dict()` method. This isolates the OpenAI wire format to `llm.py` and makes context manipulation type-safe. (`src/core/context_builder.py`, `src/bot.py`)

- [x] **Extract the NeonizeBackend from WhatsAppChannel into its own file** — `whatsapp.py` is 841 lines mixing channel orchestration (historical message handling, backpressure, safe mode) with low-level neonize protocol details (JID parsing, message extraction, event handlers). Split into `channels/whatsapp_channel.py` (the BaseChannel subclass, ~300 lines) and `channels/neonize_backend.py` (the transport layer, ~400 lines). (`src/channels/whatsapp.py`)

- [x] **Consolidate the two config-loading paths in `load_config()` into a single constructor call** — `load_config()` manually plucks keys from the parsed JSON dict with `data.get(...)` fallbacks (lines 544-567), bypassing the generic `_from_dict()` helper it already uses for `LLMConfig`. This means new Config fields must be added in two places. Refactor to use `_from_dict(Config, data)` uniformly, with env-var overrides applied after. (`src/config/config.py:531-567`)

- [x] **Remove the `MemoryConfig` backward-compat alias** — `MemoryConfig` at the bottom of `config.py` is marked "deprecated" with a comment but is never imported anywhere. Dead code. Remove it. (`src/config/config.py:640-643`)

- [x] **Introduce a `close()` lifecycle method on `VectorMemory`** — `VectorMemory` opens a SQLite connection and registers a sqlite-vec extension but never exposes a `close()` method. The builder's shutdown path in `perform_shutdown()` would need to call it for clean resource release. Add `close()` that calls `self._db.close()`. (`src/vector_memory.py`, `src/shutdown.py`)

- [x] **Move `_session_token_usage` global singleton into the `LLMClient` constructor** — `_session_token_usage` is a module-level global in `llm.py`, making tests that check token counts fragile across test isolation boundaries. Accept a `TokenUsage` instance in `LLMClient.__init__()` (already partially supported via the `token_usage` parameter) and remove the global default. (`src/llm.py:151-157`)

### Performance Optimization

- [x] **Add connection pooling for the OpenAI AsyncClient** — `LLMClient.__init__()` creates a single `AsyncOpenAI` instance with default `httpx` settings. Under concurrent multi-chat usage, a connection pool with explicit `max_connections` and `timeout` settings would reduce TCP handshake overhead and avoid implicit queueing in httpx. Configure `http_client=httpx.AsyncClient limits=httpx.Limits(max_connections=20, max_keepalive_connections=10)`. (`src/llm.py:169-172`)

- [x] **Implement eager stale-check eviction in `MessageQueue._load_pending()`** — On startup, `_load_pending()` reads the entire queue file and loads all pending messages into memory. For a long-running bot with frequent restarts, the queue file can grow large with completed entries. Add eager eviction: discard completed entries during load and rewrite the file with only pending entries. (`src/message_queue.py:374-421`)

- [x] **Cache `RoutingEngine.match_with_rule()` results for identical message signatures** — The routing engine re-evaluates all rules on every call, even for identical message patterns. Add a small TTL cache keyed on `(fromMe, toMe, sender_id, channel_type, text[:100])` to short-circuit repeated matches for the same chat within a short window. (`src/routing.py:334-377`)

- [x] **Use `aiosqlite` or `sqlite3` in WAL mode with connection-per-operation for VectorMemory reads** — `VectorMemory._search_sync()` uses a single shared `sqlite3.Connection` with WAL mode, but all reads still go through `asyncio.to_thread()` with the GIL. For read-heavy workloads, open a separate read-only connection per search to allow true concurrent reads without blocking the write lock. (`src/vector_memory.py:386-414`)

- [x] **Debounce `_check_disk_space_before_write()` calls** — Both `Database` and `VectorMemory` call `check_disk_space()` before every single write. On a fast message stream, this `statvfs` syscall adds measurable I/O latency. Cache the result for N seconds (e.g. 30s) and only re-check after the TTL expires. (`src/db/db.py:440-465`, `src/vector_memory.py:90-102`)

### Error Handling & Resilience

- [x] **Handle `json.JSONDecodeError` from malformed tool call arguments gracefully in `_react_loop()`** — In `_process_tool_calls()`, `json.loads(tool_call.function.arguments)` is wrapped in a try/except, but the LLM can also return malformed `tool_calls` structures (missing `function`, missing `name`). Add a top-level try/except around the entire tool-call iteration to catch structural issues and return a structured error message to the LLM so it can self-correct. (`src/bot.py:898-929`)

- [x] **Add retry logic for WhatsApp send failures in `_send_message()`** — `WhatsAppChannel._send_message()` splits text into chunks and sends them sequentially, but if the second chunk fails, the first chunk was already sent. Add per-chunk retry (1 attempt) with a short delay, and log partial delivery warnings. (`src/channels/whatsapp.py:597-603`)

- [x] **Guard against `None` return from `_build_turn_context()` in `process_scheduled()`** — `process_scheduled()` doesn't call `_build_turn_context()` (it builds context inline), but it also doesn't validate that the workspace directory creation succeeds. Add a try/except around `ensure_workspace()` and `build_context()` in `process_scheduled()` to handle disk-full or permission errors gracefully. (`src/bot.py:551-572`)

- [x] **Add timeout to `Database.get_recent_messages()` reverse-seek algorithm** — `_read_file_lines()` does a binary reverse-seek for large files but has no timeout. A corrupted file with missing newlines could cause an infinite loop in the seek. Add a max-iteration cap (e.g. 10,000 chunks) and fall back to simple deque read. (`src/db/db.py:819-865`)

- [x] **Validate `IncomingMessage` fields in `_extract_message()` before returning** — `_extract_message()` can return a dict with empty `chat_id` or `sender_id` if the neonize protobuf has unexpected fields. Add validation: if `chat_str` or `sender_id` is empty, log a warning and return `None`. (`src/channels/whatsapp.py:687-731`)

### Security

- [x] **Sanitize skill arguments before passing to `json.loads()` in ToolExecutor** — `ToolExecutor.execute()` parses `tool_call.function.arguments` as JSON, but doesn't validate the resulting dict for unexpected keys or excessively nested structures. A malicious LLM response could craft deeply nested JSON causing stack overflow. Add a max-depth check (e.g. 10 levels) on the parsed args. (`src/core/tool_executor.py:72-87`)

- [x] **Add rate limiting to the health check HTTP server** — The health server (`src/health/server.py`) has no rate limiting. An attacker could flood `/health` or `/metrics` endpoints. Add per-IP rate limiting (e.g. 60 requests/minute) using a simple sliding window. (`src/health/server.py`)

- [x] **Validate workspace directory traversal in `Memory.ensure_workspace()`** — `ensure_workspace()` uses `sanitize_path_component(chat_id)` but doesn't verify the resulting path is still within `workspace_root/whatsapp_data/`. Add a `resolve()` check to ensure the final path doesn't escape the workspace root via symlink attacks. (`src/memory.py:102-131`)

### Observability & Monitoring

- [x] **Add structured logging for all skill executions with duration and result status** — `ToolExecutor.execute()` logs skill start but the structured `extra` dict doesn't include the execution duration or whether the result was success/error. Add `duration_ms`, `result_status`, and `error_type` to the structured log so log aggregation tools can build skill health dashboards. (`src/core/tool_executor.py:119-173`)

- [x] **Track and expose LLM token usage in the Prometheus `/metrics` endpoint** — `TokenUsage` accumulates prompt/completion tokens but the health server's `/metrics` endpoint doesn't expose them. Add `custombot_llm_prompt_tokens_total` and `custombot_llm_completion_tokens_total` counters to the metrics export. (`src/health/server.py`, `src/llm.py`)

- [x] **Add startup health check that validates all component wiring** — Currently there's no automated check that the bot's components are correctly wired after `_build_bot()`. Add a `validate_wiring()` method to `Bot` that asserts `_db`, `_llm`, `_memory`, `_skills`, `_routing` are all non-None and logs the result. Call it from `_run_bot()` after `_build_bot()` and expose the result in the `/health` endpoint. (`src/bot.py`, `src/builder.py`, `src/health/checks.py`)

- [x] **Add per-chat message count metrics for identifying high-volume chats** — `PerformanceMetrics` tracks aggregate message counts but doesn't break down by chat. Add a bounded LRU counter (top-N chats by message count) so operators can identify which chats generate the most load. (`src/monitoring/performance.py`)

### Test Coverage

- [x] **Add integration test for the scheduled task pipeline** — There's a `test_message_pipeline.py` for normal messages but no equivalent for `Bot.process_scheduled()`. Add a test that exercises: scheduler trigger → `process_scheduled()` → LLM call → response delivery, verifying that scheduled messages bypass routing/dedup correctly. (`tests/integration/test_scheduled_pipeline.py`)

- [x] **Add unit tests for `MessageQueue` crash recovery edge cases** — `test_message_queue.py` covers basic enqueue/complete but not: (a) recovery with a corrupted JSONL file, (b) compaction during concurrent enqueue/complete, (c) stale timeout with messages that are exactly at the boundary. (`tests/unit/test_message_queue.py`)

- [x] **Add tests for the NeonizeBackend connection lifecycle** — The `NeonizeBackend` class has complex reconnection logic (`_reconnect()`, `_watchdog()`) that is untested. Mock the neonize client and verify: (a) watchdog detects disconnection and reconnects, (b) `_wait_for_connection()` timeout behavior, (c) message queue bridge from thread to asyncio. (`tests/unit/test_neonize_backend.py`)

- [x] **Add tests for `config.load_config()` environment variable overrides** — `load_config()` supports `OPENAI_API_KEY` and `OPENAI_BASE_URL` env var overrides, but there are no tests for this. Add tests verifying: (a) env var overrides config file values, (b) env var is used when config file key is missing, (c) env var is logged as redacted. (`tests/unit/test_config_env.py`)

- [x] **Add test for `ToolExecutor` handling of malformed tool calls** — `ToolExecutor.execute()` handles JSON parse errors but not: (a) tool_call with missing `function` attribute, (b) tool_call with None arguments, (c) skill that raises an unhandled exception type. Add parameterized tests for these edge cases. (`tests/unit/test_tool_executor.py`)

- [x] **Add test for `_split_text()` message chunking edge cases** — `_split_text()` splits long messages at newline/space boundaries but is untested for: (a) messages exactly at the limit, (b) messages with no spaces or newlines (force break), (c) empty string, (d) single character over limit. (`tests/unit/test_text_splitting.py`)

---

## Phase 3 — Senior Review (2026-04-19)

Generated from a third-pass deep codebase audit covering all `src/` modules,
`tests/`, wiring correctness, and production readiness gaps.

---

### Refactoring

- [x] **Extract `_run_bot()` into a dedicated `Application` class in `src/app.py`** — `_run_bot()` in `main.py` is 200+ lines of wiring logic that creates, connects, and orchestrates every subsystem (scheduler, channel, health server, message handler callback, shutdown). It mixes infrastructure lifecycle with business logic (the `on_message` closure). Extract this into a class that encapsulates the full application lifecycle with named methods for each phase (`_startup()`, `_wire_scheduler()`, `_on_message()`, `_shutdown()`), making the startup sequence testable without a running WhatsApp connection. (`main.py:110-325`)

- [x] **Wire `MessageQueue` into the Bot in `_build_bot()`** — `Bot.__init__()` accepts an optional `message_queue` parameter, and crash recovery (`recover_pending_messages()`) is fully implemented, but `_build_bot()` never creates or passes a `MessageQueue`. This means the entire crash recovery subsystem (persistent queue, stale detection, ACL-checked reprocessing) is dead code in production. Create and connect a `MessageQueue` in the builder, pass it to `Bot`, and call `recover_pending_messages()` during startup. (`src/builder.py:45-198`, `src/bot.py:234-342`, `src/message_queue.py`)

- [x] **Add `LLMClient.close()` to the graceful shutdown sequence** — `perform_shutdown()` in `lifecycle.py` closes channel, scheduler, health server, vector memory, project store, and database, but never calls `llm.close()` to release the httpx connection pool. The `LLMClient` is created in `_build_bot()` and has a `close()` method, but it is not accessible from the shutdown sequence because it is not returned in `BotComponents`. Add `llm` to `BotComponents` and call `llm.close()` during shutdown step 5 (alongside vector memory and project store). (`src/lifecycle.py:273-294`, `src/builder.py:34-43`, `src/llm.py:348-351`)

- [x] **Fix `QueuedMessage` missing `sender_id` field for crash recovery ACL checks** — `QueuedMessage.from_incoming_message()` captures `sender_name` but not `sender_id`. During crash recovery, `bot.recover_pending_messages()` falls back to `getattr(queued_msg, "sender_id", None)` which always returns `None`, then uses `sender_name` for ACL checks via `channel._is_allowed()`. If the allowed_numbers list stores phone JIDs rather than display names, every recovered message will be skipped by ACL. Add a `sender_id` field to `QueuedMessage` and populate it from `msg.sender_id` in `from_incoming_message()`. (`src/message_queue.py:106-115`, `src/bot.py:274-284`)

- [x] **Combine `Memory.read_memory()` and `read_agents_md()` dual-asyncio-to-thread calls into single thread hops** — Each method calls `await asyncio.to_thread(path.stat)` then conditionally calls `await asyncio.to_thread(path.read_text, ...)`. This schedules two separate thread pool operations for a single file read. Consolidate into one `asyncio.to_thread()` call per method that does stat + read in a single thread hop, reducing event loop overhead and halving the number of pending futures for each memory access. (`src/memory.py:148-168`, `src/memory.py:353-368`)

- [x] **Remove dead `IncomingMessage.metadata` access in `QueuedMessage.from_incoming_message()`** — `QueuedMessage.from_incoming_message()` at line 114 does `getattr(msg, "metadata", {})` but `IncomingMessage` has no `metadata` attribute (only `message_id`, `chat_id`, `sender_id`, `sender_name`, `text`, `timestamp`, `channel_type`, `fromMe`, `toMe`, `is_historical`, `correlation_id`, `raw`). This always returns `{}`. Either add `metadata` to `IncomingMessage` (if channels need to pass extra data) or remove the dead attribute access and default to `{}` explicitly. (`src/message_queue.py:114`, `src/channels/base.py:28-71`)

- [x] **Use `loop.call_soon_threadsafe()` for `GracefulShutdown.request_shutdown()` from signal handlers** — Signal handlers run in the main thread, but `request_shutdown()` calls `self._shutdown_event.set()` directly. On platforms where the event loop runs in a different thread (or when using `asyncio.run()` with signal handlers on Windows), this is not thread-safe. Wrap the `set()` call in `loop.call_soon_threadsafe(self._shutdown_event.set)` to guarantee correct cross-thread notification. (`src/shutdown.py:55-59`, `src/shutdown.py:149-180`)

### Performance Optimization

- [x] **Pool read connections in `VectorMemory` instead of open-close-per-query** — `_search_sync()`, `list_recent()`, and `count()` each open a fresh read-only SQLite connection, load the sqlite-vec extension, execute one query, then close it. For high-frequency semantic searches (e.g., `memory_recall` skill in rapid conversation), this extension-loading overhead per query is significant. Implement a `threading.local`–based connection pool where each thread gets a reused read connection, reducing per-query overhead from ~5ms to <1ms. (`src/vector_memory.py:118-138`, `src/vector_memory.py:422-496`)

- [x] **Add CJK-aware token estimation in `estimate_tokens()`** — `CHARS_PER_TOKEN = 4` is calibrated for English. For CJK text (Chinese, Japanese, Korean), the ratio is ~1-2 characters per token. For multilingual conversations, `estimate_tokens()` underestimates by 2-4x, potentially causing the `_trim_history_to_budget()` gate to allow too many tokens through and exceed the model's context window. Detect CJK character ranges and apply a lower chars-per-token ratio (e.g., 1.5) for those segments. (`src/core/context_builder.py:61-63`, `src/constants.py:85`)

- [x] **Track and log `SessionMetrics.skills_executed` counter** — `SessionMetrics` has `increment_skills()` and `_skills` counter, but the `on_message` callback in `_run_bot()` only calls `increment_messages()` and `increment_errors()`. The skill execution counter is never incremented in production, so the shutdown summary always reports `skills_executed: 0`. Wire the counter through `ToolExecutor` or the `on_message` callback. (`main.py:237`, `src/monitoring/performance.py:69-74`)

### Error Handling & Resilience

- [x] **Handle channel disconnect gracefully in `on_message` error path** — The `on_message` closure in `_run_bot()` calls `await channel.send_message(msg.chat_id, error_msg)` in the `except` block (line 288). If the WhatsApp channel has disconnected (network failure, neonize crash), this `send_message` will raise, masking the original error and potentially causing an unhandled exception in the finally block. Wrap the error-path `send_message` in its own try/except and log the secondary failure without re-raising. (`main.py:279-288`)

- [x] **Add schema migration path for VectorMemory** — `_ensure_schema()` uses `CREATE TABLE IF NOT EXISTS` with no version tracking. If a future release adds a column (e.g., `tags TEXT` to `memory_entries`), the table won't have it and queries will fail silently or with cryptic SQL errors. Add a `schema_version` metadata table and implement a migration function that applies `ALTER TABLE ADD COLUMN` statements incrementally. (`src/vector_memory.py:142-162`)

- [x] **Guard against `process_scheduled()` concurrent execution for the same chat** — `process_scheduled()` acquires no per-chat lock, unlike `handle_message()` which uses the `_chat_locks` LRU cache. If a scheduled task fires while a user message is being processed for the same chat, both the user response and scheduled response will interleave their database writes and LLM calls, potentially corrupting conversation history. Reuse the same `_chat_locks` mechanism in `process_scheduled()`. (`src/bot.py:542-662`)

### Observability & Monitoring

- [x] **Add task scheduler status to the health check endpoint** — The `/health` endpoint checks database, WhatsApp, memory, performance, and wiring, but not the task scheduler. A stopped scheduler (e.g., due to an unhandled exception in `scheduler.start()`) or one with many failed executions would go unnoticed. Add a `check_scheduler()` function that reports: (a) whether the scheduler is running, (b) number of active tasks, (c) count of recent execution failures. (`src/health/checks.py`, `src/health/server.py`, `src/scheduler.py`)

- [x] **Add circuit breaker state to the Prometheus `/metrics` endpoint** — The circuit breaker protects against cascading LLM failures but its state (open/closed/half-open, failure count, last failure time) is not exposed in the `/metrics` endpoint. Add `custombot_llm_circuit_breaker_state` (gauge: 0=closed, 1=half-open, 2=open) and `custombot_llm_circuit_breaker_failures_total` (counter) so operators can detect provider outages from Grafana alerts. (`src/health/server.py:167-384`, `src/utils/circuit_breaker.py`)

- [x] **Add startup duration and component init timing to structured logs** — `_log_startup_complete()` logs total duration but doesn't track per-component initialization time. If one component is slow (e.g., VectorMemory loading sqlite-vec), there's no way to tell which one from the logs. Add per-component timing in the builder's `ProgressBar` and log each component's duration in the startup summary. (`src/builder.py:45-198`, `src/lifecycle.py:129-148`)

### Test Coverage

- [x] **Add integration test for `perform_shutdown()` ordered cleanup sequence** — `perform_shutdown()` executes 6 ordered cleanup steps and has zero test coverage. Add tests verifying: (a) all 6 cleanup steps execute in the correct order, (b) a failing step doesn't skip subsequent steps, (c) the shutdown timeout correctly forces exit, (d) LLM client close is included (once the LLM wiring fix is done). (`tests/integration/test_shutdown_sequence.py`)

- [x] **Add tests for `context_builder._sanitize_history()` migration fast-path** — `_sanitize_history()` has an optimization that skips iteration when all messages have `_sanitized=True`. This fast-path and the migration tracking (unscanned count) have no direct tests. Add tests for: (a) all-sanitized fast path returns the same list, (b) mixed sanitized/unsanitized messages scan only unsanitized ones, (c) injection detection in unsanitized messages triggers sanitization. (`tests/unit/test_context_builder.py`)

- [x] **Add tests for `MessageQueue` concurrent enqueue/complete race conditions** — The queue uses `asyncio.Lock` for thread safety, but there are no tests for concurrent operations: (a) two coroutines enqueueing for the same chat simultaneously, (b) completing a message while another coroutine is loading pending, (c) compaction triggered during concurrent completions. These are the most likely failure modes in production. (`tests/unit/test_message_queue.py`)

- [x] **Add tests for the `_embed_batch()` cache and in-flight dedup interaction** — `_embed_batch()` has complex logic for deduplicating concurrent embedding requests across LRU cache and in-flight futures. No tests exercise: (a) a batch where some texts are in cache and others need API calls, (b) a batch where in-flight futures resolve during processing, (c) error propagation cancels all pending futures. (`tests/unit/test_vector_memory.py`)

- [x] **Add property-based test for `QueuedMessage` serialization round-trip** — `QueuedMessage.to_dict()` → `QueuedMessage.from_dict()` should be an identity transform, but there are no property-based tests verifying this across edge cases: empty strings, None fields, Unicode characters, very long text. Use Hypothesis to generate arbitrary `QueuedMessage` instances and verify round-trip fidelity. (`tests/unit/test_message_queue.py`)

- [x] **Add tests for `GracefulShutdown` signal handler thread safety** — `request_shutdown()` is called from signal handlers (main thread) but sets an `asyncio.Event` that is awaited on the event loop. No tests verify: (a) `request_shutdown()` correctly transitions `accepting_messages` to False, (b) `wait_for_in_flight()` respects the timeout, (c) `enter_operation()` rejects after shutdown is requested. (`tests/unit/test_shutdown.py`)

---

## Phase 4 — Senior Review (2026-04-19)

Generated from a fourth-pass deep codebase audit covering the full `src/` tree,
`tests/`, cross-module contracts, and production readiness gaps not caught in
Phases 1–3.

---

### Refactoring

- [x] **Type `Application._channel` properly instead of `object | None` with `# type: ignore`** — `Application._channel` is typed as `object | None`, forcing 15+ `# type: ignore[union-attr]` directives across `_startup()`, `_wire_scheduler()`, `_on_message()`, and `_shutdown_cleanup()`. After `_startup()` completes, the channel is always a `WhatsAppChannel`. Either widen the type annotation to `BaseChannel | None`, use a post-init assertion pattern, or introduce a `@property` that returns the concrete type after startup. This eliminates a whole class of silent type-safety holes. (`src/app.py:70, 80, 145, 172, 176, 181, 227, 256, 258, 262, 280, 296–306`)

- [x] **Fix `_split_text()` stripping leading whitespace from continuation chunks** — `_split_text()` in `whatsapp.py` does `text = text[idx:].lstrip()` after splitting, which strips leading spaces/newlines from the beginning of every chunk after the first. This mangles formatted content like code blocks, indented lists, or markdown tables that span across chunk boundaries. Preserve the split boundary by only stripping the newline character that was used as the split point, not all whitespace. (`src/channels/whatsapp.py:360-376`)

- [x] **Deduplicate `InstructionLoader` instances between `Bot` and `SkillRegistry`** — `_build_bot()` creates an `InstructionLoader` at line 172 for `skills.load_builtins()`, and then `Bot.__init__()` creates a second `InstructionLoader` at line 155 for its own `_load_instruction()` method. Both scan the same `instructions/` directory with independent mtime caches. Consolidate into a single shared instance passed to both `SkillRegistry.load_builtins()` and `Bot.__init__()`, reducing filesystem stat calls by half during routing and skill loading. (`src/builder.py:171-172`, `src/bot.py:155`, `src/core/instruction_loader.py`)

- [x] **Fix Prometheus per-skill latency metrics using duplicate metric names without labels** — `_build_prometheus_output()` emits `custombot_skill_latency_milliseconds` once per skill in a loop, each with identical metric name but different quantile values. Prometheus treats these as the same metric family, causing confusing query results and cardinality issues. Use a `skill` label: `custombot_skill_latency_milliseconds{skill="web_search"}` so that per-skill metrics are properly queryable and aggregatable in Grafana. (`src/health/server.py:299-321`)

### Performance Optimization

- [x] **Move `path.exists()` checks in `Memory` methods into the `asyncio.to_thread()` call** — `Memory.read_memory()` and `read_agents_md()` both call `path.exists()` synchronously (a blocking filesystem I/O call) before the `asyncio.to_thread()` hop. On a busy event loop with many concurrent chats, these synchronous stats add latency. Move the existence check inside the thread pool alongside the stat+read, so the event loop is never blocked on filesystem syscalls. (`src/memory.py:160-161, 366-367`)

- [x] **Add argument payload size validation in `ToolExecutor.execute()`** — `ToolExecutor.execute()` validates nesting depth (`MAX_ARGS_DEPTH = 10`) but not total argument size. A compromised or confused LLM could emit a tool call with a multi-megabyte JSON arguments string (e.g., `write_file` with a huge `content` parameter), causing memory pressure and bloated message history in the ReAct loop. Add a max-bytes guard (e.g., `len(raw_args) > MAX_ARGS_BYTES`) that rejects oversized payloads before JSON parsing. (`src/core/tool_executor.py:88-103`)

- [x] **Lazy-initialize `Database` asyncio locks to avoid event-loop-bound construction at import time** — `Database.__init__()` creates `asyncio.Lock()` instances (`_chats_lock`, `_index_lock`) at construction time. If a `Database` is instantiated before the asyncio event loop is running (e.g., during testing, module-level setup, or Windows ProactorEventLoop initialization), the locks may be bound to the wrong loop. Follow the pattern from `base.py:_get_safe_mode_lock()` and lazy-initialize locks on first use, or document that `Database()` must only be called within a running event loop. (`src/db/db.py:153-155`, `src/channels/base.py:82-94`)

### Error Handling & Resilience

- [x] **Guard against empty `resp.data` in `VectorMemory._embed_batch()`** — `_embed_batch()` iterates `enumerate(resp.data)` without verifying that `len(resp.data) == len(unique_texts)`. If the embeddings API returns fewer embeddings than requested (e.g., due to content filtering, empty input strings, or API bugs), the `for batch_pos, item in enumerate(resp.data)` loop will silently skip pending indices, leaving `results[i]` as `None` for those texts and causing an opaque downstream error. Add a length check and raise a descriptive error if counts don't match. (`src/vector_memory.py:349-370`)

- [x] **Ensure typing indicator is cleared on all error paths in `NeonizeBackend.send()`** — `send()` sets typing to composing at the start, then enters a try/finally that sets it to paused. But if `_reconnect()` succeeds and the retry `send_message` raises a non-connection error (e.g., invalid JID), the `raise retry_exc` at line 417 bypasses the finally block's `set_typing(False)` because the outer `try/finally` in `send()` already covers it. However, if `_reconnect()` itself raises, the typing indicator may remain on because the reconnection failure path at line 416 re-raises before the outer finally. Verify and add an explicit `set_typing(False)` in the catch path of the retry block. (`src/channels/neonize_backend.py:398-420`)

- [x] **Add graceful handling for `process_scheduled()` LLM timeout without response** — `process_scheduled()` calls `self._react_loop()` which can return the circuit-breaker "temporarily unavailable" message or an empty response, but `process_scheduled()` then calls `parse_meta(raw_response)` and persists the response as-is. If the circuit breaker returned the "⚠️ Service temporarily unavailable" message, it gets saved as a normal assistant message in the DB and delivered to the user via the scheduler. Add a check: if the response matches known error patterns, skip persistence and delivery, and log the failure instead. (`src/bot.py:619-654`)

### Security

- [x] **Improve phone number normalization in `WhatsAppChannel._is_allowed()`** — `_is_allowed()` normalizes by stripping `+`, spaces, and dashes, but doesn't handle: (a) `00` international prefix (`0049123456789` vs `+49123456789`), (b) parentheses in formatted numbers (`(123) 456-7890`), or (c) leading `0` in national format when comparing against E.164 numbers. Add a `_normalize_phone()` helper that handles these common formats and apply it consistently to both the sender ID and the allowed_numbers list. (`src/channels/whatsapp.py:316-327`)

- [x] **Replace private attribute access for circuit breaker state in health server** — `_handle_metrics()` accesses circuit breaker state via `self._bot._llm.circuit_breaker` (double private attribute access). This breaks encapsulation and would fail silently if either attribute is renamed. Add a public `get_circuit_breaker()` method on `LLMClient` (already has the `circuit_breaker` property) and expose `get_llm_status()` on `Bot` that returns circuit breaker state without leaking internal structure. (`src/health/server.py:668-671`)

### Observability & Monitoring

- [x] **Track ReAct loop iteration count per conversation in metrics** — `Bot._react_loop()` iterates up to `max_tool_iterations` times but never records how many iterations each conversation uses. Operators have no visibility into which conversations approach the limit or how many tool calls are typical. Add a `track_react_iterations(count)` method to `PerformanceMetrics` and expose `custombot_react_iterations_total` as a Prometheus histogram, enabling alerts when conversations consistently hit the iteration ceiling. (`src/bot.py:807-892`, `src/monitoring/performance.py`)

- [x] **Add `Database` operation latency tracking to `PerformanceMetrics`** — `PerformanceMetrics` has `_db_latencies` deque and `_db_op_count` counter but nothing in the codebase ever calls `track_db_latency()` (the method doesn't even exist). The `_db_op_count` is incremented nowhere. Either implement the DB latency tracking by adding `track_db_latency()` calls in `Database.save_message()`, `get_recent_messages()`, and `_save_chats()`, or remove the dead `_db_latencies` / `_db_op_count` fields and their snapshot keys to avoid confusion. (`src/monitoring/performance.py:318, 330`, `src/db/db.py`)

### Test Coverage

- [x] **Add test for `Application` class lifecycle** — The `Application` class (307 lines) encapsulates startup, scheduler wiring, message handling, and shutdown, but has zero dedicated test coverage. Add a test that verifies: (a) `_startup()` initializes all components in the correct order, (b) `_wire_scheduler()` connects scheduler callbacks to the bot and channel, (c) `_on_message()` correctly handles the preflight → typing → handle → send flow, (d) `_shutdown_cleanup()` calls `perform_shutdown()` with all components. Use mocked components to avoid WhatsApp dependency. (`tests/unit/test_application.py`, `src/app.py`)

- [x] **Add test for `Config.save_config()` → `load_config()` round-trip** — `save_config()` serializes a `Config` to JSON with schema validation, and `load_config()` deserializes it back. There are no tests verifying that a round-trip preserves all fields correctly (especially `Optional[int]` fields like `max_tokens` that default to `None`, nested dataclasses like `NeonizeConfig`, and list fields like `allowed_numbers`). Add a test that creates a `Config` with non-default values, saves, loads, and asserts equality. (`tests/unit/test_config_roundtrip.py`, `src/config/config.py`)

- [x] **Add test for `VectorMemory._embed_batch()` API error propagation** — When the embeddings API call fails in `_embed_batch()`, the code sets exceptions on all pending futures and re-raises. No test verifies: (a) all futures receive the exception, (b) in-flight entries are cleaned up, (c) the LRU cache is not polluted with partial results, (d) a subsequent batch call after failure succeeds. (`tests/unit/test_vector_memory.py`)

- [x] **Add test for `_split_text()` formatting preservation** — `_split_text()` is used to chunk WhatsApp messages at 4000 chars, but its whitespace handling (`.lstrip()` on continuation chunks) can mangle formatted content. Add tests for: (a) multi-line code blocks split across chunks, (b) indented bullet lists, (c) markdown table rows split at boundaries, (d) Unicode content at the split boundary, (e) message exactly at the limit with trailing newline. (`tests/unit/test_text_splitting.py`, `src/channels/whatsapp.py:360-376`)

---

## Phase 5 — Senior Review (2026-04-19)

Generated from a fifth-pass deep codebase audit covering production readiness,
operational concerns, cross-module contracts, and infrastructure gaps not
addressed in Phases 1–4.

---

### Infrastructure & DevOps

- [x] **Add CI/CD pipeline with GitHub Actions** — The project has no CI/CD configuration (no `.github/workflows/`, no `Makefile`, no `Dockerfile`). The only automated quality gate is `.pre-commit-config.yaml` (ruff + trailing whitespace). Add a GitHub Actions workflow that runs: (a) ruff lint + format check, (b) mypy type checking, (c) pytest with coverage report, (d) the full E2E test suite on Python 3.11+. This catches regressions before merge and enforces the quality standards already configured in `pyproject.toml`. (`.github/workflows/ci.yml`, new file)

- [x] **Add Dockerfile and `.dockerignore` for containerized deployment** — The bot is designed to run long-lived on a server, but there is no containerization support. Add a multi-stage Dockerfile: (a) build stage with dev dependencies for running tests, (b) runtime stage with only production dependencies, (c) non-root user for security, (d) health check using the existing `/health` endpoint. Add `.dockerignore` to exclude `workspace/`, `.data/`, `__pycache__/`, `.opencode/`, and `.tmp/`. (`Dockerfile`, `.dockerignore`, new files)

### Performance Optimization

- [x] **Execute independent tool calls in parallel within the ReAct loop** — `_process_tool_calls()` iterates over `choice.message.tool_calls` and executes each one sequentially via `self._tool_executor.execute()`. When the LLM requests multiple independent tools (e.g., `memory_save` + `file_write`), executing them concurrently with `asyncio.gather()` would reduce total turn latency. The tool results must still be appended to `messages` in the original order for correct LLM context. (`src/bot.py:936-1050`)

- [x] **Move `psutil.cpu_percent(interval=0.1)` off the event loop in `PerformanceMetrics.get_snapshot()`** — `get_snapshot()` calls `psutil.cpu_percent(interval=0.1)` synchronously, blocking the event loop for 100ms on every call. This is triggered by the `/health` endpoint and periodic metrics logging. Wrap in `asyncio.to_thread()` or cache the CPU reading and only refresh every N seconds. Same applies to `psutil.virtual_memory()` which is faster but still blocking I/O. (`src/monitoring/performance.py:517-525`)

- [x] **Add TTL-based cache invalidation for embedding API health check** — `VectorMemory._embed()` and `_embed_batch()` call the OpenAI embeddings API without any upfront health check. If the embeddings endpoint is down, the first symptom is a full timeout on every memory operation. Add a lightweight cached health check (e.g., embed a known string like `"health"` and cache the result for 60s) so that repeated failures short-circuit immediately instead of waiting for the full API timeout on every call. (`src/vector_memory.py:238-288`)

### Error Handling & Resilience

- [x] **Add retry with exponential backoff for scheduled task execution** — `_execute_task()` catches exceptions and increments `_failure_count`, but never retries the task. Transient failures (LLM timeout, network blip) cause the scheduled task to silently fail until the next scheduled interval (which could be hours for daily tasks). Add a configurable retry policy (e.g., 2 retries with exponential backoff: 30s, 120s) before marking the task as failed. Only retry on transient error types (timeout, connection), not on permanent failures (authentication, invalid prompt). (`src/scheduler.py:273-322`)

- [x] **Stop memory monitoring during graceful shutdown** — `Bot.start_memory_monitoring()` starts a periodic `psutil` check, but `perform_shutdown()` never calls `stop_memory_monitoring()`. The background monitoring task continues running during the shutdown sequence, potentially logging spurious warnings and holding resources. Add `bot.stop_memory_monitoring()` call in shutdown step 5 (alongside vector memory and message queue cleanup). (`src/lifecycle.py:286-320`, `src/bot.py:185-220`)

- [x] **Ensure Database message-index atomicity across crash boundaries** — `save_message()` writes to the JSONL file first (via `_append_to_file`), then updates the in-memory `_message_id_index`. If the process crashes between these two operations, the index will be stale on next startup (missing the last-written message ID). The existing index-rebuild-on-corruption logic mitigates this, but add an explicit acknowledgment: after `_append_to_file` succeeds, call `_save_message_index()` asynchronously (debounced) so the on-disk index is never more than one message behind. (`src/db/db.py:647-745`)

### Refactoring

- [x] **Remove private `_client` attribute access in WhatsApp media-sending methods** — `send_audio()` accesses `self._backend._client` directly (double underscore private attribute), and `send_document()` does the same. This breaks encapsulation — if `NeonizeBackend` renames or removes `_client`, these methods silently break. Add public `send_audio()` and `send_document()` methods to `NeonizeBackend` and delegate from the channel, following the same pattern as `send()` and `set_typing()`. (`src/channels/whatsapp.py:255-314`, `src/channels/neonize_backend.py`)

- [x] **Add protocol-based type for `ToolCall` to eliminate `Any` in ReAct loop** — `_react_loop()` and `_process_tool_calls()` use `Any` for the `choice` parameter and `tool_call` iteration, bypassing type checking entirely. The OpenAI SDK provides typed classes (`ChatCompletion`, `Choice`, `ChatCompletionMessageToolCall`), but the bot code unpacks them via `choice.message.tool_calls[0].function.name` with no type narrowing. Import and use the SDK types explicitly to catch attribute-access errors at mypy time instead of runtime. (`src/bot.py:845-1050`)

- [x] **Consolidate `format_user_error()` and `to_user_message()` into a single formatting path** — `CustomBotException.to_user_message()` and `format_user_error()` produce nearly identical output (emoji + message + suggestion + error code) but are implemented independently. `format_user_error()` adds correlation ID support; `to_user_message()` does not. Consolidate so `format_user_error()` delegates to `to_user_message()` with an optional correlation_id parameter, eliminating the duplicate formatting logic. (`src/exceptions.py:152-179`, `src/exceptions.py:385-437`)

### Observability & Monitoring

- [x] **Add LLM log rotation to prevent unbounded disk growth** — `LLMLogger` writes full request/response JSON to `workspace/logs/llm/` with no rotation or cleanup strategy. For a long-running bot, these logs accumulate indefinitely. Add: (a) max log file size with rotation (e.g., 10MB per file, keep last 5), (b) optional max age cleanup (delete logs older than N days), (c) log the current log directory size in the `/health` endpoint. (`src/logging/llm_logging.py`)

- [x] **Populate `DOCS_URLS` with actual documentation links** — `exceptions.py` defines `DOCS_URLS` with all `None` values and a TODO comment "Replace with actual documentation URLs once hosted". Five phases of improvements later, users still see no documentation links in error messages. Either: (a) host a docs site and populate the URLs, or (b) link to the project README / `.opencode/context/` documentation paths, or (c) remove the dead `DOCS_URLS` dict and `docs_url` parameter to avoid misleading users with empty links. (`src/exceptions.py:78-85`)

- [x] **Add per-chat conversation depth metric to Prometheus** — The ReAct iteration count is tracked globally but not broken down by chat. Operators cannot identify which chats have the deepest conversations (most tool calls per turn). Add a `custombot_chat_conversation_depth` gauge (per top-N chats) showing the last ReAct iteration count, enabling alerts for chats that consistently hit the max iteration ceiling. (`src/monitoring/performance.py`, `src/health/server.py`)

### Security

- [x] **Add command allowlist/denylist for the shell execution skill** — The `shell` skill executes arbitrary commands in the workspace with no command-level restrictions. While path traversal is prevented, the LLM could be social-engineered into running destructive commands (`rm -rf`, `curl | bash`, `shutdown`). Add a configurable allowlist/denylist in `config.json` (default: deny destructive patterns like `rm -rf /`, `mkfs`, `dd if=`, `shutdown`, `reboot`). Log denied commands for security auditing. (`src/skills/builtin/shell.py`, `src/config/config.py`)

- [x] **Sanitize LLM response before persisting to conversation history** — `Bot._process()` calls `filter_response_content()` to remove sensitive content from the LLM response before sending to the user, but then persists the original `raw_response` (before filtering) to the database via `save_message()`. If the LLM leaks an API key or PII, it is permanently stored in the JSONL history. Persist the filtered version instead. (`src/bot.py:812-841`)

### Test Coverage

- [x] **Add load/stress test for concurrent multi-chat message processing** — The bot uses per-chat locks (`LRULockCache`) and shared resources (LLM client, database, vector memory). There are no tests for concurrent message processing across multiple chats simultaneously. Add a test that: (a) processes 10+ messages from different chats concurrently, (b) verifies no cross-chat data leakage in database writes, (c) confirms per-chat locks prevent concurrent LLM calls for the same chat, (d) validates message queue integrity under concurrent load. (`tests/integration/`, new file)

- [x] **Add test for `RoutingEngine` cache invalidation on file modification** — `RoutingEngine.match_with_rule()` caches match results with a TTL and auto-reloads on file mtime changes. No test verifies: (a) cache is correctly invalidated when an instruction file is modified, (b) new rules appear after file creation, (c) removed rules disappear after file deletion, (d) mtime debounce prevents excessive reloads. (`tests/unit/test_routing.py`)

- [x] **Add test for `VectorMemory` read connection pool thread-safety** — `_get_read_connection()` uses `threading.local()` to create per-thread read connections. No test verifies: (a) multiple threads can read concurrently without blocking, (b) read connections are properly closed on `close()`, (c) a read connection sees data committed by a write on the main connection. (`tests/unit/test_vector_memory.py`)

---

## Phase 6 — Senior Review (2026-04-20)

Generated from a sixth-pass deep codebase audit covering cross-cutting
concerns, API contract correctness, resource lifecycle gaps, and
production hardening not addressed in Phases 1–5.

---

### Refactoring

- [x] **Extract `_normalize_phone()` from `whatsapp.py` into a shared utility** — `_normalize_phone()` is a pure function currently living as a private module-level function in `channels/whatsapp.py`. It is tested separately (`test_phone_normalization.py`) and could be reused by future channels (Telegram, Discord) or by the `NeonizeBackend` for JID normalization. Move it to `src/utils/phone.py` and import it from both `whatsapp.py` and any future channel implementations. (`src/channels/whatsapp.py:323-334`)

- [x] **Replace `is_incoming_message()` duck-type guard with structural `isinstance` check** — `is_incoming_message()` in `type_guards.py` checks `msg.text` and `msg.message_id` attributes via duck-typing rather than using `isinstance(msg, IncomingMessage)`. Since `IncomingMessage` is a frozen `@dataclass(slots=True)`, an `isinstance` check is both faster (no attribute access) and more precise (rejects arbitrary objects with `.text` and `.message_id`). Update the guard and remove the `type_guards.py` function in favor of direct `isinstance`. (`src/utils/type_guards.py`, `src/bot.py:394,431`)

- [x] **Centralize tool-call-to-dict serialization into a standalone function** — `LLMClient.tool_call_to_dict()` is a `@staticmethod` that converts an OpenAI `ChatCompletionMessage` into a plain dict. It is the only place in the codebase that manually constructs the `tool_calls` wire format. Extract to a standalone function (e.g. `serialize_tool_calls()`) in a new `src/core/serialization.py` module so it can be unit-tested independently and reused if additional LLM providers are added. (`src/llm.py:359-376`)

- [x] **Remove duplicate `pytest.ini` that conflicts with `pyproject.toml`** — Both `pytest.ini` and `pyproject.toml` contain pytest configuration (`testpaths`, `asyncio_mode`, `-v --tb=short`). Having two sources of truth risks divergence and confusion. Remove `pytest.ini` and keep all pytest configuration in `pyproject.toml` under `[tool.pytest.ini_options]`, which is the modern standard. (`pytest.ini`, `pyproject.toml`)

- [x] **Replace `_split_text()` line-by-line algorithm with a textwrap-based approach** — `_split_text()` manually iterates over text with `rfind("\n")` / `rfind(" ")` logic. Python's `textwrap.wrap()` handles the same job more robustly (handles CJK, zero-width spaces, etc.) and is standard-library. Replace with `textwrap.wrap(text, width=limit, replace_whitespace=False, drop_whitespace=False)` and adapt the chunk assembly to preserve the existing formatting guarantees. (`src/channels/whatsapp.py:363-380`)

### Performance Optimization

- [x] **Cache `_normalize_phone()` results with `functools.lru_cache`** — `_is_allowed()` calls `_normalize_phone()` for every incoming message AND normalizes the entire `allowed_numbers` set on each call. The allowed_numbers set rarely changes within a session. Cache the normalized set as a property on the channel instance (invalidate when config changes) to avoid re-normalizing the entire list on every message. (`src/channels/whatsapp.py:309-315`)

- [x] **Use `asyncio.TaskGroup` instead of `asyncio.gather` for tool call execution** — `_process_tool_calls()` uses `asyncio.gather()` which silently collects exceptions until all tasks finish. If one tool call has a catastrophic error (e.g., infinite loop), the others still complete, but the error handling is deferred. `asyncio.TaskGroup` (Python 3.11+) provides structured concurrency with immediate cancellation of siblings on `BaseException`, giving better error isolation and cleaner stack traces. (`src/bot.py:1011-1016`, `src/lifecycle.py:277`)

- [x] **Pre-compute the `estimate_tokens()` CJK character set check** — `estimate_tokens()` iterates character-by-character checking Unicode ranges on every call. For long messages (up to 50,000 chars), this is O(n) per call. Pre-build a `frozenset` of CJK character ranges and use a single `any()` / set intersection, or cache the result on `ChatMessage` creation (since messages are frozen) to avoid recomputing on every `_trim_history_to_budget` call. (`src/core/context_builder.py:61-95`)

- [x] **Optimize `_sanitize_history()` fast-path with a bitflag instead of checking all messages** — The fast-path iterates all messages checking `m._sanitized or m.role != "user"`. For a 50-message history, this is 50 comparisons per turn. Track a `_has_unsanitized` flag on the list level (or use a counter of unsanitized messages) so the fast-path is O(1) rather than O(n). This flag gets decremented when unsanitized messages are dropped by `_trim_history_to_budget`. (`src/core/context_builder.py:247-297`)

### Error Handling & Resilience

- [x] **Add reconnection backoff to `NeonizeBackend._watchdog()`** — The watchdog attempts reconnection every 5 seconds indefinitely when the internet is available but WhatsApp is disconnected. If the WhatsApp server is throttling or rate-limiting, rapid reconnection attempts may exacerbate the problem. Add exponential backoff (5s → 10s → 20s → 60s max) for consecutive reconnection failures, reset on success. (`src/channels/neonize_backend.py:454-488`)

- [x] **Handle `asyncio.CancelledError` in `_on_message()` error path** — `_on_message()` in `Application` has a `try/finally` that calls `shutdown_mgr.exit_operation(op_id)`, but the inner `except Exception as exc` block catches generic exceptions. If a `CancelledError` propagates during shutdown (e.g., `handle_message()` is cancelled by shutdown timeout), the `format_user_error(exc)` call tries to format a `CancelledError`, which is unexpected. Add an explicit `except asyncio.CancelledError` handler that re-raises after logging, before the generic `except Exception`. (`src/app.py:262-316`)

- [x] **Add a startup health check for embedding model availability** — `VectorMemory` connects to SQLite successfully even if the embedding model name is invalid (e.g., `"text-embedding-3-smalll"` typo). The error only surfaces on the first `save()` or `search()` call. Add a lightweight probe during `_build_bot()` — call `_embed("health")` with a short timeout — to catch misconfigured embedding models at startup instead of silently failing at runtime. (`src/builder.py:99-128`)

- [x] **Guard against `save_message()` throwing during `_process()` user-turn persistence** — `_process()` calls `save_message()` at line 806 to persist the user turn before building context. If this write fails (disk full, permission error, corrupted JSONL), the method raises, but the message is already enqueued in `MessageQueue` and will be retried on crash recovery, potentially causing duplicate LLM calls. Wrap the user-turn `save_message()` in a try/except that logs the error and returns `None` rather than propagating, since the message can still be processed without persisted history. (`src/bot.py:804-812`)

- [x] **Add a maximum retry budget to `_raw_chat()` to prevent unbounded retry duration** — `@retry_with_backoff(max_retries=3)` on `_raw_chat()` retries on ALL exceptions, including timeouts. With a 120s LLM timeout and 3 retries, a single LLM call can block for up to 360s + backoff delays (~370s). This exceeds the shutdown timeout (30s), making graceful shutdown impossible during prolonged LLM failures. Add a total-timeout budget (e.g., `max_total_seconds=180`) to the retry decorator that caps cumulative wait time across all attempts. (`src/llm.py:201-296`, `src/utils/retry.py`)

### Security

- [x] **Add `Content-Security-Policy` and `X-Content-Type-Options` headers to health server responses** — The health server returns JSON/HTML without security headers. If the `/metrics` or `/health` endpoint is exposed to a browser (e.g., via port forwarding), the response could be interpreted as HTML and enable XSS. Add `Content-Type: application/json`, `X-Content-Type-Options: nosniff`, and `Content-Security-Policy: default-src 'none'` headers to all responses. (`src/health/server.py`)

- [x] **Redact API keys from LLM log files** — `LLMLogger.log_request()` writes the full request to disk, including the `Authorization: Bearer sk-...` header if present in the httpx request trace. While logs are local, this creates a credential-at-rest risk. Add a `_redact_secrets()` helper that strips `api_key`, `Authorization`, and `Bearer` values before writing to disk. (`src/logging/llm_logging.py`)

- [x] **Validate `skill.name` is alphanumeric on skill registration** — `SkillRegistry` accepts any string as a skill name from both built-in and user-loaded skills. A malicious user skill with a name containing special characters (e.g., `"skill"; DROP TABLE--`) could cause unexpected behavior in log messages, Prometheus metrics, or SQL queries if the name is ever interpolated. Add validation at registration time: reject names containing characters outside `[a-z0-9_]`. (`src/skills/__init__.py`)

### Observability & Monitoring

- [x] **Add structured `correlation_id` to all `log.error()` calls in the ReAct loop** — Error logs in `_react_loop()`, `_process_tool_calls()`, and `_execute_tool_call()` use `extra={"chat_id": chat_id}` but omit `correlation_id`. During multi-chat debugging, correlating error logs to specific request flows requires both. Add `correlation_id` to the `extra` dict in all error-level logs within the ReAct loop. (`src/bot.py:868-1085`)

- [x] **Track and expose `Database` file sizes in the health check endpoint** — The `/health` endpoint checks database connectivity but doesn't report on disk usage. A JSONL file for an active chat can grow to hundreds of megabytes, silently consuming disk. Add a `check_disk_usage()` function that sums the size of `workspace/.data/` and `workspace/` directories and includes `db_size_mb` and `workspace_size_mb` in the health report. (`src/health/checks.py`, `src/health/server.py`)

- [x] **Add a `/ready` endpoint distinct from `/health` for Kubernetes-style probes** — The `/health` endpoint checks component wiring, database, and LLM credentials but doesn't distinguish between "started" and "ready to serve traffic". In Kubernetes, `liveness` and `readiness` probes serve different purposes. Add a `/ready` endpoint that returns 200 only when all components (including WhatsApp channel connection) are fully initialized and the bot is accepting messages. (`src/health/server.py`)

### Test Coverage

- [x] **Add integration test for `Application.run()` full lifecycle with mocked channel** — `Application` has unit tests for individual methods (`_startup`, `_wire_scheduler`, `_on_message`) but no integration test that exercises the complete `run()` method end-to-end: startup → channel start → message handling → shutdown signal → cleanup. Add a test using a mock channel that delivers a test message, then triggers shutdown, verifying all lifecycle phases execute correctly. (`tests/integration/test_application_lifecycle.py`)

- [x] **Add test for `_normalize_phone()` edge cases** — `_normalize_phone()` handles `00` prefix, `0` national prefix, and digit stripping, but there are no tests for: (a) empty string input, (b) pure alphabetic input (non-phone JIDs), (c) numbers with country code already present (`+491234567890` vs `0123456789`), (d) group JIDs (`123456789-1234567890@g.us`), (e) numbers shorter than expected (< 7 digits). (`tests/unit/test_phone_normalization.py`)

- [x] **Add test for `Scheduler._is_due()` timezone edge cases** — `_is_due()` converts local target times to UTC using `local_offset * 60` (integer truncation of fractional hours). Timezones like India (UTC+5:30) or Nepal (UTC+5:45) have fractional offsets. Add tests verifying: (a) `daily` tasks fire at the correct minute for UTC+5:30, (b) `cron` tasks respect fractional offsets, (c) tasks don't fire twice when the offset calculation rounds incorrectly. (`tests/unit/test_scheduler.py`)

- [x] **Add test for `VectorMemory` concurrent read-write isolation** — Verify that a long-running search query on a read connection returns a consistent snapshot even while a concurrent write inserts new entries on the main connection. This validates the WAL-mode isolation guarantee that is critical for correctness but currently untested. (`tests/unit/test_vector_memory.py`)

- [x] **Add test for `MessageQueue._load_pending()` file corruption recovery** — `_load_pending()` reads the entire JSONL file and falls back to an empty queue on `Exception`. Add tests for: (a) file with binary garbage mixed into JSON lines, (b) file with only completion markers (no pending), (c) file where the last line is truncated (no trailing newline — partial write from crash), (d) empty file. Verify each case produces a valid in-memory state without data loss. (`tests/unit/test_message_queue.py`)

- [x] **Add test for `Bot.preflight_check()` + `handle_message()` dedup consistency** — Verify that a message rejected by `preflight_check()` with `reason="duplicate"` is also rejected by `handle_message()` and does not produce duplicate LLM calls. This guards against a race condition where `preflight_check` passes but `handle_message()` sees the same message as a duplicate because another coroutine processed it between the two calls. (`tests/integration/test_message_pipeline.py`)

- [x] **Add regression test for `_split_text()` preserving markdown code blocks across chunks** — `_split_text()` splits at newline boundaries but could break a markdown code block (` ``` `) across chunks, causing WhatsApp rendering issues. Add tests for: (a) code block starting in one chunk and ending in the next, (b) inline code with backticks at the split boundary, (c) nested formatting (`*bold `code`*`) spanning the limit. (`tests/unit/test_text_splitting.py`)

---

## Phase 7 — Senior Review (2026-04-21)

Generated from a seventh-pass deep codebase audit covering the full `src/` tree,
`tests/`, cross-module contracts, architectural evolution, and production
readiness gaps not addressed in Phases 1–6.

---

### Refactoring

- [x] **Extract message-handling middleware chain from `Application._on_message()`** — `_on_message()` is a monolithic 60-line method that chains together session-metrics increment, logging, preflight, typing, handle_message, send_message, error handling, and finally shutdown-exit — all in one deeply-nested try/except/finally. Extract each concern into discrete middleware functions (or a `Pipeline` class) that can be composed, reordered, and unit-tested independently. This would make it trivial to add new cross-cutting concerns (e.g., per-chat audit logging, message-transform hooks) without bloating the method further. (`src/app.py:245-324`)

- [x] **Decouple `Bot` from concrete `LRULockCache` via constructor-injected lock factory** — `Bot.__init__()` directly instantiates `LRULockCache(max_size=MAX_LRU_CACHE_SIZE)` for per-chat locks and creates two separate `RateLimiter` instances. This makes it impossible to share lock state across bot instances (e.g., for testing with a real scheduler that also acquires chat locks) or to swap in a distributed lock backend for multi-process deployments. Accept a `lock_factory: Callable[[str], AsyncContextManager]` or similar protocol, defaulting to the current `LRULockCache`. (`src/bot.py:159-163`)

- [x] **Move `_SCHEDULED_ERROR_PREFIXES` to a shared constants or exceptions module** — `bot.py` defines `_SCHEDULED_ERROR_PREFIXES` as a module-private tuple used to detect error responses that should not be persisted. As error response patterns grow (e.g., new error types from the circuit breaker or LLM client), maintaining this list scattered across modules becomes fragile. Centralize into `src/exceptions.py` or `src/constants.py` alongside the other error-related constants. (`src/bot.py:94-98`)

- [x] **Introduce a `Channel` Protocol/ABC to formalize the channel contract** — `BaseChannel` defines abstract methods (`start`, `close`, `send_message`, `send_typing`) but `Bot.process_scheduled()` and `_process()` access channel-specific methods like `get_channel_prompt()` that are only on `WhatsAppChannel`, not on `BaseChannel`. This means the type hints say `BaseChannel | None` but the runtime contract expects `WhatsAppChannel`. Add `get_channel_prompt()` to `BaseChannel` (with a default `None` return) so all callers can rely on the abstract interface without `isinstance` checks or import-time coupling. (`src/channels/base.py`, `src/bot.py:625`, `src/bot.py:768`)

- [x] **Replace `from src.config import Config` pattern with dependency injection in `Bot`** — `Bot` stores `self._cfg` and reads config fields at runtime (`self._cfg.llm.max_tool_iterations`, `self._cfg.memory_max_history`). The config is a large frozen structure; reading individual fields deep in the call stack creates hidden coupling. Instead, extract the specific config values Bot needs (`max_tool_iterations`, `memory_max_history`, etc.) into a `BotConfig` dataclass that `Bot.__init__()` accepts, making the dependency surface explicit and testable. (`src/bot.py:149`, `src/bot.py:902`)

### Performance Optimization

- [x] **Avoid redundant `build_context()` call in `process_scheduled()` by reusing `TurnContext` flow** — `process_scheduled()` duplicates the context-assembly logic from `_build_turn_context()` (reading memory, agents_md, project context, topic cache, then calling `build_context()`). If a routing rule matched the chat (even for scheduled tasks), the assembled context could include the instruction and be built once. Refactor to share the context-assembly path, reducing code duplication and ensuring scheduled tasks benefit from the same token-budget trimming and sanitization that normal messages get. (`src/bot.py:627-643`, `src/bot.py:732-792`)

- [x] **Lazy-initialize `asyncio.Lock` in `MessageQueue` to prevent event-loop binding issues** — `MessageQueue.__init__()` creates `asyncio.Lock()` at construction time. If a `MessageQueue` is instantiated before the asyncio event loop is running (e.g., in a test fixture or during module-level setup on Windows with `ProactorEventLoop`), the lock may bind to a stale or wrong loop. Follow the pattern used in `base.py:_get_safe_mode_lock()` and lazily initialize the lock on first `async with` call, or document that `MessageQueue()` must only be called within a running event loop. (`src/message_queue.py:175`)

- [x] **Pre-compute and cache `RoutingRule` match results for wildcard-only rules** — Many routing rules use `*` (match-all) for sender, recipient, channel, and content_regex, differing only in `fromMe`/`toMe` flags and priority. For these trivial rules, the current per-field regex matching is wasteful. Add a `is_wildcard` flag computed in `__post_init__()` that short-circuits the match evaluation to only check `fromMe`/`toMe`, skipping all regex/compiled-pattern evaluation. This reduces matching latency for the common case of catch-all rules. (`src/routing.py:163-168`, `src/routing.py:408-432`)

- [x] **Batch `Database.save_message()` writes for multi-tool ReAct responses** — In `_process()`, after the ReAct loop completes, a single `save_message()` call persists the assistant turn. But during the loop, `_process_tool_calls()` may trigger multiple skill executions that each produce tool-result messages. If the LLM produces many tool calls per turn, the serialized message writes accumulate. Consider buffering tool-result messages and persisting them in a single batched write alongside the final assistant turn, reducing the number of `asyncio.to_thread()` hops and JSONL appends. (`src/bot.py:867-872`, `src/db/db.py`)

- [x] **Use `orjson` or `ujson` for JSON (de)serialization in hot paths** — The codebase uses stdlib `json` everywhere (message queue, database reads/writes, config loading, tool-call argument parsing). For hot paths like `db.py` JSONL reads/writes, `message_queue.py` serialization, and `vector_memory.py` batch operations, switching to `orjson` or `ujson` would yield 2-5x serialization speedup with minimal code changes. Add as an optional dependency with stdlib fallback. (`src/db/db.py`, `src/message_queue.py`, `src/vector_memory.py`)

### Error Handling & Resilience

- [x] **Handle `GracefulShutdown._in_flight_lock` being bound to wrong event loop on Windows** — `GracefulShutdown.__init__()` creates `asyncio.Lock()` at construction time, but `register_signal_handlers()` may be called from a different thread or the lock may be bound to an event loop that gets replaced (common on Windows with `ProactorEventLoop`). If `enter_operation()` or `exit_operation()` is called on a new loop, the lock will raise `RuntimeError: Task got Future attached to a different loop`. Lazy-initialize the lock on first use, matching the pattern from `base.py`. (`src/shutdown.py:42`)

- [x] **Guard against `process_scheduled()` producing `None` response_text that breaks `parse_meta()`** — `process_scheduled()` calls `self._react_loop()` which can return `None` as `response_text` in certain edge cases (e.g., circuit breaker returning an error string, empty response). If `response_text` is `None`, `parse_meta(None)` will crash with `AttributeError: 'NoneType' has no attribute 'startswith'`. Add a `None` guard before `parse_meta()` and handle it the same as the empty-response error prefix. (`src/bot.py:655-665`)

- [x] **Add timeout wrapper around `channel.start()` in `Application.run()`** — `Application.run()` calls `await self.channel.start(self._on_message)` which blocks until the QR code is scanned or `MAX_QR_WAIT` elapses. However, there's no outer timeout — if `start()` hangs due to a neonize bug or network issue, the bot never reaches the `try/finally` block that enables shutdown handling. Wrap `channel.start()` in `asyncio.wait_for()` with a generous timeout (e.g., 5 minutes) and surface a clear error on timeout. (`src/app.py:106-111`)

- [x] **Sanitize or truncate excessively long messages in `QueuedMessage.text` before queue persistence** — A malicious or buggy sender could produce an extremely large message (up to `MAX_MESSAGE_LENGTH = 50_000` chars) that gets persisted to the message queue JSONL file. On crash recovery, `_load_pending()` reads the entire file into memory, including these giant payloads. Add a `MAX_QUEUED_TEXT_LENGTH` constant (e.g., 10_000 chars) and truncate `QueuedMessage.text` during enqueue so the queue file doesn't grow unboundedly. (`src/message_queue.py:231-259`)

- [x] **Handle `Database` file-handle exhaustion under extreme load** — `Database.get_recent_messages()` opens the JSONL file for each read via `_read_file_lines()`, and `save_message()` opens it for each append. Under extreme concurrency (many chats all active simultaneously), the OS file-handle limit could be reached. Implement a bounded file-handle pool or reuse a single file handle per chat within the lock scope, reducing open/close syscalls and preventing `OSError: [Errno 24] Too many open files`. (`src/db/db.py`)

### Security

- [x] **Add audit logging for skill execution with arguments** — The `shell` skill now has a command denylist, but there is no persistent audit log of what commands were executed, by which chat, and whether they were allowed or denied. Add a structured audit log file (e.g., `workspace/logs/audit.jsonl`) that records every skill execution with `{timestamp, chat_id, skill_name, args_hash, allowed, result_summary}`. This is essential for post-incident forensics and compliance. (`src/core/tool_executor.py:166-200`, `src/security/audit.py`)

- [x] **Validate and sanitize `IncomingMessage.channel_type` against a whitelist** — `IncomingMessage.channel_type` is a free-form string that flows from the channel into routing, logging, and metrics. If the neonize backend or a future channel produces an unexpected `channel_type` (e.g., containing path separators or special characters), it could cause unexpected behavior in log aggregation or metrics systems. Add validation in `IncomingMessage.__post_init__()` that `channel_type` is alphanumeric or from a known set. (`src/channels/base.py:28-71`)

- [x] **Add request signing or HMAC verification for health server endpoints** — The health server exposes operational data (token usage, circuit breaker state, queue depth) that could reveal internal architecture details. While rate limiting is in place, there is no authentication — anyone with network access can query `/health` and `/metrics`. Add optional HMAC-based request verification (configured via environment variable) so that only authorized monitoring agents can query the endpoints, while unauthenticated requests receive only a basic 200/503 status code. (`src/health/server.py`)

### Observability & Monitoring

- [x] **Add per-turn token usage tracking to structured logs** — `_raw_chat()` logs token usage every 10th request as a session total, but individual turn-level token usage (prompt_tokens, completion_tokens) is only tracked in the `TokenUsage` accumulator, not in structured log fields. Operators cannot query logs to identify which specific conversations consume the most tokens. Add `prompt_tokens` and `completion_tokens` to the structured `extra` dict on every `_raw_chat()` call so that log aggregation tools can build per-chat cost dashboards. (`src/llm.py:252-284`)

- [x] **Expose scheduler task execution history in the `/health` endpoint** — The scheduler's `get_status()` returns aggregate counts (success_count, failure_count) but no details about which tasks recently failed, when they last ran, or their next scheduled execution. Add a `recent_executions` list (last 10) to the scheduler status with `{task_id, chat_id, status, timestamp, error_summary}` and expose it in the health report, enabling operators to diagnose stalled or failing scheduled tasks without checking logs. (`src/scheduler.py:110-124`, `src/health/checks.py`)

- [x] **Track and expose `Memory` cache hit/miss ratio** — `Memory` uses `LRUDict` caches for both memory content and agents_md, but there's no metric tracking cache effectiveness. If the cache is too small (causing frequent evictions), operators have no visibility into the resulting disk I/O overhead. Add `_memory_cache_hits`, `_memory_cache_misses` counters and expose them in the Prometheus `/metrics` endpoint. (`src/memory.py:84-85`, `src/monitoring/performance.py`)

- [x] **Add a `/version` endpoint to the health server** — The health server exposes health, readiness, and metrics but has no endpoint that reports the bot's version. `src/__version__.py` contains the version string. Add a `/version` endpoint that returns `{"version": "x.y.z", "python": "3.11.x"}` to aid fleet management and rolling update verification. (`src/health/server.py`, `src/__version__.py`)

### Test Coverage

- [x] **Add test for `Bot.process_scheduled()` with `None` response from `_react_loop()`** — `_react_loop()` can return `None` as `response_text` when the circuit breaker is open or the LLM produces an empty response. There is no test verifying that `process_scheduled()` handles this gracefully without crashing. Add a test that mocks `_react_loop()` to return `None` and verifies the method returns `None` without persisting anything. (`tests/unit/test_bot.py`)

- [x] **Add test for `Application.run()` shutdown during QR-wait phase** — If the user presses Ctrl+C while the QR code is being displayed (before WhatsApp connects), the `run()` method should handle the cancellation cleanly. There is no test for this common user scenario. Add a test that triggers shutdown during `channel.start()` and verifies no resource leaks or unhandled exceptions. (`tests/integration/test_application_lifecycle.py`)

- [x] **Add integration test for `VectorMemory` schema migration path** — `_migrate_schema()` has infrastructure for incremental schema migrations but `_MIGRATIONS` is currently empty. When the first migration is added, there will be no test verifying that an existing database migrates correctly from version 0 to version 1 (and from 1 to 2, etc.). Add a test that creates a version-0 database, runs `_migrate_schema()`, and verifies the new columns/tables exist. (`tests/unit/test_vector_memory.py`)

- [x] **Add test for `RateLimiter.check_message_rate()` consuming slots on allowed messages** — `check_message_rate()` correctly uses the two-phase `check_only()` + `record()` pattern, but there's no test verifying that allowed messages actually consume a slot and that the slot is correctly reflected in subsequent `check_only()` calls. Add a parameterized test that verifies slot consumption and exhaustion. (`tests/unit/test_rate_limiter.py`)

- [x] **Add chaos test for concurrent `Database.save_message()` and `get_recent_messages()` on the same chat** — The database uses per-chat `asyncio.Lock` but there is no test exercising concurrent read+write on the same chat file at the same time. This is the most common production pattern (one coroutine reading history while another appends a message). Add a test that runs `save_message()` and `get_recent_messages()` concurrently on the same chat_id and verifies neither produces corrupted data. (`tests/integration/test_concurrent_load.py`)

- [x] **Add test for `NeonizeBackend._watchdog()` exponential backoff reset** — The watchdog now uses exponential backoff on consecutive reconnection failures, but there is no test verifying that the backoff resets to the initial delay after a successful reconnection. Add a test that simulates multiple failed reconnection attempts followed by a successful one, then verifies the next failure starts from the initial delay. (`tests/unit/test_neonize_backend.py`)

- [x] **Add test for `ToolExecutor` with skill that returns non-string result** — Skills return arbitrary types from `execute()`, and `ToolExecutor` converts them with `str(result)`. But there's no test for edge cases: (a) skill returns `None` (currently handled with `""` fallback), (b) skill returns a large binary object that `str()` would serialize verbosely, (c) skill returns a dict or list. Verify the `str(result)` conversion handles these gracefully without crashing the ReAct loop. (`tests/unit/test_tool_executor.py`)

- [x] **Add test for `Config` schema validation rejecting unknown keys** — `load_config()` uses `_from_dict()` to map config fields, but there is no test verifying that unknown/unexpected keys in `config.json` are either warned about or ignored. A user with a typo in their config (e.g., `"llm_mode": "fast"` instead of `"llm": {...}`) would silently get defaults. Add a test that loads a config with unknown top-level and nested keys and verifies appropriate warnings are logged. (`tests/unit/test_config_roundtrip.py`)

- [x] **Add end-to-end test for the full graceful shutdown with an in-flight LLM call** — The shutdown sequence waits for in-flight operations, but there is no test that exercises the specific scenario where a long-running LLM call is in progress when shutdown is requested. Verify that: (a) the in-flight operation completes, (b) the shutdown timeout correctly forces exit if the LLM call exceeds the timeout, (c) the response is persisted before shutdown. (`tests/integration/test_shutdown_sequence.py`)

---

## Phase 8 — Senior Review (2026-04-21)

Generated from an eighth-pass codebase audit covering user experience,
operational gaps, cross-platform correctness, and architectural evolution
not addressed in Phases 1–7.

---

### Refactoring

- [x] **Introduce a lightweight event bus for cross-component decoupling** — Components communicate through direct method calls (e.g., `Bot` calls `_metrics.track_*()`, `_tool_executor.execute()`, `_message_queue.enqueue()`). Adding new cross-cutting concerns (e.g., plugin hooks, audit trails, analytics) requires modifying core classes. Introduce a simple typed event bus (`EventBus.emit("skill_executed", ...)` / `EventBus.on("skill_executed", callback)`) so that extensions and plugins can subscribe to events (message_received, skill_executed, response_sent, error_occurred, shutdown_started) without modifying `Bot`, `ToolExecutor`, or `Application`. (`src/core/event_bus.py`, new file)

- [x] **Make middleware pipeline dynamically extensible via config** — `Application._build_pipeline()` hardcodes seven middleware classes in a fixed order. Adding or removing middleware (e.g., a rate-limit middleware, a plugin-injected middleware) requires editing `app.py`. Refactor the pipeline to accept a configurable middleware list (loaded from a config section or a plugin registry), with the built-in middlewares as defaults. This enables plugin authors to inject their own middleware without forking the application. (`src/app.py:243-260`, `src/core/message_pipeline.py`)

- [x] **Consolidate `safe_json_parse*` family into a single function with mode parameter** — `src/utils/__init__.py` exposes `safe_json_parse`, `safe_json_parse_line`, `safe_json_parse_with_error`, and `json_dumps`. These share overlapping error-handling logic. Consolidate into a unified `JsonParseMode` enum (STRICT, LENIENT, LINE) with a single `safe_json_parse(data, mode=...)` entry point, reducing API surface and test burden. (`src/utils/__init__.py`, `src/utils/json_utils.py`)

### Performance Optimization

- [x] **Implement streaming LLM responses to reduce perceived latency** — The ReAct loop (`_react_loop`) waits for the full completion before returning. For long responses, the user sees nothing for 10-30+ seconds. Add optional streaming: use `stream=True` in the OpenAI API call, accumulate chunks, and forward partial text to the channel (WhatsApp allows sending a "composing…" indicator followed by the message). Implement behind a `stream_response: bool` config flag (default: False) since streaming adds complexity and not all providers support it. (`src/llm.py:241`, `src/bot.py:914-1016`)

- [x] **Add per-skill timeout configuration instead of global `DEFAULT_SKILL_TIMEOUT`** — `ToolExecutor.execute()` uses a single `DEFAULT_SKILL_TIMEOUT` for all skills. Skills like `web_research` (HTTP fetch + LLM summarization) naturally take longer than `memory_save` (file write). Add a `timeout_seconds` attribute to `BaseSkill` that defaults to `DEFAULT_SKILL_TIMEOUT` but can be overridden per skill, and use it in the `asyncio.wait_for()` call. (`src/skills/base.py`, `src/core/tool_executor.py:211`)

- [x] **Configure asyncio ThreadPoolExecutor size for `asyncio.to_thread` calls** — The codebase uses `asyncio.to_thread()` extensively (database reads/writes, file I/O, psutil calls) but relies on the default `ThreadPoolExecutor` (max_workers = `min(32, os.cpu_count() + 4)`). Under high concurrency (many chats active simultaneously), this pool can saturate, causing `to_thread` calls to queue. Add a custom executor with configurable `max_workers` (e.g., `Config.max_thread_pool_workers = 16`) and set it on the event loop during startup. (`src/app.py`, `src/lifecycle.py`)

- [x] **Add workspace size monitoring and periodic cleanup for old files** — The workspace directory grows without bound: JSONL conversation files, vector memory, backups, logs, and LLM request/response files accumulate indefinitely. Add a periodic background task that: (a) sums workspace disk usage and reports it in `/health`, (b) archives conversations older than N days (configurable) into compressed `.tar.gz`, (c) prunes LLM log files beyond the rotation limit, (d) cleans stale backup files older than N days. (`src/monitoring/workspace_monitor.py`, new file)

### Error Handling & Resilience

- [x] **Fix `QueuedMessage.from_incoming_message()` losing `channel_type`** — Line 120 does `channel=getattr(msg, "channel", None)` but `IncomingMessage` has `channel_type`, not `channel`. The `channel` field on `QueuedMessage` is always `None`. On crash recovery, channel information is lost, which could affect routing if the message is reprocessed through a different code path. Change to `channel=msg.channel_type`. (`src/message_queue.py:120`)

- [x] **Add startup workspace integrity check** — If the workspace directory is corrupted (missing `.data/`, orphaned temp files, stale `.tmp` files from crashed writes, or unreadable JSONL), the bot may crash with confusing errors. Add a `_check_workspace_integrity()` function called during `_build_bot()` that verifies: (a) `.data/` exists and is writable, (b) no stale `.tmp` files remain (older than 1 hour), (c) JSONL files are parseable (spot-check first/last line), (d) vector memory and project store databases are not locked. Report issues as warnings and attempt auto-repair (remove stale temps, rebuild corrupt indices). (`src/lifecycle.py`, `src/db/db.py`)

- [x] **Handle CRLF line endings in `Database._read_file_lines()`** — `_read_file_lines()` splits on `\n` and reads line-by-line. If a JSONL file is ever edited or transferred on Windows (producing `\r\n`), the `\r` remains appended to each JSON line, causing `json.loads()` to fail on the last field. Add `.rstrip('\r')` or split on `\r?\n` when reading JSONL files. (`src/db/db.py`)

- [x] **Guard against `LRULockCache` evicting locks for in-flight operations** — `_chat_locks` (an `LRULockCache`) evicts the least-recently-used lock when `max_size` is reached. If a chat's lock is evicted while a coroutine holds it, the next call for that chat creates a new lock, allowing concurrent processing of the same chat — violating the per-chat serialization invariant. Track in-flight locks with a reference count and skip eviction for locks that are currently held. (`src/utils/__init__.py:LRULockCache`, `src/bot.py:527-528`)

- [x] **Add outbound message dedup to prevent duplicate scheduled task responses** — The bot deduplicates incoming messages via `_db.message_exists()`, but there's no tracking of outbound messages. If `process_scheduled()` is retried (via `_trigger_with_retry`) and succeeds on the second attempt, the first attempt's response may also be delivered, causing duplicate messages. Add a short-lived outbound message ID cache (e.g., `LRUDict` of recently sent message hashes per chat) and skip sending if the response was already delivered within the last 60 seconds. (`src/bot.py:596-750`, `src/scheduler.py:317-380`)

### Security

- [x] **Warn on unknown config keys in `load_config()`** — `_from_dict()` silently ignores unknown keys. A typo in `config.json` (e.g., `"temperture": 0.5` instead of `"temperature": 0.5`) results in the default being used silently. After constructing the Config, compare the input dict keys against known dataclass field names and log a WARNING for each unknown key with the correct field name suggestion (fuzzy match). (`src/config/config.py:328-354`, `src/config/config.py:533-578`)

- [x] **Add JSONL schema versioning for forward-compatible message format changes** — The JSONL message files have no version header. If a future release adds fields (e.g., `embeddings`, `metadata`, `tool_call_id`), old messages lack them and new code may crash. Add a `_version` field to the first line of each JSONL file (e.g., `{"_version": 1, "type": "header"}`) and implement a migration function that can backfill missing fields in older files. Write the header on file creation. (`src/db/db.py`)

- [x] **Validate scheduler task structure before persistence** — `TaskScheduler.add_task()` accepts any dict without validating required fields (`schedule`, `prompt`, `schedule.type`). A malformed task (e.g., missing `prompt`) persists to `tasks.json` and silently fails on execution. Add a `_validate_task()` method that checks: (a) `prompt` is a non-empty string, (b) `schedule` dict exists with a valid `type` (daily/interval/cron), (c) type-specific fields are present (e.g., `hour`/`minute` for daily). Raise `ValueError` on invalid tasks. (`src/scheduler.py:131-157`)

### Observability & Monitoring

- [x] **Add disk space monitoring to the `/health` endpoint** — The health endpoint checks component wiring, database connectivity, and performance metrics but doesn't verify available disk space. A full disk causes silent write failures across all subsystems (JSONL, vector memory, message queue, logs). Add a `check_disk_space()` to health checks that reports `disk_free_mb` and sets status to `DEGRADED` when free space drops below a configurable threshold (e.g., 500 MB). (`src/health/checks.py`, `src/health/server.py`)

- [x] **Track and expose error rate trends over time** — `PerformanceMetrics` tracks counts (messages, errors, skill calls) but not time-series trends. An operator cannot answer "has the error rate increased in the last hour?" without external log aggregation. Add a simple sliding-window error counter (e.g., errors in the last 5/15/60 minutes) and expose `error_rate_5m`, `error_rate_15m`, `error_rate_60m` in the `/health` and `/metrics` endpoints, enabling alerting on error rate spikes. (`src/monitoring/performance.py`)

- [x] **Add configuration hot-reload for non-destructive config changes** — `load_config()` reads config once at startup. To change `allowed_numbers`, `max_tool_iterations`, or `log_verbosity`, the user must restart the entire bot (losing the WhatsApp session). Add a file watcher (debounced, using `watchdog` or a simple polling check like `RoutingEngine._is_stale()`) that detects changes to `config.json` and applies safe changes at runtime. Destructive changes (model swap, provider URL change) still require restart. (`src/config/config.py`, `src/app.py`)

### Test Coverage

- [x] **Add test for `QueuedMessage` preserving `channel_type` from `IncomingMessage`** — Verify that `QueuedMessage.from_incoming_message()` correctly captures the `channel_type` from an `IncomingMessage` (currently broken — captures `None`). Add a test that creates an `IncomingMessage(channel_type="whatsapp", ...)`, converts to `QueuedMessage`, and asserts `queued.channel == "whatsapp"`. (`tests/unit/test_message_queue.py`)

- [x] **Add test for workspace integrity check detecting stale temp files** — Verify the startup integrity check (once implemented) detects and removes stale `.tmp` files left by crashed atomic writes. Create a workspace with a stale temp file (mtime > 1 hour ago), run the check, and verify the temp file is removed. (`tests/unit/test_lifecycle.py`)

- [x] **Add test for `LRULockCache` eviction safety with held locks** — Verify that an `LRULockCache` does not evict a lock while it is held (once the guard is implemented). Create a cache with `max_size=2`, acquire lock for key "A", then insert keys "B" and "C" to trigger eviction. Assert that key "A" is not evicted while held, but can be evicted after release. (`tests/unit/test_async_executor.py` or new file)

- [x] **Add test for per-skill timeout override in `ToolExecutor`** — Verify that when a skill declares `timeout_seconds = 60`, the `ToolExecutor` uses that timeout instead of `DEFAULT_SKILL_TIMEOUT`. Mock a slow skill execution that exceeds the default timeout but finishes within the skill-specific timeout, and verify it succeeds. (`tests/unit/test_tool_executor.py`)

- [x] **Add test for scheduler task validation rejecting malformed tasks** — Verify that `TaskScheduler.add_task()` (once validation is added) rejects tasks missing `prompt`, missing `schedule`, or with invalid `schedule.type`. Verify that valid tasks are accepted and persisted correctly. (`tests/unit/test_scheduler.py`)

- [x] **Add test for config unknown key warnings** — Verify that `load_config()` logs warnings when unknown keys are present in the JSON file. Create a config with `"temperture": 0.5` (typo), load it, and assert a warning is logged suggesting the correct field name. (`tests/unit/test_config_roundtrip.py`)

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

- [ ] **Lazy-load tool definitions instead of building them on every ReAct iteration** — `_react_loop()` calls `self._skills.tool_definitions` at the top, which rebuilds the OpenAI tool schema list from all registered skills. For a bot with 15+ skills, this rebuilds 15 Pydantic models on every iteration (up to `max_tool_iterations` times per message). Cache the tool definitions on the `SkillRegistry` and invalidate only when skills are added/removed (which only happens at startup and config reload). (`src/bot.py:871, 663`, `src/skills/__init__.py`)

### Error Handling & Resilience

- [ ] **Add `ChatMessage` validation in `_process_tool_calls()` buffered_persist** — `_process_tool_calls()` appends dicts to `buffered_persist` with hardcoded keys like `{"role": "tool", "content": content, "name": tool_entry.name}`. If a skill returns a very long result (e.g., `file_read` on a 100KB file), the entire result is persisted to the JSONL conversation history, bloating disk usage and slowing future context builds. Add a `MAX_TOOL_RESULT_PERSIST_LENGTH` constant (e.g., 10_000 chars) and truncate results in the buffered_persist dict with a `[truncated, full length: N]` suffix. The full result is still available in the in-memory `messages` list for the current ReAct iteration. (`src/bot.py:1107-1109`)

- [ ] **Handle `chat_stream()` partial failure leaving buffered text undelivered** — In `chat_stream()`, if the stream breaks mid-way (network failure, provider error), the accumulated `buffered_chunk` may contain text that was never flushed to `on_chunk`. The except block catches and classifies the error, but the partial text is silently lost. Add a `finally` block that flushes any remaining `buffered_chunk` via a best-effort `on_chunk` call (wrapped in its own try/except) so the user sees the partial response rather than nothing. (`src/llm.py:475-477, 532-545`)

- [ ] **Add database write conflict detection for concurrent scheduled and user messages** — `process_scheduled()` and `handle_message()` both write to the same chat's JSONL file, serialized only by the per-chat lock. If both produce responses within the same lock acquisition window (e.g., a scheduled task finishes while the user sends a new message), the conversation history can have interleaved user/assistant turns that confuse the LLM. Add a generation counter to each chat's in-memory state: when `save_messages_batch()` is called, verify that the chat's generation hasn't changed since the context was built. If it has, re-read the latest history and rebuild context before persisting. (`src/bot.py:527-593, 629-748`, `src/db/db.py`)

- [ ] **Guard `_process_tool_calls()` against `TaskGroup` exception propagating tool-call ordering issues** — `_process_tool_calls()` uses `asyncio.TaskGroup` which, by design, cancels all sibling tasks if any raises `BaseException` (not just `Exception`). If one tool call triggers a `KeyboardInterrupt` or `SystemExit`, all other in-flight tool executions are cancelled, and their results are lost. Wrap the `TaskGroup` in a try/except that catches `BaseException` and returns whatever partial results are available (from completed tasks) rather than losing them entirely. (`src/bot.py:1089-1096`)

### Security

- [ ] **Add prompt-injection detection for scheduled task prompts** — `process_scheduled()` accepts a `prompt` string from `tasks.json` and injects it directly into the LLM context without any injection detection. If an attacker gains write access to `tasks.json` (or if a compromised LLM creates a malicious scheduled task via the `task_scheduler` skill), the prompt could contain injection attempts that bypass the normal message pipeline's safeguards. Run `sanitize_user_input()` on scheduled prompts before appending them to the message list, consistent with how incoming messages are sanitized. (`src/bot.py:652`, `src/security/prompt_injection.py`)

- [ ] **Add `workspace/` path traversal guard for skill `workspace_dir` parameter** — Skills receive `workspace_dir` as an argument from `_execute_tool_call()`. While individual skills like `shell.py` have their own path sanitization, the `workspace_dir` itself is constructed from `self._memory.ensure_workspace(chat_id)`. A malicious `chat_id` (e.g., `../../etc`) that bypasses sanitization would propagate to all skill executions. Add a defensive assertion in `_execute_tool_call()` that verifies `workspace_dir.resolve().is_relative_to(WORKSPACE_DIR.resolve())` before executing any skill, as a belt-and-suspenders guard. (`src/bot.py:1134-1138`, `src/core/tool_executor.py:214-217`)

- [ ] **Enforce HMAC timing-safe comparison in health server authentication** — The health server's HMAC verification in `_verify_hmac()` likely uses `hmac.compare_digest()` (constant-time), but if the secret or timestamp parsing has edge cases (empty signature, malformed timestamp), the error path may leak timing information about the expected format. Audit all comparison paths to ensure they are constant-time and add explicit length-normalization of the compared values before the comparison. (`src/health/server.py`)

### Observability & Monitoring

- [ ] **Add per-chat token usage tracking and cost estimation** — `TokenUsage` accumulates global token counts (prompt, completion, total) but has no per-chat breakdown. For a multi-tenant bot serving different users, operators cannot identify which chats consume the most tokens/cost. Add a bounded LRU per-chat token accumulator (`LRUDict` keyed by chat_id, tracking prompt/completion/total per chat) and expose `custombot_chat_prompt_tokens` and `custombot_chat_completion_tokens` as top-N Prometheus metrics. (`src/llm.py:132-158`, `src/monitoring/performance.py`)

- [ ] **Track and expose LLM response latency percentiles in Prometheus metrics** — `PerformanceMetrics` tracks `_llm_latencies` and exposes `custombot_llm_latency_milliseconds` as a simple counter/average. Prometheus histograms are the standard way to express latency distributions (p50, p95, p99). Replace the simple average with a Prometheus histogram bucket approach (even in the custom text format) so operators can set alerts on p95 latency degradation. (`src/monitoring/performance.py`, `src/health/server.py`)

- [ ] **Add structured event emission to the EventBus from core components** — The `EventBus` is implemented and wired but no core components actually emit events. `Bot._process()`, `ToolExecutor.execute()`, and `Application._on_message()` should emit events (`message_received`, `skill_executed`, `response_sent`) so that plugins and extensions can subscribe without modifying core classes. This was the stated purpose of the EventBus but remains unused. (`src/bot.py`, `src/core/tool_executor.py`, `src/core/event_bus.py`)

- [ ] **Add startup banner with QR-code URL for remote headless deployment** — When the bot starts in a Docker container or headless environment, the QR code is printed to stdout but operators SSH'd into the machine may not see it. Add a structured log line (or `/health` field) with the QR code data as a base64-encoded `data:image/png` URL that monitoring tools or dashboards can display. Also log the connection status (waiting-for-QR / connected / disconnected) in the `/ready` endpoint. (`src/channels/neonize_backend.py`, `src/health/server.py`)

### Test Coverage

- [ ] **Add test for `_assemble_context()` parallel-read correctness** — Once the 4 async reads are parallelized via `asyncio.gather()`, add a test that verifies: (a) all 4 data sources are correctly read, (b) results are identical to sequential execution, (c) a failure in one read doesn't cancel the others (use `return_exceptions=True`), (d) the order of returned results matches the expected (memory, agents_md, project_context, topic_summary). (`tests/unit/test_bot.py`)

- [ ] **Add test for conversation-history compression preserving recent messages** — When the JSONL compression feature is implemented, add a test verifying: (a) messages beyond the threshold are summarized, (b) the most recent N messages are preserved verbatim, (c) the summary is injected as a system message with correct metadata, (d) the compressed JSONL file is valid and parseable. (`tests/unit/test_context_builder.py`)

- [ ] **Add test for `EventBus` handler error isolation** — The EventBus uses `_safe_call()` to isolate handler errors, but there is no test verifying: (a) a failing handler doesn't prevent other handlers from executing, (b) a failing handler's exception is logged with the correct event metadata, (c) `emit()` returns normally even when all handlers fail. Add a test with multiple handlers where some raise exceptions. (`tests/unit/test_event_bus.py`, new or existing)

- [ ] **Add test for `ToolExecutor` result truncation in buffered_persist** — Verify that when a skill returns a result exceeding `MAX_TOOL_RESULT_PERSIST_LENGTH`, the buffered_persist dict contains a truncated version with the correct suffix, while the in-memory `messages` list retains the full result for the current ReAct iteration. (`tests/unit/test_tool_executor.py`)

- [ ] **Add test for `chat_stream()` partial delivery on stream failure** — Simulate a stream that delivers 3 chunks then raises a network error. Verify that: (a) the `on_chunk` callback received the chunks that were successfully delivered before the error, (b) the error is classified and raised as an `LLMError`, (c) any buffered text is flushed in the finally block. (`tests/unit/test_llm.py`)

- [ ] **Add test for scheduled task prompt injection detection** — Create a scheduled task with a prompt containing common injection patterns (e.g., "Ignore all previous instructions..."). Verify that `process_scheduled()` sanitizes or flags the prompt before passing it to the LLM, consistent with how incoming messages are handled. (`tests/unit/test_bot.py`)

- [ ] **Add test for `Database.save_messages_batch()` atomicity** — `save_messages_batch()` writes multiple messages to the JSONL file. Add a test that verifies: (a) if the write fails mid-way, no partial messages are persisted, (b) the message index is updated only after the full batch succeeds, (c) concurrent calls to `save_messages_batch()` for the same chat are serialized correctly. (`tests/unit/test_db.py`)
