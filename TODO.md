# TODO — CustomBot Improvement Plan

Comprehensive improvement plan based on full codebase review, industry best practices, and web research.
Sorted by category and priority (P0 = critical, P1 = high, P2 = medium, P3 = low).

---

## 1. Security & Hardening

### P0 — Prompt Injection Defense
- [ ] Add prompt injection detection layer before LLM calls
- [ ] Sanitize user input before embedding into system prompt (context_builder.py)
- [ ] Add `max_system_prompt_length` guard to prevent context overflow attacks
- [ ] Strip or escape special instruction patterns from user messages (e.g., "ignore previous instructions")
- [ ] Add content filtering for outgoing responses (PII, secrets, API keys)
- **Why**: LLM chatbots are highly vulnerable to prompt injection. User messages are directly concatenated into system prompts with no sanitization. This is the #1 security risk for chatbot applications.



### P1 — Shell Skill Sandboxing
- [ ] Restrict shell skill to a whitelist of commands or add configurable command blacklist
- [ ] Add timeout enforcement for shell commands (currently relies on skill timeout only)
- [ ] Validate working directory hasn't escaped workspace via symlink
- [ ] Log all shell commands executed with chat_id correlation
- **Why**: Shell skill gives the LLM arbitrary command execution. Even with workspace confinement, this is a significant attack surface.

### P2 — File Skill Path Validation
- [ ] Enhance path traversal protection in `read_file`/`write_file` to handle encoded paths (e.g., `%2e%2e`)
- [ ] Add file size limits for read operations to prevent memory exhaustion
- [ ] Validate file extensions (block `.env`, `.key`, `.pem`, etc.)
- **Why**: Path traversal via URL-encoded or double-encoded sequences can bypass current `..` blocking.

### P2 — Rate Limiter Enhancements
- [ ] Add global rate limit across all chats (prevent total API abuse)
- [ ] Add configurable rate limits per routing rule (some personas may need different limits)
- [ ] Implement token bucket algorithm as alternative to sliding window for burst allowance
- **Why**: Current per-chat limits don't protect against distributed abuse from multiple chats.

---

## 2. Architecture & Code Quality

### P1 — Dependency Injection Container
- [ ] Replace manual wiring in `builder.py` with a proper DI container or factory pattern
- [ ] The `_build_bot()` function has 8 tightly-coupled initialization steps — extract into independent factory functions
- [ ] `Bot.__init__` takes 10 parameters (code smell) — consider a `BotConfig` dataclass
- **Why**: `builder.py:22` is a god function. Extracting independent factories improves testability and makes the dependency graph explicit.

### P1 — Eliminate Global Mutable State
- [ ] Replace `_session_token_usage` global in `llm.py:58` with an instance variable on `LLMClient`
- [ ] Replace `_global_metrics` module-level singleton in `monitoring/performance.py:436` with explicit dependency injection
- [ ] Replace `_global_rate_limiter` in `rate_limiter.py:446` with DI
- [ ] `set_scheduler_instance()` in `task_scheduler.py` uses a module-level global — inject scheduler via constructor
- **Why**: Global mutable state makes testing difficult, prevents multiple bot instances, and creates hidden coupling. This is the most impactful refactoring for testability.

### P2 — Abstract Database Layer
- [ ] Extract a `MessageStore` protocol/interface from `Database` class
- [ ] Allow swapping JSONL storage for SQLite-backed storage without changing `Bot`
- [ ] The current JSONL approach doesn't scale — individual message files grow unbounded
- [ ] Add message compaction/archival for old conversations (e.g., compress messages older than 30 days)
- **Why**: The file-based JSONL database will have performance issues with high-volume chats. The code already has `MAX_MESSAGE_HISTORY = 500` but reads ALL lines just to get the last N.

### P2 — Configuration Consolidation
- [ ] Move from `dataclass` config to `pydantic` for runtime validation + type coercion
- [ ] Consolidate `config.py` (635 lines) and `config_schema.py` (564 lines) — the manual JSON Schema validator duplicates what pydantic provides
- [ ] Add config migration system (version-based schema evolution)
- [ ] Support config hot-reload (watch config.json for changes)
- **Why**: Two separate validation systems (JSON Schema + type guards) is redundant. Pydantic handles both plus serialization.

### P3 — Code Organization
- [ ] `src/channels/whatsapp.py` (705 lines) — extract `NeonizeBackend` into its own file
- [ ] `src/config/config.py` (635 lines) — split into `config_types.py`, `config_loader.py`, `config_validation.py`
- [ ] `src/db/db.py` (947 lines) — split into `db_core.py`, `db_messages.py`, `db_chats.py`
- [ ] Move `src/routing.py`, `src/scheduler.py`, `src/message_queue.py` into `src/core/`
- **Why**: Files over 500 lines are difficult to navigate and review.

---

## 3. LLM & Agent Improvements

### P1 — Context Window Management
- [ ] Add token counting before LLM calls to prevent context overflow
- [ ] Implement intelligent context truncation (summarize old messages instead of dropping them)
- [ ] Add `tiktoken` or equivalent for accurate token counting per model
- [ ] Current approach: fetch N messages, hope it fits — this can silently fail or waste tokens
- **Why**: Without token counting, the bot can hit API limits unexpectedly, especially with long conversations or large MEMORY.md files.

### P1 — Streaming Responses
- [ ] Implement server-side streaming (SSE) for LLM responses via the OpenAI streaming API
- [ ] Stream partial responses to WhatsApp as they arrive (send chunks as they complete sentences)
- [ ] Add configurable streaming mode per routing rule
- **Why**: Current implementation waits for the full response before sending. For long responses, this creates a poor user experience with long waits.

### P2 — ReAct Loop Improvements
- [ ] Add early termination when tool calls produce errors (currently retries even after repeated failures)
- [ ] Implement tool result caching (avoid re-executing identical tool calls within the same conversation)
- [ ] Add "reasoning" step visibility — show the LLM's reasoning before tool execution when `skillExecVerbose` is enabled
- [ ] Add maximum total token budget across all ReAct iterations (not just max iterations)
- [ ] Consider parallel tool execution when multiple independent tools are called
- **Why**: The ReAct loop is the core of the bot. These improvements directly affect reliability and user experience.

### P2 — Embedding & Vector Memory
- [ ] Add support for local embedding models (e.g., sentence-transformers) for offline operation
- [ ] Implement embedding batch API for `memory_save` (currently one-by-one)
- [ ] Add similarity threshold configuration (current search returns results regardless of distance)
- [ ] Add memory deduplication (detect and merge similar entries)
- [ ] Add memory expiration/decay (old entries gradually lose relevance)
- **Why**: Vector memory is only useful with good embeddings. Local models would reduce costs and enable offline use.

### P3 — Multi-Model Support
- [ ] Allow different models per routing rule (e.g., cheap model for casual chat, powerful model for complex tasks)
- [ ] Support model fallback chain (if primary model fails, try backup)
- [ ] Add response quality scoring to choose optimal model dynamically
- **Why**: Cost optimization — not every message needs GPT-4. A lightweight model can handle routine responses.

---

## 4. Testing & Quality Assurance

### P0 — Test Coverage Expansion
- [ ] Current test suite is minimal — add unit tests for ALL core modules:
  - `bot.py` (ReAct loop, message processing, error handling)
  - `llm.py` (retry logic, token tracking, tool_call_to_dict)
  - `routing.py` (rule matching, priority ordering, regex patterns)
  - `scheduler.py` (schedule evaluation, task CRUD, persistence)
  - `message_queue.py` (enqueue, complete, recovery)
  - `memory.py` (read/write, cache, corruption detection)
  - `vector_memory.py` (save, search, list, delete)
- [ ] Add integration tests for the full message pipeline (incoming → routing → LLM → response)
- [ ] Target: 80%+ line coverage
- **Why**: The test suite currently covers only edge utilities. Core business logic is completely untested. This is the highest-impact quality improvement.

### P1 — Mock Infrastructure
- [ ] Create mock LLM client with configurable responses for testing
- [ ] Create mock WhatsApp channel for end-to-end pipeline testing
- [ ] Add `conftest.py` fixtures for common test setups (bot, database, skills)
- [ ] Add property-based testing for routing rules (hypothesis library)
- **Why**: Testing the ReAct loop and message pipeline requires controllable LLM responses.

### P2 — Continuous Quality
- [ ] Add `ruff` configuration to `pyproject.toml` (currently using `.ruff_cache` but no config file)
- [ ] Add `mypy` type checking to CI pipeline
- [ ] Add pre-commit hooks (ruff, mypy, pytest)
- [ ] Add `pyproject.toml` for project metadata (currently only has `package.json` with just name/version)
- **Why**: Modern Python projects use `pyproject.toml` for all tool configuration. Pre-commit hooks catch issues before they enter the codebase.

---

## 5. Reliability & Resilience

### P1 — Circuit Breaker for LLM Calls
- [ ] Implement circuit breaker pattern for LLM API failures
- [ ] File `src/circuit_breaker.py` exists but is not used — integrate it into `LLMClient.chat()`
- [ ] Add half-open state for gradual recovery after failures
- [ ] Track failure rates per provider/model
- **Why**: The `circuit_breaker.py` file exists in the codebase but is never imported or used. LLM API outages can cascade into repeated failures.

### P1 — Database Resilience
- [ ] Enable WAL mode for all SQLite databases (vector_memory.db, projects.db)
- [ ] Add connection pooling for SQLite databases
- [ ] Implement periodic database integrity checks (not just on startup)
- [ ] Add automatic message file rotation/compaction (prevent unbounded JSONL growth)
- **Why**: Without WAL mode, SQLite writes block reads. The current `_lock` based approach in `VectorMemory` is correct but suboptimal compared to WAL mode.

### P2 — WhatsApp Reconnection
- [ ] Add exponential backoff for reconnection attempts (current watchdog uses fixed 5s intervals)
- [ ] Implement session health monitoring (detect stale connections faster)
- [ ] Add reconnection state machine with clear states (disconnected → connecting → connected)
- [ ] Persist and replay messages that arrive during disconnection
- **Why**: Network interruptions are common. The current watchdog approach works but doesn't handle prolonged outages gracefully.

### P2 — Error Recovery
- [ ] Add dead-letter queue for permanently failed messages
- [ ] Implement structured error reporting (aggregate similar errors, suppress duplicates)
- [ ] Add automatic recovery from corrupted MEMORY.md (currently manual via `repair_memory_file()`)
- [ ] Add startup self-healing: validate all workspace directories and repair inconsistencies
- **Why**: The bot currently logs errors but doesn't recover automatically. Users must manually intervene.

---

## 6. Observability & Monitoring

### P1 — Structured Metrics Export
- [ ] Add Prometheus/OpenTelemetry metrics exporter
- [ ] Expose metrics on the health check server (`/metrics` endpoint)
- [ ] Track: message throughput, LLM latency percentiles, skill execution times, error rates, memory usage
- [ ] Add configurable alerting thresholds
- **Why**: The `PerformanceMetrics` class collects data but only logs it. External monitoring tools need a standard format.

### P2 — Distributed Tracing
- [ ] Correlation IDs are already implemented — extend them to span across tool executions
- [ ] Add parent-child span relationships (message → LLM call → tool execution)
- [ ] Export traces to Jaeger/Zipkin compatible format
- **Why**: Correlation IDs exist but aren't hierarchical. Tracing would help debug complex multi-tool interactions.

### P2 — LLM Cost Tracking
- [ ] Add per-chat token usage tracking (not just global session totals)
- [ ] Implement cost estimation per model (input/output token pricing table)
- [ ] Add daily/weekly cost summaries in the health endpoint
- [ ] Add configurable cost alerts (warn when daily spend exceeds threshold)
- **Why**: LLM API costs can grow rapidly. Users need visibility into spending patterns.



---

## 7. Features & UX

### P1 — Message History with Context
- [ ] Implement conversation summarization when history exceeds token limits
- [ ] Store summaries alongside messages for fast retrieval
- [ ] The `topic_cache.py` + `TopicCache` exist but the topic detection prompt is basic — improve it
- **Why**: Long conversations hit token limits. Summarization preserves context while staying within bounds.

### P2 — Multi-Channel Support
- [ ] Add Telegram channel implementation (the `BaseChannel` ABC is ready for it)
- [ ] Add Discord channel implementation
- [ ] Add web/HTTP channel for direct browser access
- [ ] Unify channel prompts per platform (WhatsApp formatting vs Telegram Markdown)
- **Why**: The architecture supports multi-channel via `BaseChannel` but only WhatsApp is implemented.

### P2 — Enhanced Scheduler
- [ ] Add one-time (non-recurring) scheduled tasks
- [ ] Add timezone-aware scheduling (currently uses local UTC offset with hourly cache)
- [ ] Add task chaining (output of one task becomes input of the next)
- [ ] Add task failure notifications (currently silent on failure)
- [ ] Support natural language scheduling ("every weekday at 9am")
- **Why**: The scheduler is functional but limited. Timezone handling is fragile (breaks on DST transitions).

### P3 — Plugin/Extension System
- [ ] Allow skills to register custom HTTP endpoints on the health server
- [ ] Add skill lifecycle hooks (on_register, on_enable, on_disable, on_error)
- [ ] Support skill configuration (per-skill settings in config.json)
- [ ] Add skill marketplace/registry (install skills from a remote repository)
- **Why**: The skill system is the main extension point. Making it more powerful increases the bot's capabilities.

---

## 8. Performance & Scalability

### P1 — Async I/O Optimization
- [ ] Replace `asyncio.to_thread(path.read_text)` calls in `memory.py` with `aiofiles` for true async file I/O
- [ ] Batch database writes (currently one write per message — batch every N messages)
- [ ] Use `asyncio.TaskGroup` instead of `asyncio.gather` for structured concurrency
- [ ] Add connection reuse for HTTP calls (httpx AsyncClient pooling)
- **Why**: `asyncio.to_thread` still blocks a thread pool slot. True async I/O would be more efficient.

### P2 — Caching Improvements
- [ ] Add TTL-based cache for instruction files (current mtime-based cache works but has no TTL)
- [ ] Implement embedding cache persistence (current in-memory cache is lost on restart)
- [ ] Add LLM response cache for identical prompts within a time window
- [ ] Consider Redis for distributed caching if multi-instance deployment is needed
- **Why**: Embedding API calls are expensive. Caching them persistently would reduce costs significantly.

### P2 — Memory Optimization
- [ ] Add memory usage monitoring per component (chat_locks, message_id_index, rate limiters)
- [ ] Implement periodic cleanup of stale LRU cache entries
- [ ] Add configurable memory limits with graceful degradation
- [ ] Profile memory usage under load (1000+ concurrent chats)
- **Why**: The bot uses multiple LRU caches and in-memory indexes. Under high load, memory could grow unexpectedly.

---

## 9. Developer Experience

### P2 — Project Configuration
- [ ] Create `pyproject.toml` with proper metadata, dependencies, and tool configs
- [ ] Move from `requirements.txt` to dependency groups (core, dev, vector, testing)
- [ ] Add proper `__version__.py` version management (bump version command)
- [ ] Add Docker support (Dockerfile + docker-compose.yml)
- **Why**: Modern Python projects use `pyproject.toml`. The current `package.json` is vestigial (just name + version).

### P3 — Documentation
- [ ] Add inline architecture decision records (ADRs) for key design choices
- [ ] Generate API documentation from docstrings (Sphinx or MkDocs)
- [ ] Add contributing guide with development setup instructions
- [ ] Add troubleshooting guide for common issues
- **Why**: The README is excellent but there's no developer documentation beyond it.

### P3 — Development Tools
- [ ] Add REPL/debug mode for interactive bot testing
- [ ] Add message replay tool (replay saved messages for debugging)
- [ ] Add configuration migration helper (upgrade config.json between versions)
- [ ] Add workspace inspection CLI command (`python main.py inspect`)
- **Why**: Debugging issues in production requires good tooling. Currently relies on log analysis only.

---
