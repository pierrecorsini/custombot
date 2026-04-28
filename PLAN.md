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
- [x] Eliminate `# type: ignore[arg-type]` suppressions in `BuilderContext.to_bot_components()` — the 8 suppressions indicate the type system can't verify steps have populated required `None`-able fields; introduce a `TypedBuilderContext` with `__post_init__` validation or use `@overload` on `to_bot_components()` so that the builder orchestrator only calls it when all fields are known populated

### Performance Optimization

- [x] Replace `RoutingEngine` polling-based stale detection with OS-native file watching — `_is_stale()` runs `os.scandir()` + `stat()` on every `.md` file every 5 seconds; integrate `watchdog` (already a lightweight pure-Python library) for instant inotify/ReadDirectoryChanges notification with zero polling overhead, falling back to current debounced polling on platforms without native support
- [x] Use a faster non-cryptographic hash for `VectorMemory` embedding cache keys — `_embed()` and `_embed_batch()` use `hashlib.sha256()` to deduplicate embedding text, which is cryptographically overkill; replace with `xxhash.xxh128()` (10-50× faster on short strings) or Python's `hash()` with a stable seed for a pure-Python fallback
- [x] Move `Database.connect()` template seeding off the event loop — the `shutil.copy2` loop that seeds instruction templates (lines 506-513 of `db.py`) runs synchronously during `connect()`; wrap in `asyncio.to_thread()` to prevent blocking startup when the template directory is large or on slow filesystems
- [x] Add a pre-computed leaderboard in `TokenUsage.get_top_chats()` — `get_top_chats()` sorts all tracked chats by token usage on every call (O(n log n)); maintain a `SortedList` or cached top-N that updates incrementally on each `add_for_chat()` call for O(log n) insertion instead of full re-sort

### Error Handling & Resilience

- [x] Extract duplicated LLM error classification from `LLMClient.chat()` and `chat_stream()` — lines 320-339 and 521-540 are nearly identical (circuit breaker failure recording, error classification, metrics tracking); refactor into a shared async context manager or decorator `@with_circuit_breaker_and_classification` to eliminate the ~20 duplicated lines and ensure both paths stay in sync
- [x] Add timeout protection to `Application._shutdown_cleanup()` — individual cleanup steps (config watcher stop, workspace monitor stop) have no timeout and could block shutdown indefinitely; wrap each step in `asyncio.wait_for(coro, timeout=10.0)` so a hung cleanup component doesn't prevent process exit
- [x] Stop masking non-OSError exceptions in `MtimeCache.read()` — line 170 wraps ALL exceptions from `asyncio.to_thread()` in `OSError`, which hides programming errors (e.g., `TypeError`, `AttributeError`) as I/O errors; catch only `OSError` and let other exceptions propagate unmodified so bugs surface during development
- [x] Add `BaseException` guard in `Scheduler._loop()` gather handling — the existing `isinstance(result, Exception)` check after `asyncio.gather(return_exceptions=True)` doesn't handle `BaseException` subclasses like `SystemExit` or `GeneratorExit` that could escape from misbehaving task callbacks; add explicit handling to log and re-raise these

### Test Coverage & Quality

- [x] Add unit tests for `ContextAssembler` — `src/core/context_assembler.py` orchestrates 4 async context reads (memory, agents_md, project context, topic cache) and token-budget trimming but has no dedicated tests; create `tests/unit/test_context_assembler.py` covering cache hits/misses, token budget overflow, missing instruction files, and concurrent assembly calls
- [x] Add security-focused tests for the shell skill (`src/skills/builtin/shell.py`) — the shell skill executes arbitrary commands and is the highest-risk attack surface; test the denylist/allowlist enforcement, command injection patterns (pipe chains, backticks, environment variable expansion), timeout handling, and output truncation
- [x] Fix `MockChatCompletion.usage` class-level mutable dict in `conftest.py` — line 109 defines `usage` as a class variable (shared across instances), causing potential test pollution if tests mutate it; move to `__init__` alongside `self.choices` so each instance gets its own copy
- [x] Add benchmark regression tests and integrate into CI — `tests/unit/bench_serialization.py` exists but isn't gated in CI; add `pytest-benchmark` fixtures for critical paths (routing match, embedding cache lookup, JSONL message write, context assembly) and a CI job that fails on >10% regression from a stored baseline
- [x] Add tests for channel input validation (`src/channels/validation.py`) — the module sanitizes incoming message fields (sender_id, chat_id, message_id lengths and formats) but has no dedicated test file; add boundary tests for max-length enforcement, special character handling, and the interaction with `MAX_MESSAGE_LENGTH`

### Security

- [x] Replace hardcoded `api_key="sk-no-key"` sentinel in `LLMClient.__init__` — line 82 falls back to `"sk-no-key"` when no API key is configured, which could accidentally authenticate against providers that accept any non-empty key; use `"not-configured"` or raise a clear `ConfigurationError` at construction time for providers requiring authentication
- [x] Add `format: "uri"` validation enforcement in `config_schema.py` — the JSON Schema declares `format: "uri"` for `base_url` but the hand-rolled validator doesn't enforce it (no `format` validation); either add RFC 3986 URI validation for `base_url` or replace the custom validator with the `jsonschema` library for proper spec compliance
- [x] Add embedding model reachability check to `--diagnose` command — the diagnostic command checks LLM connectivity, workspace integrity, and disk space but doesn't probe the embedding model separately; a misconfigured `embedding_model` or unreachable embeddings endpoint would only surface when users invoke vector memory skills, not at startup diagnostic time

### DevOps & CI/CD

- [x] Sync `requirements.txt` with `pyproject.toml` — `msgpack~=1.1.0` is declared in `pyproject.toml` dependencies but missing from `requirements.txt`; since the Dockerfile installs from `requirements.txt`, the msgpack package is absent in production images; either sync both files or switch the Dockerfile to install from `pyproject.toml` via `pip install .`
- [x] Add pip cache and vulnerability database caching to CI — the security scanning job (`pip-audit`) re-downloads the OSV vulnerability database on every run; cache `~/.cache/pip-audit` and `~/.cache/pip` across runs to cut CI time by ~30-60s per workflow
- [x] Add CI step to verify `requirements.txt` and `pyproject.toml` are in sync — the two files maintain the same dependency list independently with no validation; add a CI job that parses both and asserts their dependency sets match (name + version constraint), preventing silent drift

### Documentation & Observability

- [x] Surface VectorMemory degradation status in the health endpoint — the `/health` endpoint reports LLM circuit breaker state and DB write breaker state but doesn't include VectorMemory health (embedding API reachability, retry queue depth); add a `vector_memory` component to the health response with `DEGRADED` status when the embedding API is unreachable or the retry queue exceeds 50% capacity
- [x] Add `--diagnose` check for orphaned workspace directories — workspace directories whose `.chat_id` origin files are missing or whose corresponding JSONL is empty indicate crashed or interrupted sessions; the diagnose command should scan `workspace/whatsapp_data/` for orphaned dirs and report them for cleanup

---

## Round 3 — Additional Improvements

### Architecture & Refactoring

- [x] Extract stream-response reconstruction from `LLMClient.chat_stream()` into a dedicated `StreamAccumulator` class — the method is ~180 lines reconstructing a `ChatCompletion` from SSE deltas (accumulating content, tool_call fragments, usage data); extracting a `StreamAccumulator` would make the streaming logic independently testable, reusable for future providers, and reduce the cognitive load of `chat_stream()` to ~50 lines of orchestration
- [x] Deduplicate `StartupOrchestrator` and `BuilderOrchestrator` into a shared `StepOrchestrator[T]` base — `src/core/startup.py` and `src/builder.py` contain near-identical orchestrator patterns (topological sort → sequential execution → timing → logging → progress bar) parameterized only by context type; a generic `StepOrchestrator[T]` base class would eliminate ~60 duplicated lines and ensure both startup and builder phases evolve in lockstep
- [x] Extract connectivity-check pattern from `diagnose.py` into a shared `_probe_api_endpoint()` helper — `check_llm_connectivity()` and `check_embedding_model()` share ~40 lines of identical boilerplate (create client, time the call, catch TimeoutError/401/general exception, close client); a shared async helper would reduce duplication and make it trivial to add future API probes (e.g., web search endpoint)
- [x] Make `Bot.handle_message()` per-chat lock cache eviction policy configurable — `LRULockCache` evicts the least-recently-used lock when at capacity (`MAX_LRU_CACHE_SIZE=1000`), but under sustained load with >1000 concurrent chats, a lock eviction while messages are still in-flight for that chat would allow concurrent processing of the same chat on the next message; add an active-count check or raise the default to a documented configurable value
- [x] Move `src/constants.py` (627 lines) into a `src/constants/` package split by domain — the single file mixes routing, database, circuit breaker, scheduler, memory, health, and skill constants in one flat namespace; splitting into `constants/cache.py`, `constants/llm.py`, `constants/db.py`, `constants/scheduler.py`, etc. with a re-exporting `__init__.py` would improve navigability without breaking existing `from src.constants import X` imports

### Performance Optimization

- [x] Implement adaptive sleep in `Scheduler._loop()` instead of fixed 30s tick — the scheduler polls every `TICK_SECONDS=30` regardless of when the next task is due; compute the minimum time-to-next-due-task and sleep `min(TICK_SECONDS, time_to_next)`, waking earlier when a task is imminent and sleeping longer (up to a cap) when no tasks exist, reducing unnecessary CPU wakeups from ~2880/day to a fraction
- [x] Add optional batched fsync to `MessageQueue._append_to_queue()` — every `enqueue()` issues an `os.fsync()` call, costing ~1-5ms per message on HDD/NFS; accumulate a small batch window (e.g., 50ms or N messages) before fsyncing, trading a worst-case loss of ~50ms of queue entries for 10-50× throughput improvement under burst traffic
- [x] Bound `MtimeCache._missing` dict to prevent unbounded growth — the `_missing` dict tracks absent files with monotonic timestamps but has no size limit; a bot with thousands of unique chat IDs (each probing for a nonexistent MEMORY.md) would accumulate entries indefinitely; add a `maxlen` cap (e.g., `MAX_LRU_CACHE_SIZE`) with FIFO eviction to match the bounded `_cache` LRU dict
- [x] Lazy-compile `RoutingRule` regex patterns on first match instead of at construction — `RoutingRule.__post_init__` compiles all 4 patterns (sender, recipient, channel, content) eagerly, but many rules have `*` wildcards for most fields; skip compilation for `*` patterns (already handled by `_is_wildcard`) and compile only the non-wildcard fields on first `match_with_rule()` invocation, reducing rule-load time proportional to the number of regex-qualified rules
- [x] Cache `sanitize_path_component()` results in `Memory._resolve_chat_path()` — the path cache stores the resolved `Path` object, but `sanitize_path_component()` is called before the cache lookup; move sanitization inside the cache-miss path so repeated lookups for the same `chat_id` skip both the regex and the path construction

### Error Handling & Resilience

- [x] Add retry logic to `LLMClient.chat_stream()` — unlike `chat()` which wraps `_raw_chat()` with `retry_with_backoff(max_retries=3)`, the streaming path has no retry; a transient network error during stream establishment fails immediately; add a lightweight retry wrapper around the stream-creation `await self._client.chat.completions.create(**kwargs)` call (not the full stream consumption) so transient failures at the handshake layer are retried
- [ ] Add exponential backoff to `Scheduler._loop()` on repeated failures — when `_loop()` catches an exception it logs and continues at the fixed 30s cadence; if failures persist (e.g., all scheduled tasks fail because the LLM is down), the scheduler keeps executing and failing every 30s, wasting API calls and logging noise; track consecutive-failure count and multiply the sleep interval (capped at e.g., 5 minutes), resetting on the first success
- [ ] Harden `Memory.backup_memory_file()` and `repair_memory_file()` against event-loop blocking — both methods perform synchronous `shutil.copy2()` and `path.write_text()` which could block the event loop for large files or slow filesystems; wrap the I/O operations in `asyncio.to_thread()` and expose async counterparts (`abackup_memory_file`, `arepair_memory_file`) for callers in async context
- [ ] Add validation and graceful handling for missing `workspace/instructions/` directory at startup — `RoutingEngine.__init__` accepts the directory and `load_rules()` warns but doesn't raise if it's missing; however, `Bot.handle_message()` proceeds to context assembly without routing rules, causing an empty instruction set sent to the LLM; add an explicit startup check that warns when zero rules are loaded and a diagnostic suggestion to create at least a default `chat.agent.md`

### Test Coverage & Quality

- [ ] Add unit tests for `src/config/config_watcher.py` — the config hot-reload watcher polls `config.json` mtime and applies changes but has no test coverage; add tests for: mtime-based change detection, debouncing of rapid successive saves, config validation before apply, and graceful handling of malformed JSON during a write-in-progress
- [ ] Add unit tests for `src/monitoring/workspace_monitor.py` — the periodic workspace cleanup (archive old JSONL, prune backups, remove stale temp files) runs as a background daemon but has no tests; create `tests/unit/test_workspace_monitor.py` covering: age-based file selection, archive creation, temp file cleanup, and size-threshold reporting
- [ ] Add unit tests for `src/utils/frontmatter.py` — the YAML frontmatter parser underpins all routing rule extraction but has no dedicated tests; add tests for: valid frontmatter with routing rules, multi-rule arrays, missing/malformed YAML, BOM-prefixed files, empty files, and files with no frontmatter block
- [ ] Add integration test for full startup → message → shutdown lifecycle — current tests mock individual components; add a test that creates a real `Application` with a mock channel, sends a message through the full pipeline (`Application._on_message`), verifies the response is delivered, and shuts down cleanly, catching lifecycle wiring bugs that unit tests miss
- [ ] Add tests for `src/skills/prompt_skill.py` — the prompt skill allows the LLM to call another LLM with a different system prompt, which is a powerful and risky capability; test prompt validation, recursive call prevention, timeout handling, and that the skill respects `shell_config` restrictions
- [ ] Increase coverage threshold from 75% to 80% — the CI `--cov-fail-under=75` gate has been at 75% for multiple rounds; bump to 80% now that test files for context assembler, channel validation, shell security, and other modules exist, targeting uncovered branches in `bot/_bot.py`, `diagnose.py`, `channels/neonize_backend.py`, and `config/config.py`

### Security

- [ ] Redact API keys from `diagnose.py` stack traces and error messages — `check_llm_connectivity()` and `check_embedding_model()` create `AsyncOpenAI(api_key=api_key, ...)` inline; if the client raises an exception that includes the request URL or headers in its message, the API key could appear in the diagnostic output; wrap client creation and all exception formatting in a helper that sanitizes sensitive fields before including them in `CheckResult.message`
- [ ] Add input length validation to the `options` TUI command — `run_options_tui()` accepts user-typed values for API key, base URL, model name, etc. but doesn't enforce length limits; an extremely long input could cause UI rendering issues or excessive memory use in the questionary prompts; add max-length constraints matching the config schema
- [ ] Add audit logging for config hot-reload changes — `ConfigWatcher` detects config.json changes and applies them via the channel's config applier, but doesn't log what fields changed or who triggered the change (filesystem mtime); add structured audit entries recording the old vs. new values for security-sensitive fields (`api_key`, `allowed_numbers`, `allow_all`) so operators can detect unauthorized config modifications post-incident
- [ ] Add `Content-Security-Policy` and `X-Content-Type-Options` headers to all health server responses — the health server already sets some security headers but should ensure `Content-Security-Policy: default-src 'none'` and `X-Content-Type-Options: nosniff` are present on every response to prevent content-type sniffing and inline script execution if the endpoint is exposed to untrusted networks

### DevOps & CI/CD

- [ ] Add Dependabot configuration for GitHub Actions version pinning — `.github/dependabot.yml` exists but only covers pip dependencies; add an `ecosystem: github-actions` entry to auto-update pinned action versions (e.g., `actions/checkout@v4`, `actions/setup-python@v5`, `aquasecurity/trivy-action@0.30.0`) and receive PRs when new patch/minor versions are released
- [ ] Add a CI job to verify the Docker image builds and starts successfully — the `docker-scan` job builds for Trivy scanning but doesn't verify the container actually starts; add a smoke-test job that builds the image, runs it with a test config (expecting graceful failure since no WhatsApp session exists), and verifies the entrypoint and healthcheck are configured correctly
- [ ] Run `mypy` on the `tests/` directory in CI — type errors in test fixtures and assertions go undetected; add `mypy tests/ --ignore-missing-imports` as a non-blocking CI step to catch type mismatches between test mocks and actual interfaces before they cause subtle test pollution
- [ ] Add a release automation workflow — currently releases are manual; add a GitHub Actions workflow triggered by tag push (`v*`) that runs the full CI matrix, builds the Docker image with version labels, publishes to GHCR, and generates a release notes draft from conventional commits

### Documentation & Observability

- [ ] Add routing engine metrics to the `/metrics` endpoint — `RoutingEngine` tracks match cache hit/miss rates, rule reload frequency, and active rule count internally but doesn't expose them via `PerformanceMetrics`; add `routing_cache_hits_total`, `routing_cache_misses_total`, `routing_rule_reloads_total`, and `routing_active_rules` gauges so operators can detect stale rules or routing misconfiguration
- [ ] Add per-skill execution latency tracking — `ToolExecutor` records skill execution duration in structured logs but doesn't expose aggregated metrics; add a `skill_execution_seconds` histogram (by skill name) to `PerformanceMetrics` so operators can identify slow skills and track performance regressions across releases
- [ ] Add a `/ready` readiness probe separate from `/health` liveness — the `/health` endpoint checks all subsystems (DB, LLM, vector memory) which is appropriate for liveness but too strict for readiness during startup; add a `/ready` endpoint that returns 200 only after all startup steps complete and the channel is connected, enabling Kubernetes-style readiness gates without false-positive crash loops during slow LLM warmup
- [ ] Add workspace disk usage breakdown to `--diagnose` — `check_disk_space()` reports total free space but not what is consuming it; add a breakdown by directory (`whatsapp_data/`, `.data/`, `logs/`, `skills/`) with sizes so operators can quickly identify whether disk pressure is from conversation history, vector memory, or log accumulation
