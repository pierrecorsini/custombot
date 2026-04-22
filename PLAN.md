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
