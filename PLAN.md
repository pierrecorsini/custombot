# CustomBot — Improvement Plan

## Architecture & Refactoring

- [x] Extract `Bot.__init__` into a factory/builder pattern — `Bot.__init__` accepts 12+ constructor parameters; wrap construction in `BotBuilder` to enforce required deps and eliminate `# type: ignore[arg-type]` suppression across `builder.py`
- [x] Replace bare `except Exception: pass` patterns with structured error handling — audit all 15+ instances of silent exception swallowing (especially in `bot.py` event emission, `memory.py` `_track_cache_event`, `vector_memory.py`) and replace with explicit error codes or re-raise where appropriate
- [x] Introduce a `Protocol`-based `LLMProvider` interface — `LLMClient` is tightly coupled to the OpenAI SDK's `AsyncOpenAI`; abstracting behind a protocol would allow testing without mocking `openai` internals and make it easier to support non-OpenAI-compatible providers natively
- [ ] Consolidate the three separate lock strategies (`threading.Lock` in `rate_limiter.py`/`vector_memory.py`, `asyncio.Lock` in `db.py`/`message_queue.py`, `asyncio.Lock` lazy-init in `event_bus.py`/`shutdown.py`) into a documented locking policy module with helper mixins to prevent future misuse
- [ ] Split `bot.py` (1256+ lines) into focused modules — extract `ReActLoop`, `PreflightChecker`, and `CrashRecovery` into `src/bot/` subpackage to improve navigability and reduce merge conflicts

## Performance Optimization

- [ ] Add connection pooling for the file-based Database — the `FileHandlePool` caps at 256 handles but `MessageStore` still opens/closes for reads; implement a read-handle pool or mmap-based reads for hot-path message retrieval
- [ ] Batch embedding API calls in `VectorMemory` — multiple `store()` calls in quick succession each trigger a separate OpenAI embeddings request; implement request coalescing with a short debounce window to batch embeddings
- [ ] Pre-warm routing rule cache on startup — `RoutingEngine.match_with_rule()` triggers lazy rule loading on the first message; call `load_rules()` eagerly during startup (it does, but `_is_stale()` can re-trigger scans per-message with debouncing at only 2s)
- [ ] Evaluate replacing `orjson` hot-path serialization with msgpack for tool-call result payloads — tool results can be large (e.g., file reads) and msgpack's binary format would reduce both serialization time and memory for in-flight pipeline data

## Error Handling & Resilience

- [ ] Add structured retry with backoff for SQLite writes in `Database` — JSONL message writes currently have a circuit breaker but no retry; transient disk-full or lock-contention errors should retry with exponential backoff before tripping the breaker
- [ ] Implement health-check-driven LLM failover — when the LLM circuit breaker opens, proactively poll the provider endpoint and auto-close the breaker on recovery instead of waiting for the full cooldown period
- [ ] Add graceful degradation when `VectorMemory` embedding model is unreachable at runtime — startup probe catches initial failures, but mid-session API outages cause unhandled `LLMError` in `store()`; catch and log gracefully, queuing failed embeddings for retry
- [ ] Harden `MessageQueue` against JSONL corruption — if the queue file is partially written (crash mid-write), the entire queue becomes unreadable; add line-level recovery that skips malformed lines instead of failing the whole file

## Test Coverage & Quality

- [ ] Increase coverage from 75% to 80% — target uncovered branches in `bot.py` `_react_loop` error paths, `db.py` `_save_chats` edge cases, and `routing.py` regex compilation failures
- [ ] Add property-based tests for `RoutingEngine.match_with_rule()` — use Hypothesis to generate random `MatchingContext`/`RoutingRule` combinations and verify rule precedence, wildcard matching, and cache correctness invariants
- [ ] Add integration test for the full ReAct loop with tool calls — current tests mock the LLM; add a test that exercises the real loop with a stub LLM returning tool_calls followed by a final response, verifying message persistence and tool-log assembly end-to-end
- [ ] Add chaos/stress test for concurrent message processing — verify that per-chat locks, the dedup service, and the message queue behave correctly under concurrent load (10+ chats, interleaved messages, forced failures)
- [ ] Add test matrix for Python 3.13 compatibility — CI marks 3.13 as `experimental: true` but doesn't track specific failures; add a dedicated test job that reports 3.13-specific issues

## Security

- [ ] Add rate limiting to the health-check HTTP endpoint — the `/health` endpoint in `health.py` is unauthenticated and unrestricted; add IP-based rate limiting to prevent abuse when `--health-port` is exposed
- [ ] Sanitize `chat_id` values before using them in filesystem paths — `_sanitize_chat_id_for_path()` exists but `Memory._resolve_chat_path()` calls it via `sanitize_path_component()`; audit all code paths to ensure no `chat_id` bypasses sanitization (e.g., from scheduled tasks or recovery)
- [ ] Add request signing or HMAC verification for scheduled task prompts — `process_scheduled()` accepts arbitrary prompts from the scheduler config; an attacker who can write to the scheduler config could inject malicious prompts that bypass the normal message pipeline's security checks

## DevOps & CI/CD

- [ ] Add `ruff` to `pre-commit` hooks — the `.pre-commit-config.yaml` exists but doesn't include `ruff`; adding it would catch lint/format issues before they reach CI
- [ ] Add Docker image vulnerability scanning to CI — the `Dockerfile` is well-structured but the `security` job only runs `pip-audit` on requirements; add `trivy` or `grype` scanning of the built image
- [ ] Pin `requirements-dev.txt` dependencies — `pytest>=8.0.0` and `mypy>=1.13.0` are unpinned upper bounds; pin exact versions for reproducible CI builds and add `dependabot` coverage for dev dependencies
- [ ] Add `mypy --strict` coverage expansion — the CI already has an opt-in strict job for `src/core` and `src/bot.py`; expand to `src/channels/`, `src/security/`, and `src/monitoring/` incrementally

## Documentation & Observability

- [ ] Add OpenTelemetry-compatible tracing spans to the message pipeline — current observability is limited to structured logs and performance metrics; adding OTel spans would enable distributed tracing across LLM calls, skill execution, and message delivery
- [ ] Expose Prometheus-compatible metrics endpoint alongside health check — `PerformanceMetrics` already tracks latencies, queue depth, and error rates; expose these via a `/metrics` endpoint for external monitoring
- [ ] Add a `--diagnose` CLI command for common troubleshooting — auto-check config validity, LLM connectivity, workspace integrity, and disk space, outputting a structured report to help users self-serve before filing issues
