# CustomBot — Improvement Plan

## Architecture & Refactoring

- [x] Extract `Bot.__init__` into a factory/builder pattern — `Bot.__init__` accepts 12+ constructor parameters; wrap construction in `BotBuilder` to enforce required deps and eliminate `# type: ignore[arg-type]` suppression across `builder.py`
- [x] Replace bare `except Exception: pass` patterns with structured error handling — audit all 15+ instances of silent exception swallowing (especially in `bot.py` event emission, `memory.py` `_track_cache_event`, `vector_memory.py`) and replace with explicit error codes or re-raise where appropriate
- [x] Introduce a `Protocol`-based `LLMProvider` interface — `LLMClient` is tightly coupled to the OpenAI SDK's `AsyncOpenAI`; abstracting behind a protocol would allow testing without mocking `openai` internals and make it easier to support non-OpenAI-compatible providers natively
- [x] Consolidate the three separate lock strategies (`threading.Lock` in `rate_limiter.py`/`vector_memory.py`, `asyncio.Lock` in `db.py`/`message_queue.py`, `asyncio.Lock` lazy-init in `event_bus.py`/`shutdown.py`) into a documented locking policy module with helper mixins to prevent future misuse
- [x] Split `bot.py` (1256+ lines) into focused modules — extract `ReActLoop`, `PreflightChecker`, and `CrashRecovery` into `src/bot/` subpackage to improve navigability and reduce merge conflicts

## Performance Optimization

- [x] Add connection pooling for the file-based Database — the `FileHandlePool` caps at 256 handles but `MessageStore` still opens/closes for reads; implement a read-handle pool or mmap-based reads for hot-path message retrieval
- [x] Batch embedding API calls in `VectorMemory` — multiple `store()` calls in quick succession each trigger a separate OpenAI embeddings request; implement request coalescing with a short debounce window to batch embeddings
- [x] Pre-warm routing rule cache on startup — `RoutingEngine.match_with_rule()` triggers lazy rule loading on the first message; call `load_rules()` eagerly during startup (it does, but `_is_stale()` can re-trigger scans per-message with debouncing at only 2s)
- [x] Evaluate replacing `orjson` hot-path serialization with msgpack for tool-call result payloads — tool results can be large (e.g., file reads) and msgpack's binary format would reduce both serialization time and memory for in-flight pipeline data

## Error Handling & Resilience

- [x] Add structured retry with backoff for SQLite writes in `Database` — JSONL message writes currently have a circuit breaker but no retry; transient disk-full or lock-contention errors should retry with exponential backoff before tripping the breaker
- [x] Implement health-check-driven LLM failover — when the LLM circuit breaker opens, proactively poll the provider endpoint and auto-close the breaker on recovery instead of waiting for the full cooldown period
- [x] Add graceful degradation when `VectorMemory` embedding model is unreachable at runtime — startup probe catches initial failures, but mid-session API outages cause unhandled `LLMError` in `store()`; catch and log gracefully, queuing failed embeddings for retry
- [x] Harden `MessageQueue` against JSONL corruption — if the queue file is partially written (crash mid-write), the entire queue becomes unreadable; add line-level recovery that skips malformed lines instead of failing the whole file

## Test Coverage & Quality

- [x] Increase coverage from 75% to 80% — target uncovered branches in `bot.py` `_react_loop` error paths, `db.py` `_save_chats` edge cases, and `routing.py` regex compilation failures
- [x] Add property-based tests for `RoutingEngine.match_with_rule()` — use Hypothesis to generate random `MatchingContext`/`RoutingRule` combinations and verify rule precedence, wildcard matching, and cache correctness invariants
- [x] Add integration test for the full ReAct loop with tool calls — current tests mock the LLM; add a test that exercises the real loop with a stub LLM returning tool_calls followed by a final response, verifying message persistence and tool-log assembly end-to-end
- [x] Add chaos/stress test for concurrent message processing — verify that per-chat locks, the dedup service, and the message queue behave correctly under concurrent load (10+ chats, interleaved messages, forced failures)
- [x] Add test matrix for Python 3.13 compatibility — CI marks 3.13 as `experimental: true` but doesn't track specific failures; add a dedicated test job that reports 3.13-specific issues

## Security

- [x] Add rate limiting to the health-check HTTP endpoint — the `/health` endpoint in `health.py` is unauthenticated and unrestricted; add IP-based rate limiting to prevent abuse when `--health-port` is exposed
- [x] Sanitize `chat_id` values before using them in filesystem paths — `_sanitize_chat_id_for_path()` exists but `Memory._resolve_chat_path()` calls it via `sanitize_path_component()`; audit all code paths to ensure no `chat_id` bypasses sanitization (e.g., from scheduled tasks or recovery)
- [x] Add request signing or HMAC verification for scheduled task prompts — `process_scheduled()` accepts arbitrary prompts from the scheduler config; an attacker who can write to the scheduler config could inject malicious prompts that bypass the normal message pipeline's security checks

## DevOps & CI/CD

- [x] Add `ruff` to `pre-commit` hooks — the `.pre-commit-config.yaml` exists but doesn't include `ruff`; adding it would catch lint/format issues before they reach CI
- [x] Add Docker image vulnerability scanning to CI — the `Dockerfile` is well-structured but the `security` job only runs `pip-audit` on requirements; add `trivy` or `grype` scanning of the built image
- [x] Pin `requirements-dev.txt` dependencies — `pytest>=8.0.0` and `mypy>=1.13.0` are unpinned upper bounds; pin exact versions for reproducible CI builds and add `dependabot` coverage for dev dependencies
- [x] Add `mypy --strict` coverage expansion — the CI already has an opt-in strict job for `src/core` and `src/bot.py`; expand to `src/channels/`, `src/security/`, and `src/monitoring/` incrementally

## Documentation & Observability

- [x] Add OpenTelemetry-compatible tracing spans to the message pipeline — current observability is limited to structured logs and performance metrics; adding OTel spans would enable distributed tracing across LLM calls, skill execution, and message delivery
- [x] Expose Prometheus-compatible metrics endpoint alongside health check — `PerformanceMetrics` already tracks latencies, queue depth, and error rates; expose these via a `/metrics` endpoint for external monitoring
- [x] Add a `--diagnose` CLI command for common troubleshooting — auto-check config validity, LLM connectivity, workspace integrity, and disk space, outputting a structured report to help users self-serve before filing issues

---

## Round 2 — Additional Improvements

### Architecture & Refactoring

- [x] Replace `Application`'s 9 `None`-initialized component properties with a typed state machine or builder — `Application.__init__` sets `_shutdown_mgr`, `_components`, `_scheduler`, `_channel`, `_pipeline`, `_executor`, `_workspace_monitor`, `_config_watcher`, `_health_server` all to `None` with `@property` accessors that raise `RuntimeError` if called too early; a `@dataclass(frozen=True)` `AppState` + explicit phase transitions would catch misuse at construction time rather than at runtime in production
- [x] Type the `_react_iteration` span parameter correctly — `react_loop.py` line 221 uses `span: Any` instead of the OTel `Span` type, defeating type-checking on the hot path; import `Span` under `TYPE_CHECKING` from `opentelemetry.trace` and use it so mypy catches attribute errors on span operations
- [x] Split `vector_memory.py` (892 lines) into focused sub-modules — extract batch coalescing (`_batched_embed`, `_flush_pending`, `_embed_batch`) into `vector_memory/batch.py` and embedding health + retry queue (`_check_embedding_api_health`, `_mark_embedding_api_*`, `_queue_for_retry`, `_retry_pending_saves`) into `vector_memory/health.py`; the main module would re-export the `VectorMemory` class for backward compatibility
- [x] Split `db.py` (915 lines) further — extract JSONL schema migration logic (`_ensure_jsonl_schema`, `_apply_jsonl_migrations`) into `db/migration.py` and generation-counter logic (`_bump_generation`, `get_generation`, `check_generation`) into `db/generations.py`; the `Database` facade would delegate as it already does for `MessageStore` and `CompressionService`
- [ ] Eliminate `# type: ignore[arg-type]` suppressions in `BuilderContext.to_bot_components()` — the 8 suppressions indicate the type system can't verify steps have populated required `None`-able fields; introduce a `TypedBuilderContext` with `__post_init__` validation or use `@overload` on `to_bot_components()` so that the builder orchestrator only calls it when all fields are known populated

### Performance Optimization

- [ ] Replace `RoutingEngine` polling-based stale detection with OS-native file watching — `_is_stale()` runs `os.scandir()` + `stat()` on every `.md` file every 5 seconds; integrate `watchdog` (already a lightweight pure-Python library) for instant inotify/ReadDirectoryChanges notification with zero polling overhead, falling back to current debounced polling on platforms without native support
- [ ] Use a faster non-cryptographic hash for `VectorMemory` embedding cache keys — `_embed()` and `_embed_batch()` use `hashlib.sha256()` to deduplicate embedding text, which is cryptographically overkill; replace with `xxhash.xxh128()` (10-50× faster on short strings) or Python's `hash()` with a stable seed for a pure-Python fallback
- [ ] Move `Database.connect()` template seeding off the event loop — the `shutil.copy2` loop that seeds instruction templates (lines 506-513 of `db.py`) runs synchronously during `connect()`; wrap in `asyncio.to_thread()` to prevent blocking startup when the template directory is large or on slow filesystems
- [ ] Add a pre-computed leaderboard in `TokenUsage.get_top_chats()` — `get_top_chats()` sorts all tracked chats by token usage on every call (O(n log n)); maintain a `SortedList` or cached top-N that updates incrementally on each `add_for_chat()` call for O(log n) insertion instead of full re-sort

### Error Handling & Resilience

- [ ] Extract duplicated LLM error classification from `LLMClient.chat()` and `chat_stream()` — lines 320-339 and 521-540 are nearly identical (circuit breaker failure recording, error classification, metrics tracking); refactor into a shared async context manager or decorator `@with_circuit_breaker_and_classification` to eliminate the ~20 duplicated lines and ensure both paths stay in sync
- [ ] Add timeout protection to `Application._shutdown_cleanup()` — individual cleanup steps (config watcher stop, workspace monitor stop) have no timeout and could block shutdown indefinitely; wrap each step in `asyncio.wait_for(coro, timeout=10.0)` so a hung cleanup component doesn't prevent process exit
- [ ] Stop masking non-OSError exceptions in `MtimeCache.read()` — line 170 wraps ALL exceptions from `asyncio.to_thread()` in `OSError`, which hides programming errors (e.g., `TypeError`, `AttributeError`) as I/O errors; catch only `OSError` and let other exceptions propagate unmodified so bugs surface during development
- [ ] Add `BaseException` guard in `Scheduler._loop()` gather handling — the existing `isinstance(result, Exception)` check after `asyncio.gather(return_exceptions=True)` doesn't handle `BaseException` subclasses like `SystemExit` or `GeneratorExit` that could escape from misbehaving task callbacks; add explicit handling to log and re-raise these

### Test Coverage & Quality

- [ ] Add unit tests for `ContextAssembler` — `src/core/context_assembler.py` orchestrates 4 async context reads (memory, agents_md, project context, topic cache) and token-budget trimming but has no dedicated tests; create `tests/unit/test_context_assembler.py` covering cache hits/misses, token budget overflow, missing instruction files, and concurrent assembly calls
- [ ] Add security-focused tests for the shell skill (`src/skills/builtin/shell.py`) — the shell skill executes arbitrary commands and is the highest-risk attack surface; test the denylist/allowlist enforcement, command injection patterns (pipe chains, backticks, environment variable expansion), timeout handling, and output truncation
- [ ] Fix `MockChatCompletion.usage` class-level mutable dict in `conftest.py` — line 109 defines `usage` as a class variable (shared across instances), causing potential test pollution if tests mutate it; move to `__init__` alongside `self.choices` so each instance gets its own copy
- [ ] Add benchmark regression tests and integrate into CI — `tests/unit/bench_serialization.py` exists but isn't gated in CI; add `pytest-benchmark` fixtures for critical paths (routing match, embedding cache lookup, JSONL message write, context assembly) and a CI job that fails on >10% regression from a stored baseline
- [ ] Add tests for channel input validation (`src/channels/validation.py`) — the module sanitizes incoming message fields (sender_id, chat_id, message_id lengths and formats) but has no dedicated test file; add boundary tests for max-length enforcement, special character handling, and the interaction with `MAX_MESSAGE_LENGTH`

### Security

- [ ] Replace hardcoded `api_key="sk-no-key"` sentinel in `LLMClient.__init__` — line 82 falls back to `"sk-no-key"` when no API key is configured, which could accidentally authenticate against providers that accept any non-empty key; use `"not-configured"` or raise a clear `ConfigurationError` at construction time for providers requiring authentication
- [ ] Add `format: "uri"` validation enforcement in `config_schema.py` — the JSON Schema declares `format: "uri"` for `base_url` but the hand-rolled validator doesn't enforce it (no `format` validation); either add RFC 3986 URI validation for `base_url` or replace the custom validator with the `jsonschema` library for proper spec compliance
- [ ] Add embedding model reachability check to `--diagnose` command — the diagnostic command checks LLM connectivity, workspace integrity, and disk space but doesn't probe the embedding model separately; a misconfigured `embedding_model` or unreachable embeddings endpoint would only surface when users invoke vector memory skills, not at startup diagnostic time

### DevOps & CI/CD

- [ ] Sync `requirements.txt` with `pyproject.toml` — `msgpack~=1.1.0` is declared in `pyproject.toml` dependencies but missing from `requirements.txt`; since the Dockerfile installs from `requirements.txt`, the msgpack package is absent in production images; either sync both files or switch the Dockerfile to install from `pyproject.toml` via `pip install .`
- [ ] Add pip cache and vulnerability database caching to CI — the security scanning job (`pip-audit`) re-downloads the OSV vulnerability database on every run; cache `~/.cache/pip-audit` and `~/.cache/pip` across runs to cut CI time by ~30-60s per workflow
- [ ] Add CI step to verify `requirements.txt` and `pyproject.toml` are in sync — the two files maintain the same dependency list independently with no validation; add a CI job that parses both and asserts their dependency sets match (name + version constraint), preventing silent drift

### Documentation & Observability

- [ ] Surface VectorMemory degradation status in the health endpoint — the `/health` endpoint reports LLM circuit breaker state and DB write breaker state but doesn't include VectorMemory health (embedding API reachability, retry queue depth); add a `vector_memory` component to the health response with `DEGRADED` status when the embedding API is unreachable or the retry queue exceeds 50% capacity
- [ ] Add `--diagnose` check for orphaned workspace directories — workspace directories whose `.chat_id` origin files are missing or whose corresponding JSONL is empty indicate crashed or interrupted sessions; the diagnose command should scan `workspace/whatsapp_data/` for orphaned dirs and report them for cleanup
