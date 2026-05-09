# CustomBot Improvement Plan — Round 19

**Created**: 2026-05-08
**Source**: Web research (Google searches on WhatsApp AI chatbot best practices, ReAct loop patterns, security, observability, multimodal, memory architecture, testing, deployment, UX, plugin systems) + existing known gaps + security gaps + improvement roadmap
**Goal**: 100 actionable improvement points organized by category

---

## Category 1: Architecture & Refactoring (15 points)

- [x] 1.1 — **Abstract database backend behind `StorageProvider` protocol** — Replace direct `db.py` imports with a protocol-based interface (`get_chat`, `save_messages`, `search_messages`) so future backends (Redis, PostgreSQL, MongoDB) can be swapped without touching business logic
- [x] 1.2 — **Extract `Bot._process()` into a `TurnOrchestrator` class** — Separate turn preparation, ReAct loop iteration, and response delivery into distinct methods on a new class, reducing `Bot` to a thin coordinator
- [x] 1.3 — **Implement event-sourced message store** — Store events (message_received, tool_called, response_sent) as immutable facts enabling audit trails, replay debugging, and time-travel inspection
- [x] 1.4 — **Add dependency injection container** — Replace `BuilderOrchestrator` manual wiring with a lightweight DI container (e.g. `python-inject` or custom) supporting scoped lifetimes and auto-wiring
- [x] 1.5 — **Modular channel abstraction** — Formalize `BaseChannel` into a full abstract class with lifecycle hooks (`on_connect`, `on_disconnect`, `on_message`, `on_error`) and a channel registry for dynamic loading
- [x] 1.6 — **Extract skill registry into standalone module** — Decouple skill discovery/registration from `Bot` into a `SkillRegistry` that supports hot-loading, versioning, and dependency declarations between skills
- [x] 1.7 — **Implement command bus pattern for skill execution** — Route tool calls through a command bus with middleware pipeline (logging, auth, rate-limiting, timeout) instead of direct dispatch in `ToolExecutor`
- [x] 1.8 — **Replace mutable context bags with immutable snapshots** — Make `ReactIterationContext` frozen/immutable after creation to prevent accidental mutation mid-turn
- [x] 1.9 — **Add structured plugin loading with manifest validation** — Each skill/plugin ships a `manifest.json` declaring dependencies, permissions, version range — loaded at startup with dependency resolution
- [x] 1.10 — **Extract configuration layers** — Split monolithic `config.json` into layered configs: `defaults.json` (bundled), `user.json` (runtime), `env` (overrides) with documented merge priority
- [x] 1.11 — **Add application state machine persistence** — Persist `AppPhase` transitions to disk so the bot can resume its last known phase after crash/restart instead of always starting from `INITIALIZING`
- [x] 1.12 — **Implement middleware ordering DSL** — Allow middleware ordering via config (priority numbers) instead of hardcoded insertion order, enabling users to customize the message pipeline
- [x] 1.13 — **Separate read/write models for conversation store** — Write-optimized JSONL for persistence, read-optimized in-memory index for fast context retrieval (CQRS-lite pattern)
- [x] 1.14 — **Add bounded lifecycle management for long-running skills** — Skills that spawn background tasks (e.g. web research, file processing) should register with a `TaskManager` that enforces wall-clock limits and cancellation propagation
- [x] 1.15 — **Refactor error hierarchy into domain-specific exceptions** — Replace generic `Exception` catches with `LLMError`, `StorageError`, `ChannelError`, `SkillError` base classes carrying structured context

## Category 2: LLM & Agent Intelligence (15 points)

- [x] 2.1 — **Conversation summarization for long contexts** — When chat history exceeds token budget, automatically summarize older turns into a compressed summary block, preserving key facts and decisions
- [x] 2.2 — **Multi-model routing** — Allow routing different message types to different models (e.g. code questions → code-specialized model, creative tasks → larger model, simple queries → fast/cheap model)
- [x] 2.3 — **Token usage prediction** — Before sending to LLM, estimate token count of the full prompt; if approaching limit, trigger summarization or truncation proactively instead of waiting for API error
- [x] 2.4 — **Streaming response chunking for WhatsApp** — Break long streaming responses into WhatsApp-sized chunks (4096 chars) and send progressively with typing indicators between chunks
- [x] 2.5 — **Implement plan-and-execute agent pattern** — For complex tasks, add a planning step that generates a task breakdown, then executes steps sequentially with verification at each stage
- [x] 2.6 — **Human-in-the-loop approval for destructive actions** — Before executing skills marked as "dangerous" (shell commands, file deletion, bulk operations), send a confirmation prompt to the user
- [x] 2.7 — **Self-reflection and response quality scoring** — After generating a response, run a lightweight self-evaluation pass scoring coherence, relevance, and completeness; regenerate if below threshold
- [x] 2.8 — **Dynamic tool selection** — Instead of sending all 28+ tool schemas every turn, analyze the user message and send only relevant tool definitions, reducing token waste and improving tool selection accuracy
- [x] 2.9 — **Conversation topic detection and segmentation** — Detect topic shifts in conversation to create natural boundaries for memory indexing and context window management
- [x] 2.10 — **Structured output mode** — For skills that need JSON/XML responses, enable structured output mode (JSON schema) to eliminate parsing failures and reduce retry cycles
- [x] 2.11 — **Retry with model fallback** — On LLM failure, automatically retry with a configured fallback model (e.g. primary: GPT-4o, fallback: Claude Haiku) before surfacing error to user
- [x] 2.12 — **Context compression via embedding deduplication** — Before sending context to LLM, deduplicate semantically similar past messages using embeddings to reduce noise and token count
- [x] 2.13 — **Add configurable ReAct loop strategies** — Support different loop strategies: standard ReAct, chain-of-thought, reflexion (self-correcting), and tree-of-thought (multi-path exploration)
- [x] 2.14 — **LLM response caching** — Cache identical prompts (hash of system + context + user message) with TTL to avoid redundant LLM calls for repeated questions within a time window
- [x] 2.15 — **Tool call result validation** — Validate tool call results against expected schema before feeding back to LLM; reject malformed results and retry with error context

## Category 3: Memory & Context Management (10 points)

- [x] 3.1 — **Episodic memory layer** — Store significant conversation episodes (decisions made, facts learned, user preferences) in a structured episodic memory separate from raw chat history
- [x] 3.2 — **Memory decay with importance scoring** — Assign importance scores to memories; low-importance memories decay over time (reduced retrieval weight), high-importance memories persist indefinitely
- [x] 3.3 — **Cross-chat memory sharing (opt-in)** — Allow users to share specific memories between chats (e.g. "remember this across all conversations") with explicit privacy controls
- [x] 3.4 — **Memory consolidation background job** — Periodically run a background LLM task to review, deduplicate, and consolidate memories, creating higher-level summaries from raw conversation data
- [x] 3.5 — **Working memory with priority queue** — Maintain a bounded working memory (recent + relevant facts) that fits within token budget, using priority scoring combining recency, relevance, and importance
- [x] 3.6 — **Semantic memory graph** — Build a knowledge graph from extracted entities and relationships, enabling structured queries like "what did I say about project X last week?"
- [x] 3.7 — **Memory search with hybrid retrieval** — Combine keyword search (BM25) with vector similarity search for better memory recall; rank results using reciprocal rank fusion
- [x] 3.8 — **User preference learning** — Automatically extract and store user preferences (communication style, response length, language, format preferences) from conversation patterns
- [x] 3.9 — **Memory versioning and rollback** — Track memory mutations with version history, allowing rollback if corrupted memories are detected or user wants to undo a learned preference
- [x] 3.10 — **Context window budget allocator** — Divide the context window into budgeted slots (system prompt, tools, memory, recent history, current turn) with configurable percentages and intelligent overflow handling

## Category 4: Performance & Scalability (10 points)

- [x] 4.1 — **Batch inbound dedup lookups** — Group multiple inbound message dedup checks into a single hash lookup batch instead of per-message queries
- [x] 4.2 — **Redis caching backend option** — Add Redis as an optional caching layer for hot data (active chat contexts, routing rules, dedup state) with automatic serialization and TTL management
- [x] 4.3 — **Connection pool for embedding HTTP calls** — Reuse HTTP connections for vector embedding API calls with connection pooling and keep-alive to reduce latency per embedding request
- [x] 4.4 — **Lazy loading of skill modules** — Defer importing skill modules until first use instead of importing all 28+ skills at startup, reducing cold-start time and memory footprint
- [x] 4.5 — **Async SQLite with write-ahead logging optimization** — Tune SQLite WAL settings (checkpoint interval, journal mode, cache size) for optimal concurrent read/write performance
- [x] 4.6 — **Memory-mapped file access for large JSONL stores** — Use `mmap` for reading large conversation history files instead of loading entire files into memory
- [x] 4.7 — **Circuit breaker for vector memory operations** — Add a circuit breaker around embedding and vector search operations so degraded embedding services don't block the main message processing pipeline
- [x] 4.8 — **Graceful degradation under load** — When message queue exceeds threshold, temporarily disable expensive features (vector memory search, complex tool calls) and use simplified fast-path responses
- [x] 4.9 — **Pre-computed context templates** — Cache assembled context templates per routing rule, only updating the variable portions (user message, recent history) on each turn
- [x] 4.10 — **Parallel tool execution for independent calls** — When the LLM requests multiple tool calls in a single turn that have no dependencies between them, execute them concurrently instead of sequentially

## Category 5: Security & Privacy (10 points)

- [x] 5.1 — **Add ACL rejection audit trail** — Emit `message_dropped` event with `reason="acl_rejected"` when messages fail ACL check, providing security observability parity with other rejection paths
- [x] 5.2 — **Rate-limit error replies to prevent amplification** — Add per-chat sliding window rate limiting on `_send_error_reply()` to prevent DoS via error message amplification attacks
- [x] 5.3 — **Block high-confidence injection detections** — When injection detection scores above confidence threshold, reject or sanitize the prompt instead of just logging a warning
- [x] 5.4 — **HMAC mandatory for scheduled task execution** — Make HMAC signature verification mandatory (not optional) for all scheduled task execution to prevent unauthorized task injection
- [x] 5.5 — **Skill sandboxing with resource limits** — Implement configurable sandboxing for skills that execute external code (shell, file I/O) with CPU time limits, memory limits, and filesystem access boundaries
- [x] 5.6 — **Audit logging for configuration changes** — Log all config hot-reload changes with before/after diff, timestamp, and trigger source for security compliance
- [x] 5.7 — **Sensitive data redaction in logs** — Automatically redact phone numbers, API keys, tokens, and other PII from all log output using configurable regex patterns
- [x] 5.8 — **End-to-end encryption for stored conversations** — Add optional at-rest encryption for conversation JSONL files using a user-provided key, protecting data if storage is compromised
- [x] 5.9 — **HTTP-level rate limiting for LLM client** — Add rate limiting at the HTTP client level to prevent runaway LLM API usage from bugs or malicious prompts
- [x] 5.10 — **Prompt template injection prevention** — Validate and sanitize all user-supplied strings before inserting them into system prompts, using a template engine with auto-escaping

## Category 6: Observability & Monitoring (10 points)

- [x] 6.1 — **Prometheus histogram for routing match latency** — Add histogram metric tracking time spent in routing engine per message, broken down by match/no-match and rule count
- [x] 6.2 — **Per-skill error rate gauge** — Track error rate per skill in `PerformanceMetrics`, exposing via Prometheus endpoint for skill-specific alerting
- [x] 6.3 — **Token cost estimation and tracking** — Add estimated cost tracking per model to `TokenUsage`, exposed via health endpoint, enabling budget monitoring
- [x] 6.4 — **Full OpenTelemetry metrics instruments** — Replace ad-hoc metrics with proper OTel instruments (counters, histograms, gauges) for standardized collection and visualization
- [x] 6.5 — **Distributed trace correlation across message lifecycle** — Ensure trace context propagates from message receipt → routing → context assembly → LLM call → tool execution → delivery
- [x] 6.6 — **Per-chat message processing latency percentiles** — Track and expose p50/p95/p99 latency per chat for SLA monitoring and anomaly detection
- [x] 6.7 — **Conversation quality metrics** — Track metrics like average turns per conversation, tool call success rate, user follow-up rate (indicating unsatisfying first responses)
- [x] 6.8 — **Anomaly detection on LLM latency** — Automatically detect unusual spikes in LLM response time and emit alerts, indicating provider degradation before circuit breaker triggers
- [x] 6.9 — **Dashboard-ready health endpoint** — Extend health endpoint to return structured JSON suitable for Grafana/Glass dashboard panels showing system status at a glance
- [x] 6.10 — **Periodic outbound dedup stats logging** — Log deduplication statistics (hit rate, buffer size, eviction count) periodically for operational awareness

## Category 7: Reliability & Resilience (10 points)

- [x] 7.1 — **Atomic writes for message queue persistence** — Use write-to-temp + `os.replace()` pattern for message queue writes to prevent corruption on crash mid-write
- [x] 7.2 — **Configurable wall-clock timeout for ReAct loop** — Add a configurable maximum wall-clock time for the entire ReAct loop, terminating gracefully if exceeded
- [x] 7.3 — **Generation-conflict recovery with re-read + merge** — When concurrent write conflicts detected in `_deliver_response()`, re-read the latest state, merge, and retry instead of logging and proceeding
- [x] 7.4 — **Fail fast on user-message persistence failure** — In `_prepare_turn()`, if saving the user's message fails, immediately surface the error instead of proceeding with LLM call
- [x] 7.5 — **Structured retry budget recovery** — Track and expose retry budget recovery progress, with metric for time-until-full-recovery after budget exhaustion
- [x] 7.6 — **Suppress rate-limit responses in group chats** — In group chat contexts, silently drop rate-limited messages instead of sending "rate limited" replies that spam the group
- [x] 7.7 — **Debounced save write loss prevention** — Add a synchronous flush on debounce-protected writes before process exit, and a journal file for crash recovery of in-flight debounced writes
- [x] 7.8 — **WhatsApp reconnect backoff with jitter** — Implement exponential backoff with jitter for WhatsApp reconnection attempts to avoid thundering herd after provider outages
- [x] 7.9 — **Health probe for active circuit breaker recovery** — Background task that actively probes LLM provider health (lightweight completion call) to detect recovery faster than passive half-open testing
- [x] 7.10 — **Graceful feature degradation protocol** — Define clear degradation levels (full → reduced → minimal → emergency) with automatic feature disabling based on system health metrics

## Category 8: Testing & Quality Assurance (10 points)

- [x] 8.1 — **Increase test coverage floor from 75% to 85%** — Raise the minimum coverage gate, targeting untested modules: scheduler, vector memory, security subsystem
- [x] 8.2 — **Property-based tests for routing engine** — Use Hypothesis to generate arbitrary message/rule combinations, verifying routing engine invariants (no crash, deterministic, priority ordering)
- [x] 8.3 — **Integration test for config hot-reload end-to-end** — Test full cycle: write config change → watcher detects → diff logged → components updated → behavior verified
- [x] 8.4 — **End-to-end crash recovery pipeline test** — Simulate crash at various points (mid-LLM call, mid-tool execution, mid-write), verify recovery produces consistent state
- [x] 8.5 — **Contract test suite for BaseChannel subclasses** — Define channel behavior contract (send, receive, reconnect, shutdown) and auto-verify all channel implementations satisfy it
- [x] 8.6 — **Mutation testing in CI** — Add mutation testing (mutmut or similar) as a non-blocking CI step to measure test effectiveness at catching real bugs
- [x] 8.7 — **Load testing framework** — Create a load testing harness that simulates 100+ concurrent chats with varying message rates to validate performance claims
- [x] 8.8 — **Chaos engineering for concurrent operations** — Add tests that randomly inject failures (network errors, timeouts, exceptions) into concurrent operations to validate error handling
- [x] 8.9 — **LLM response mock library** — Build a comprehensive mock library of typical LLM responses (tool calls, errors, streaming chunks) for deterministic skill testing without API calls
- [x] 8.10 — **Regression test for ReAct loop edge cases** — Add tests for: infinite loop detection, tool call with missing parameters, circular tool dependencies, context overflow mid-turn

## Category 9: User Experience & Multimodal (10 points)

- [x] 9.1 — **Multi-language support** — Add automatic language detection and response generation in the user's language, with configurable default language per chat
- [x] 9.2 — **Rich message formatting** — Support WhatsApp rich message types: lists, buttons, carousels for structured responses (e.g. search results, scheduling options)
- [x] 9.3 — **Image understanding and processing** — Accept image inputs via WhatsApp, process with vision-capable LLMs for description, analysis, OCR, and image-based questions
- [x] 9.4 — **Voice message transcription** — Accept voice notes via WhatsApp, transcribe with Whisper API, and respond to the transcribed content
- [x] 9.5 — **Interactive command menu** — Add a `/menu` command that presents available skills and actions as an interactive WhatsApp list for discoverability
- [x] 9.6 — **Response length adaptation** — Automatically adapt response length based on conversation context: short confirmations for simple queries, detailed explanations for complex questions
- [x] 9.7 — **Message reactions as feedback** — Accept WhatsApp message reactions (👍/👎) as implicit feedback for response quality, using thumbs-down to trigger regeneration with alternative approach
- [x] 9.8 — **Progressive response delivery** — For long-running operations (web research, file processing), send intermediate progress updates ("Searching...", "Found 5 results, analyzing...")
- [x] 9.9 — **Conversation branching** — Allow users to branch a conversation from a previous point, exploring alternative paths without losing the original conversation context
- [x] 9.10 — **Accessibility improvements** — Add text-to-speech for all text responses (existing TTS skill), alt-text generation for images, and simplified response mode for accessibility needs

## Category 10: Developer Experience & DevOps (10 points)

- [x] 10.1 — **Remove backward-compat re-exports** — Clean up `from src.llm import LLMClient` style re-exports from module reorganization, updating all internal imports
- [x] 10.2 — **Add `--dry-run` flag to config validation** — Allow running config validation without starting the bot, useful for CI and deployment pre-checks
- [x] 10.3 — **Fix Ruff PLC0415 violations** — Incrementally address 618 import-outside-top-level violations across the codebase for cleaner module structure
- [x] 10.4 — **Expand strict mypy coverage** — Enable strict mypy for `src/core/`, `src/llm/`, `src/security/`, and `src/scheduler/` modules
- [x] 10.5 — **Add `make test-quick` target** — Run only fast unit tests (exclude integration/e2e) for rapid feedback during development
- [x] 10.6 — **Document `BotDeps` injection contract** — Add comprehensive docstring to `BotDeps` dataclass explaining the dependency injection pattern and how to extend it
- [x] 10.7 — **CI pipeline with GitHub Actions** — Set up proper CI pipeline: lint → type check → unit tests → integration tests → build Docker image → security scan
- [x] 10.8 — **Automated release versioning** — Add semantic versioning with automatic changelog generation based on conventional commits
- [x] 10.9 — **Development container (devcontainer)** — Add VS Code devcontainer configuration for instant onboarding with pre-configured Python environment and extensions
- [x] 10.10 — **Interactive debugging mode** — Add `--debug` CLI flag that enables verbose logging, request/response dumping, and an interactive breakpoint on errors for development troubleshooting

---

## Summary

| # | Category | Points |
|---|----------|--------|
| 1 | Architecture & Refactoring | 15 |
| 2 | LLM & Agent Intelligence | 15 |
| 3 | Memory & Context Management | 10 |
| 4 | Performance & Scalability | 10 |
| 5 | Security & Privacy | 10 |
| 6 | Observability & Monitoring | 10 |
| 7 | Reliability & Resilience | 10 |
| 8 | Testing & Quality Assurance | 10 |
| 9 | User Experience & Multimodal | 10 |
| 10 | Developer Experience & DevOps | 10 |
| **Total** | | **110** |

### Priority Matrix

| Priority | Categories | Rationale |
|----------|-----------|-----------|
| **P0 — Critical** | Security (5), Reliability (7) | Production safety: audit trails, injection blocking, crash recovery, data integrity |
| **P1 — High** | LLM Intelligence (2), Performance (4), Observability (6) | User-facing quality: better responses, faster processing, operational visibility |
| **P2 — Medium** | Architecture (1), Memory (3), Testing (8) | Long-term maintainability: clean code, better memory, comprehensive tests |
| **P3 — Nice-to-have** | UX & Multimodal (9), DevEx & DevOps (10) | Growth features: rich media, multi-language, developer tooling |

### Research Sources

- AWS Best Practices for WhatsApp AI Assistants (2025)
- "Using the ReAct Pattern in AI Agents: Best Practices & Pitfalls" (MetaDesign Solutions)
- "20 Agentic AI Workflow Patterns That Actually Work in 2025" (Skywork AI)
- "Context Window Management Strategies for Long-Context AI Agents" (Maxim AI, 2025)
- "Memory Systems for AI Agents: What the Research Says" (Steve Kinney)
- "AI Chatbot Security: Prevent Costly Prompt Injection Risks" (DEV Community)
- "Complete Chatbot Testing Checklist 2025" (Alphabin)
- "Slow Responses in Chatbots: Solutions and Optimization" (Com.bot)
- "The Definitive CI/CD Pipeline for AI Agents" (ActiveWizards)
- "What Is a Multimodal Chatbot and Why It Matters in 2025" (TailorTalk)
- "8 Chat Bot Best Practices for Success in 2025" (Chatiant)
- Existing known gaps: `project/errors/known-gaps.md`
- Existing security gaps: `project/errors/security-gaps.md`
- Existing improvement roadmap: `project/lookup/improvement-roadmap.md`
