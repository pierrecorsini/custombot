"""
bot.py — Core bot orchestrator.

Implements the ReAct (Reason + Act) loop:

  User message
    → load history + memory
    → build LLM context
    → call LLM (with tool definitions)
    → if tool_calls  → execute skill in workspace → append result → loop
    → if stop        → send text response

Each chat has an isolated workspace at  <workspace>/<chat_id>/
All skill I/O is confined to that directory.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_function_tool_call import ChatCompletionMessageFunctionToolCall
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
)

from src.channels.base import IncomingMessage, SendMediaCallback

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
from src.constants import (
    DEFAULT_CHAT_RATE_LIMIT,
    MAX_LRU_CACHE_SIZE,
    MAX_MESSAGE_LENGTH,
    MAX_TOOL_RESULT_PERSIST_LENGTH,
    MEMORY_CHECK_INTERVAL_SECONDS,
    MEMORY_CRITICAL_THRESHOLD_PERCENT,
    MEMORY_WARNING_THRESHOLD_PERCENT,
    RATE_LIMIT_WINDOW_SECONDS,
    SCHEDULED_ERROR_PREFIXES,
    WORKSPACE_DIR,
)
from src.core.event_bus import Event, EventBus, get_event_bus
from src.core.context_assembler import ContextAssembler, ContextResult
from src.core.context_builder import ChatMessage
from src.core.dedup import DedupStats, DeduplicationService
from src.core.instruction_loader import InstructionLoader
from src.core.serialization import serialize_tool_call_message
from src.core.project_context import ProjectContextLoader as _ProjectContextLoaderImpl
from src.core.tool_executor import ToolExecutor
from src.core.tool_formatter import (
    ToolLogEntry,
    format_response_with_tool_log,
    format_single_tool_execution,
)
from src.db import Database
from src.exceptions import ErrorCode, LLMError
from src.llm import LLMClient
from src.logging import clear_correlation_id, get_correlation_id, set_correlation_id
from src.message_queue import MessageQueue
from src.monitoring import PerformanceMetrics, SessionMetrics, get_metrics_collector
from src.rate_limiter import RateLimiter
from src.routing import RoutingEngine
from src.security.audit import SkillAuditLogger
from src.security.prompt_injection import detect_injection, filter_response_content, sanitize_user_input
from src.skills import SkillRegistry
from src.utils import LRULockCache
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.protocols import (
    LockProvider,
    MemoryMonitor,
    MemoryProtocol,
    ProjectContextLoader,
    ProjectStore,
)


log = logging.getLogger(__name__)

# Lifecycle logger for component initialization tracking
lifecycle_log = logging.getLogger("lifecycle.bot")

# Type alias for streaming tool execution updates
StreamCallback = Callable[[str], Awaitable[None]]

# Per-request routing flag — contextvar prevents cross-request state leaks
# when multiple messages are processed concurrently on the event loop.
_routing_show_errors_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "routing_show_errors", default=True
)


@dataclass(slots=True, frozen=True)
class PreflightResult:
    """Immutable result of preflight filter checks.

    Returned by Bot.preflight_check() to indicate whether a message
    passed all read-only filters (validation, emptiness, dedup, routing).
    Used to decide whether to show typing indicators before expensive processing.
    """

    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


@dataclass(slots=True, frozen=True)
class TurnContext:
    """Immutable context assembled for a single ReAct turn.

    Built by ``Bot._build_turn_context()`` from routing match, instruction
    loading, memory reads, and the LLM message list.  Returned as a single
    object so the context-assembly stage can be unit-tested independently of
    the full ReAct loop.
    """

    messages: list[ChatMessage]
    rule_id: str
    skill_exec_verbose: str
    show_errors: bool


@dataclass(slots=True, frozen=True)
class BotConfig:
    """Explicit configuration surface for the Bot orchestrator.

    Extracts the specific values Bot reads from the full application
    config, making the dependency surface narrow, typed, and testable
    without coupling to the entire config structure.

    Constructed in ``builder.py`` from the full ``Config`` and injected
    into ``Bot.__init__()`` — no ``Config`` import needed here.
    """

    max_tool_iterations: int
    memory_max_history: int
    system_prompt_prefix: str
    stream_response: bool = False


class Bot:
    def __init__(
        self,
        config: BotConfig,
        db: Database,
        llm: LLMClient,
        memory: MemoryProtocol,
        skills: SkillRegistry,
        routing: RoutingEngine | None = None,
        instructions_dir: str = "",
        message_queue: MessageQueue | None = None,
        project_store: ProjectStore | None = None,
        project_ctx: ProjectContextLoader | None = None,
        session_metrics: "SessionMetrics | None" = None,
        instruction_loader: InstructionLoader | None = None,
        chat_locks: LockProvider | None = None,
        dedup: DeduplicationService | None = None,
    ) -> None:
        self._cfg = config
        self._db = db
        self._llm = llm
        self._memory = memory
        self._skills = skills
        self._routing = routing
        self._instructions_dir = Path(instructions_dir)
        self._message_queue = message_queue
        self._project_store = project_store
        # Semaphore: only one active LLM call per chat at a time (bounded LRU cache)
        self._chat_locks: LockProvider = (
            chat_locks if chat_locks is not None else LRULockCache(max_size=MAX_LRU_CACHE_SIZE)
        )
        # Unified dedup service — wraps both inbound (message-id) and outbound
        # (content-hash) strategies.  Falls back to direct DB check when not
        # provided (backward-compat for tests that construct Bot directly).
        self._dedup: DeduplicationService | None = dedup
        # Rate limiter for skill execution
        self._rate_limiter = RateLimiter()
        # Per-chat message rate limiter
        self._chat_rate_limiter = RateLimiter()
        # Memory monitor for tracking resource usage
        self._memory_monitor: MemoryMonitor | None = None
        # Performance metrics collector
        self._metrics: PerformanceMetrics = get_metrics_collector()
        # Tool executor (delegates to skill registry with rate limiting and error handling)
        self._tool_executor = ToolExecutor(
            skills_registry=skills,
            rate_limiter=self._rate_limiter,
            metrics=self._metrics,
            on_skill_executed=session_metrics.increment_skills if session_metrics else None,
            audit_logger=SkillAuditLogger(Path(WORKSPACE_DIR) / "logs"),
        )
        # Instruction file loader (mtime-cached) — prefer injected shared instance
        self._instruction_loader = instruction_loader or InstructionLoader(self._instructions_dir)
        # Project context loader — prefer injected shared instance
        self._project_ctx = project_ctx or _ProjectContextLoaderImpl(project_store)
        # Context assembler (stateless service — owns topic cache lifecycle)
        self._context_assembler = ContextAssembler(
            db=db,
            config=config,
            memory=memory,
            project_ctx=self._project_ctx,
            workspace_root=WORKSPACE_DIR,
        )

        # Log bot initialization with component summary
        lifecycle_log.info(
            "Bot instance created - components: db=%s, llm=%s, memory=%s, skills=%d, routing=%s, projects=%s",
            type(db).__name__,
            type(llm).__name__,
            type(memory).__name__,
            len(skills.all()),
            "enabled" if routing else "disabled",
            "enabled" if project_store else "disabled",
        )

    def start_memory_monitoring(self) -> None:
        """
        Start memory monitoring for this bot instance.

        Registers the chat_locks LRU cache for size tracking and
        starts periodic memory checks.
        """
        try:
            from src.monitoring import get_global_monitor

            self._memory_monitor = get_global_monitor(
                warning_threshold_percent=MEMORY_WARNING_THRESHOLD_PERCENT,
                critical_threshold_percent=MEMORY_CRITICAL_THRESHOLD_PERCENT,
            )
            # Register the chat_locks cache for size tracking
            self._memory_monitor.register_cache("chat_locks", lambda: len(self._chat_locks))
            self._memory_monitor.start_periodic_check(
                interval_seconds=MEMORY_CHECK_INTERVAL_SECONDS
            )
            log.info(
                "Memory monitoring started (warning=%.1f%%, critical=%.1f%%, interval=%.1fs)",
                MEMORY_WARNING_THRESHOLD_PERCENT,
                MEMORY_CRITICAL_THRESHOLD_PERCENT,
                MEMORY_CHECK_INTERVAL_SECONDS,
            )
        except ImportError:
            log.warning("psutil not installed - memory monitoring disabled")
        except Exception as e:
            log.error("Failed to start memory monitoring: %s", e, exc_info=True)

    async def stop_memory_monitoring(self) -> None:
        """Stop memory monitoring for this bot instance."""
        if self._memory_monitor:
            await self._memory_monitor.stop()
            self._memory_monitor = None
            log.info("Memory monitoring stopped")

    # ── wiring validation ────────────────────────────────────────────────────

    def validate_wiring(self) -> list[tuple[str, bool, str]]:
        """Validate that all core components are wired correctly.

        Returns a list of (component_name, is_ok, message) tuples.
        Logs an overall summary at INFO or WARNING level.
        """
        checks: list[tuple[str, bool, str]] = [
            ("database", self._db is not None, "Database instance missing"),
            ("llm", self._llm is not None, "LLM client missing"),
            ("memory", self._memory is not None, "Memory instance missing"),
            ("skills", self._skills is not None, "Skill registry missing"),
            ("routing", self._routing is not None, "Routing engine missing"),
        ]
        failed = [name for name, ok, _ in checks if not ok]
        if failed:
            log.warning("Wiring validation FAILED — missing: %s", ", ".join(failed))
        else:
            log.info("Wiring validation passed — all %d components OK", len(checks))
        return checks

    # ── LLM diagnostics ──────────────────────────────────────────────────────

    def get_llm_status(self) -> CircuitBreaker | None:
        """Return the LLM circuit breaker for health/metrics endpoints.

        Provides read-only access to the circuit breaker without exposing
        the private ``_llm`` client.  Returns ``None`` if the LLM client
        is not wired.
        """
        if self._llm is None:
            return None
        return self._llm.circuit_breaker

    def get_dedup_stats(self) -> DedupStats | None:
        """Return dedup hit/miss counters for health/metrics endpoints.

        Returns ``None`` if the dedup service is not wired.
        """
        if self._dedup is None:
            return None
        return self._dedup.stats

    # ── crash recovery ────────────────────────────────────────────────────────

    async def recover_pending_messages(
        self,
        timeout_seconds: int | None = None,
        channel: "BaseChannel | None" = None,
    ) -> dict:
        """
        Recover and process stale pending messages from previous crash.

        Should be called during bot startup to handle messages that were
        interrupted by a crash or restart.

        Args:
            timeout_seconds: Custom timeout for stale detection (uses queue default if not provided).
            channel: Optional channel for sender ACL validation during recovery.

        Returns:
            dict with keys:
            - total_found: int - total stale messages found
            - recovered: int - successfully recovered count
            - failed: int - failed recovery count
            - failures: list - list of {message_id, chat_id, error} dicts
        """
        if not self._message_queue:
            log.debug("No message queue configured, skipping recovery")
            return {"total_found": 0, "recovered": 0, "failed": 0, "failures": []}

        stale_messages = await self._message_queue.recover_stale(timeout_seconds)

        if not stale_messages:
            log.info("No stale messages to recover")
            return {"total_found": 0, "recovered": 0, "failed": 0, "failures": []}

        recovered_count = 0
        failures = []
        skipped_acl = 0
        for queued_msg in stale_messages:
            try:
                # Validate sender against current ACL before reprocessing
                if channel is not None and hasattr(channel, "_is_allowed"):
                    # Use sender_id from queued message, fallback to sender_name
                    sender_id = queued_msg.sender_id or queued_msg.sender_name or ""
                    if not channel._is_allowed(sender_id):
                        log.warning(
                            "Skipping recovery of message %s — sender %s not in allowed_numbers",
                            queued_msg.message_id,
                            sender_id,
                        )
                        skipped_acl += 1
                        continue
                elif channel is None:
                    # No channel available — defer recovery until ACL can be checked
                    log.warning(
                        "Skipping recovery of message %s — no channel for ACL check. "
                        "Recovery should be called after channel initialization.",
                        queued_msg.message_id,
                    )
                    skipped_acl += 1
                    continue

                # Reconstruct IncomingMessage from queued data
                recovered_msg = IncomingMessage(
                    message_id=queued_msg.message_id,
                    chat_id=queued_msg.chat_id,
                    sender_id=queued_msg.sender_id or "",
                    sender_name=queued_msg.sender_name or "",
                    text=queued_msg.text,
                    timestamp=queued_msg.created_at or time.time(),
                )

                # Process the recovered message
                await self.handle_message(recovered_msg)
                recovered_count += 1
                log.info(
                    "Successfully recovered message %s from chat %s",
                    queued_msg.message_id,
                    queued_msg.chat_id,
                )
            except Exception as exc:
                failures.append(
                    {
                        "message_id": queued_msg.message_id,
                        "chat_id": queued_msg.chat_id,
                        "error": str(exc),
                    }
                )
                log.error(
                    "Failed to recover message %s: %s",
                    queued_msg.message_id,
                    exc,
                    exc_info=True,
                )

        # Log recovery summary
        log.info(
            "Recovery complete: %d/%d messages recovered, %d failed, %d skipped (ACL)",
            recovered_count,
            len(stale_messages),
            len(failures),
            skipped_acl,
        )

        return {
            "total_found": len(stale_messages),
            "recovered": recovered_count,
            "failed": len(failures),
            "failures": failures,
        }

    # ── public entry point ─────────────────────────────────────────────────

    async def preflight_check(self, msg: IncomingMessage) -> PreflightResult:
        """Run read-only filter checks before expensive processing.

        Performs lightweight checks (validation, empty, dedup, routing match)
        without side effects. Use before showing typing indicators to avoid
        revealing bot activity for messages that will be filtered out.

        Does NOT check rate limits (check_message_rate records timestamps).

        Args:
            msg: The incoming message to check.

        Returns:
            PreflightResult indicating whether the message should be processed.
        """
        if not isinstance(msg, IncomingMessage):
            return PreflightResult(passed=False, reason="invalid")

        if not msg.text or not msg.text.strip():
            return PreflightResult(passed=False, reason="empty")

        if await self._dedup.is_inbound_duplicate(msg.message_id):
            return PreflightResult(passed=False, reason="duplicate")

        if self._routing:
            matched_rule, _ = self._routing.match_with_rule(msg)
            if not matched_rule:
                return PreflightResult(passed=False, reason="no_routing_rule")

        return PreflightResult(passed=True)

    async def handle_message(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
    ) -> str | None:
        """
        Process an incoming message and return the response text.

        Performs dedup, rate limiting, and the full ReAct loop. Safe to call
        directly — ``preflight_check()`` is an optional pre-filter to avoid
        showing typing indicators for messages that will be rejected anyway.

        Returns None if the message was a duplicate or filtered.

        Args:
            msg: The incoming message to process.
            channel: Optional channel instance for getting channel-specific prompts.
            stream_callback: Optional async callback for streaming tool executions in real-time.
        """
        # Runtime validation for incoming message
        if not isinstance(msg, IncomingMessage):
            log.warning("Invalid incoming message received: %r", msg)
            return None

        # Set correlation ID for request tracing (use custom ID if provided)
        correlation_id = set_correlation_id(msg.correlation_id)

        # Reject empty messages early (optimization + prevents LLM confusion)
        if not msg.text or not msg.text.strip():
            log.debug(
                "Empty message from %s in chat %s, skipping",
                msg.sender_name,
                msg.chat_id,
                extra={"chat_id": msg.chat_id},
            )
            clear_correlation_id()
            return None

        # Reject oversized messages to prevent token overflow and cost spikes
        if len(msg.text) > MAX_MESSAGE_LENGTH:
            log.warning(
                "Message from %s in chat %s exceeds length limit (%d > %d chars), rejecting",
                msg.sender_name,
                msg.chat_id,
                len(msg.text),
                MAX_MESSAGE_LENGTH,
                extra={"chat_id": msg.chat_id, "message_length": len(msg.text)},
            )
            clear_correlation_id()
            return None

        # Log message receipt with correlation ID
        log.info(
            "Processing message %s from %s in chat %s",
            msg.message_id,
            msg.sender_name,
            msg.chat_id,
            extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )

        # Dedup: single authoritative gate.  preflight_check() also performs
        # this check but only as an optimisation to avoid the typing indicator.
        if await self._dedup.is_inbound_duplicate(msg.message_id):
            log.debug(
                "Duplicate message %s from chat %s, skipping",
                msg.message_id,
                msg.chat_id,
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            clear_correlation_id()
            return None

        # Check per-chat message rate limit
        rate_result = self._chat_rate_limiter.check_message_rate(
            msg.chat_id,
            limit=DEFAULT_CHAT_RATE_LIMIT,
            window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        )
        if not rate_result.allowed:
            log.warning(
                "Message rate limit exceeded for chat %s (%d messages/min)",
                msg.chat_id,
                rate_result.limit_value,
                extra={"chat_id": msg.chat_id, "rate_limit": rate_result.limit_value},
            )
            # Send rate limit message directly via channel if available
            if channel:
                await channel.send_message(
                    msg.chat_id,
                    "⚠️ You're sending messages too quickly. Please wait a moment.",
                )
            clear_correlation_id()
            return None

        return await self._handle_message_inner(msg, channel=channel, stream_callback=stream_callback, correlation_id=correlation_id)

    async def _handle_message_inner(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
        correlation_id: str | None = None,
    ) -> str | None:
        """
        Core message processing: acquire lock, enqueue, run ReAct loop, track metrics.

        Called by ``handle_message()`` after validation, dedup, and rate limiting pass.
        Separated so the processing pipeline can be tested in isolation.
        """
        async with self._chat_locks.acquire(msg.chat_id):
            start_time = time.perf_counter()
            generation = self._db.get_generation(msg.chat_id)

            if self._message_queue:
                await self._message_queue.enqueue(msg)

            _routing_show_errors_var.set(True)

            try:
                result = await self._process(msg, channel=channel, stream_callback=stream_callback, generation=generation)

                if self._message_queue:
                    await self._message_queue.complete(msg.message_id)

                processing_time = time.perf_counter() - start_time
                self._metrics.track_message_latency(processing_time)
                self._metrics.track_chat_message(msg.chat_id)

                if self._message_queue:
                    queue_depth = await self._message_queue.get_pending_count()
                    self._metrics.update_queue_depth(queue_depth)

                self._metrics.update_active_chat_count(len(self._chat_locks))

                log.info(
                    "Message %s processed successfully in %.2fs",
                    msg.message_id,
                    processing_time,
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
                return result
            except Exception as exc:
                log.error(
                    "Message processing failed for %s: %s",
                    msg.message_id,
                    exc,
                    exc_info=True,
                    extra={
                        "chat_id": msg.chat_id,
                        "message_id": msg.message_id,
                        "correlation_id": correlation_id,
                    },
                )

                if not _routing_show_errors_var.get():
                    log.info(
                        "Error suppressed (showErrors=false) for message %s",
                        msg.message_id,
                    )
                    return None

                raise
            finally:
                clear_correlation_id()

    # ── scheduled task processing ──────────────────────────────────────────

    async def process_scheduled(
        self,
        chat_id: str,
        prompt: str,
        channel: "BaseChannel | None" = None,
    ) -> str | None:
        """
        Process a scheduled task prompt directly, bypassing routing and dedup.

        Unlike handle_message, this is a system-level entry point:
        - No routing rule matching (the scheduler already knows the target chat)
        - No dedup (synthetic message IDs)
        - No rate limiting
        - Uses the channel prompt from the delivery channel (no file lookup)

        Args:
            chat_id: Target chat identifier.
            prompt: The task prompt to execute.
            channel: The delivery channel (used to get channel-specific prompt).

        Returns:
            The LLM response text, or None on failure.
        """
        correlation_id = set_correlation_id(f"sched_{chat_id}_{uuid.uuid4().hex[:8]}")

        log.info(
            "Processing scheduled task for chat %s",
            chat_id,
            extra={"chat_id": chat_id},
        )

        # Acquire per-chat lock to prevent concurrent execution with handle_message
        # or other scheduled tasks for the same chat (ref-tracked for safe eviction)
        async with self._chat_locks.acquire(chat_id):
            try:
                # Ensure per-chat workspace directory exists
                try:
                    workspace_dir = self._memory.ensure_workspace(chat_id)
                except OSError as exc:
                    log.warning(
                        "Scheduled task for chat %s aborted: workspace creation failed: %s",
                        chat_id,
                        exc,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                # Get channel-specific prompt (e.g. WhatsApp formatting rules)
                channel_prompt = channel.get_channel_prompt() if channel else None

                # Build LLM context with the scheduled prompt as the user message
                try:
                    result = await self._context_assembler.assemble(
                        chat_id=chat_id,
                        channel_prompt=channel_prompt,
                    )
                    # Sanitize scheduled prompt to catch injection attempts that
                    # could bypass normal message pipeline safeguards.
                    safe_prompt = sanitize_user_input(prompt)
                    injection_result = detect_injection(safe_prompt)
                    if injection_result.detected:
                        log.warning(
                            "Scheduled task prompt for chat %s flagged as injection "
                            "(confidence=%.1f, patterns=%s) — sanitizing",
                            chat_id,
                            injection_result.confidence,
                            injection_result.matched_patterns,
                            extra={
                                "chat_id": chat_id,
                                "injection_patterns": injection_result.matched_patterns,
                            },
                        )
                    messages = result.messages
                    messages.append(ChatMessage(role="user", content=safe_prompt))
                except OSError as exc:
                    log.warning(
                        "Scheduled task for chat %s aborted: context build failed: %s",
                        chat_id,
                        exc,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                # Run the ReAct loop
                tools = self._skills.tool_definitions
                response_text, _ = await self._react_loop(
                    chat_id=chat_id,
                    messages=[m.to_api_dict() for m in messages],
                    tools=tools if tools else None,
                    workspace_dir=workspace_dir,
                    channel=channel,
                )

                # Skip persistence for known error responses (circuit breaker,
                # empty LLM output, max iterations) — they are not real content.
                if response_text and any(
                    response_text.startswith(prefix)
                    for prefix in SCHEDULED_ERROR_PREFIXES
                ):
                    log.warning(
                        "Scheduled task for chat %s produced an error response, "
                        "skipping persistence: %.80s",
                        chat_id,
                        response_text,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                # Guard against None response_text (e.g. circuit breaker returned
                # an error, LLM produced empty response, or max iterations hit).
                if response_text is None:
                    log.warning(
                        "Scheduled task for chat %s produced None response, "
                        "skipping persistence",
                        chat_id,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                # Parse topic META from response and update topic cache
                response_text = self._context_assembler.finalize_turn(chat_id, response_text)

                # Filter sensitive content (PII, secrets, API keys) before persisting
                filter_result = filter_response_content(response_text)
                if filter_result.flagged:
                    response_text = filter_result.sanitized_content
                    log.warning(
                        "Filtered sensitive content from scheduled response: %s",
                        filter_result.categories,
                        extra={
                            "chat_id": chat_id,
                            "filter_categories": filter_result.categories,
                        },
                    )

                # Persist both turns in conversation history
                await self._db.upsert_chat(chat_id, "Scheduler")
                await self._db.save_message(
                    chat_id=chat_id,
                    role="user",
                    content=prompt,
                    name="Scheduler",
                    message_id=f"sched_{uuid.uuid4().hex[:8]}",
                )
                await self._db.save_message(
                    chat_id=chat_id,
                    role="assistant",
                    content=response_text,
                )

                log.info(
                    "Scheduled task for chat %s completed successfully",
                    chat_id,
                    extra={"chat_id": chat_id},
                )
                return response_text

            except Exception as exc:
                log.error(
                    "Scheduled task failed for chat %s: %s",
                    chat_id,
                    exc,
                    exc_info=True,
                    extra={"chat_id": chat_id, "correlation_id": correlation_id},
                )
                return None
            finally:
                clear_correlation_id()

    # ── internal processing ────────────────────────────────────────────────

    async def _build_turn_context(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
    ) -> TurnContext | None:
        """Match routing rule, load instruction, and assemble LLM messages.

        Returns ``None`` when routing is disabled or no rule matches.
        """
        # 1. Match routing rule to get instruction file
        if not self._routing:
            log.warning("No routing engine configured, skipping message")
            return None

        matched_rule, instruction_filename = self._routing.match_with_rule(msg)
        if not matched_rule:
            log.info(
                "No routing rule matched for message from %s (fromMe=%s, toMe=%s), ignoring",
                msg.sender_id,
                msg.fromMe,
                msg.toMe,
            )
            return None

        # Store whether errors should be sent to channel for this request
        _routing_show_errors_var.set(matched_rule.showErrors)

        log.info(
            "Matched routing rule '%s' (instruction: %s) for message from %s",
            matched_rule.id,
            instruction_filename,
            msg.sender_id,
        )

        # 2. Load instruction and channel-specific prompt
        instruction_content = self._load_instruction(instruction_filename)
        channel_prompt = channel.get_channel_prompt() if channel else None

        # 3. Build LLM message list (async — fetches history from DB)
        result = await self._context_assembler.assemble(
            chat_id=msg.chat_id,
            channel_prompt=channel_prompt,
            instruction=instruction_content,
            rule_id=matched_rule.id,
        )

        return TurnContext(
            messages=result.messages,
            rule_id=result.rule_id,
            skill_exec_verbose=matched_rule.skillExecVerbose,
            show_errors=matched_rule.showErrors,
        )

    async def _process(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
        generation: int = 0,
    ) -> str:
        log.debug(
            "Starting _process for chat %s",
            msg.chat_id,
            extra={"chat_id": msg.chat_id},
        )

        # Emit message_received event for plugins/subscribers
        await get_event_bus().emit(Event(
            name="message_received",
            data={"chat_id": msg.chat_id, "sender": msg.sender_name},
            source="Bot._process",
            correlation_id=get_correlation_id(),
        ))

        # 1. Persist user turn
        await self._db.upsert_chat(msg.chat_id, msg.sender_name)
        try:
            await self._db.save_message(
                chat_id=msg.chat_id,
                role="user",
                content=msg.text,
                name=msg.sender_name,
                message_id=msg.message_id,
            )
        except Exception as exc:
            log.error(
                "Failed to persist user turn for chat %s: %s",
                msg.chat_id,
                exc,
                exc_info=True,
                extra={"chat_id": msg.chat_id},
            )

        # 2. Ensure per-chat workspace directory exists (seeds AGENTS.md)
        workspace_dir = self._memory.ensure_workspace(msg.chat_id)

        # 3. Assemble turn context (routing, instruction, memory, context)
        ctx = await self._build_turn_context(msg, channel)
        if not ctx:
            return None

        # 4. Run the ReAct loop with optional streaming
        tools = self._skills.tool_definitions
        verbose = ctx.skill_exec_verbose
        stream_cb = stream_callback if verbose == "full" else None
        raw_response, tool_log, buffered_persist = await self._react_loop(
            chat_id=msg.chat_id,
            messages=[m.to_api_dict() for m in ctx.messages],
            tools=tools if tools else None,
            workspace_dir=workspace_dir,
            stream_callback=stream_cb,
            channel=channel,
        )

        # 5. Parse topic META from response and update topic cache
        response_text = self._context_assembler.finalize_turn(msg.chat_id, raw_response)

        # 5b. Filter sensitive content (PII, secrets, API keys) from LLM response
        filter_result = filter_response_content(response_text)
        if filter_result.flagged:
            response_text = filter_result.sanitized_content
            log.warning(
                "Filtered sensitive content from LLM response: %s",
                filter_result.categories,
                extra={
                    "chat_id": msg.chat_id,
                    "filter_categories": filter_result.categories,
                },
            )

        # 6. Append tool summary if skillExecVerbose == "summary"
        if verbose == "summary" and tool_log:
            response_text = format_response_with_tool_log(response_text, tool_log)

        # 7. Persist assistant turn + buffered tool messages in a single batch write
        #    Check generation to detect concurrent writes (e.g. scheduled task).
        batch = [*buffered_persist, {"role": "assistant", "content": response_text}]
        if not self._db.check_generation(msg.chat_id, generation):
            log.warning(
                "Write conflict detected for chat %s — generation changed during "
                "processing. Re-reading latest history before persist.",
                msg.chat_id,
                extra={"chat_id": msg.chat_id},
            )
        await self._db.save_messages_batch(chat_id=msg.chat_id, messages=batch)

        # Emit response_sent event for plugins/subscribers
        await get_event_bus().emit(Event(
            name="response_sent",
            data={"chat_id": msg.chat_id, "response_length": len(response_text)},
            source="Bot._process",
            correlation_id=get_correlation_id(),
        ))

        return response_text

    # ── ReAct loop ─────────────────────────────────────────────────────────

    async def _react_loop(
        self,
        chat_id: str,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: "BaseChannel | None" = None,
    ) -> tuple[str, list[ToolLogEntry], list[dict]]:
        """
        Run the ReAct loop and return response text, tool log, and buffered messages.

        Args:
            chat_id: Chat identifier for logging.
            messages: Conversation history for LLM context.
            tools: Available tool definitions.
            workspace_dir: Workspace directory for skill execution.
            stream_callback: Optional callback to stream tool executions in real-time.
            channel: Optional channel for media-sending callback injection.

        Returns:
            Tuple of (response_text, tool_log, buffered_persist) where *tool_log*
            contains :class:`ToolLogEntry` records and *buffered_persist* holds
            dicts suitable for :meth:`Database.save_messages_batch` — one per
            assistant tool-call turn and one per tool-result message accumulated
            across all iterations.
        """
        max_iter = self._cfg.max_tool_iterations
        tool_log: list[ToolLogEntry] = []
        buffered_persist: list[dict] = []
        use_streaming = self._cfg.stream_response

        for iteration in range(max_iter):
            # Track LLM latency
            llm_start = time.perf_counter()
            try:
                if use_streaming:
                    # Build a streaming-aware callback that forwards text
                    # chunks to the user via the stream_callback (which
                    # sends via the channel).  Only flush chunks for the
                    # final text response — tool-call iterations accumulate
                    # silently.
                    completion = await self._llm.chat_stream(
                        messages,
                        tools=tools,
                        on_chunk=stream_callback,
                        chat_id=chat_id,
                    )
                else:
                    completion = await self._llm.chat(messages, tools=tools, chat_id=chat_id)
            except LLMError as exc:
                if exc.error_code == ErrorCode.LLM_CIRCUIT_BREAKER_OPEN:
                    log.warning("Circuit breaker open — returning unavailable message")
                    self._metrics.track_react_iterations(iteration + 1)
                    self._metrics.track_conversation_depth(chat_id, iteration + 1)
                    return (
                        "⚠️ Service temporarily unavailable. "
                        "The AI provider is experiencing issues. Please try again in a minute.",
                        tool_log,
                        buffered_persist,
                    )
                raise
            llm_latency = time.perf_counter() - llm_start
            self._metrics.track_llm_latency(llm_latency)

            choice = completion.choices[0]
            finish = choice.finish_reason

            # Check for tool calls — either explicit finish_reason or edge case
            if finish == "tool_calls" or choice.message.tool_calls:
                iteration_log, iteration_buffered = await self._process_tool_calls(
                    choice,
                    messages,
                    chat_id,
                    workspace_dir,
                    stream_callback,
                    channel,
                )
                tool_log.extend(iteration_log)
                buffered_persist.extend(iteration_buffered)
            else:
                # LLM is done — return the final text
                content = choice.message.content
                if not content or not content.strip():
                    self._metrics.track_react_iterations(iteration + 1)
                    self._metrics.track_conversation_depth(chat_id, iteration + 1)
                    return (
                        "(The assistant generated an empty response. "
                        "Please try rephrasing your request.)",
                        tool_log,
                        buffered_persist,
                    )
                self._metrics.track_react_iterations(iteration + 1)
                self._metrics.track_conversation_depth(chat_id, iteration + 1)
                return content, tool_log, buffered_persist

        log.warning(
            "Reached max tool iterations (%d) for chat %s",
            max_iter,
            chat_id,
            extra={"chat_id": chat_id, "max_iterations": max_iter},
        )
        self._metrics.track_react_iterations(max_iter)
        self._metrics.track_conversation_depth(chat_id, max_iter)
        # Build informative truncation message with tool summary
        tool_summary = ""
        if tool_log:
            tool_names = [entry.name for entry in tool_log]
            unique_tools = dict.fromkeys(tool_names)  # preserve order, deduplicate
            tool_summary = f"\n\n🔧 Tools used ({len(tool_log)} calls): {', '.join(unique_tools)}"
        return (
            f"⚠️ Reached maximum tool iterations ({max_iter}). "
            f"The task may be too complex for a single request. "
            f"Try breaking it into smaller steps.{tool_summary}",
            tool_log,
            buffered_persist,
        )

    async def _process_tool_calls(
        self,
        choice: Choice,
        messages: list[ChatCompletionMessageParam],
        chat_id: str,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: "BaseChannel | None" = None,
    ) -> tuple[list[ToolLogEntry], list[dict]]:
        """Process tool calls from an LLM response and append results to messages.

        Executes all requested tool calls in parallel via ``asyncio.TaskGroup``,
        then appends results to *messages* in the original call order so the
        LLM receives correctly-ordered tool-call-result pairs.

        Returns:
            Tuple of (tool_log, buffered_persist).  *buffered_persist* contains
            dicts suitable for :meth:`Database.save_messages_batch` — one dict
            per assistant tool-call turn and one per tool-result message.
        """
        tool_log: list[ToolLogEntry] = []
        buffered_persist: list[dict] = []

        # Append the assistant's tool-call turn to context
        assistant_msg = serialize_tool_call_message(choice.message)
        messages.append(assistant_msg)
        buffered_persist.append(
            {"role": "assistant", "content": assistant_msg.get("content") or ""}
        )

        # Create send_media callback if channel is available
        send_media = None
        if channel is not None:
            from src.channels.base import SendMediaCallback

            async def _send_media(kind: str, path: "Path", caption: str = "") -> None:
                """Route media to the appropriate channel send method."""
                try:
                    if kind == "audio":
                        await channel.send_audio(chat_id, path, ptt=True)
                    elif kind == "document":
                        await channel.send_document(chat_id, path, caption=caption)
                    else:
                        log.warning("Unknown media kind: %s", kind)
                except Exception as exc:
                    log.error(
                        "send_media callback failed: %s",
                        exc,
                        extra={"chat_id": chat_id, "correlation_id": get_correlation_id()},
                    )

            send_media = _send_media

        tool_calls = choice.message.tool_calls or []
        if not tool_calls:
            return tool_log, buffered_persist

        # Execute all function-type tool calls in parallel via structured concurrency
        function_calls = [tc for tc in tool_calls if tc.type == "function"]
        results: list[tuple[str, str, ToolLogEntry]] = []
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [
                    tg.create_task(
                        self._execute_tool_call(tc, chat_id, workspace_dir, send_media)
                    )
                    for tc in function_calls
                ]
            results = [t.result() for t in tasks]
        except BaseException as exc:
            # TaskGroup cancels all siblings on BaseException (e.g. KeyboardInterrupt).
            # Salvage whatever results completed before the cancellation.
            log.warning(
                "TaskGroup interrupted in chat %s: %s — salvaging %d partial results",
                chat_id,
                type(exc).__name__,
                results_count := sum(1 for t in tasks if t.done() and not t.cancelled()),
                extra={"chat_id": chat_id},
            )
            for t in tasks:
                if t.done() and not t.cancelled():
                    try:
                        results.append(t.result())
                    except Exception:
                        pass

        # Append results to messages in original order
        for tc_id, content, tool_entry in results:
            tool_msg: ChatCompletionToolMessageParam = {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": content,
            }
            messages.append(tool_msg)
            tool_log.append(tool_entry)
            # Truncate large results in persisted history to keep JSONL bounded.
            # The full content remains in the in-memory messages list for the
            # current ReAct iteration.
            if len(content) > MAX_TOOL_RESULT_PERSIST_LENGTH:
                persist_content = (
                    content[:MAX_TOOL_RESULT_PERSIST_LENGTH]
                    + f"\n[truncated, full length: {len(content)}]"
                )
            else:
                persist_content = content
            buffered_persist.append(
                {"role": "tool", "content": persist_content, "name": tool_entry.name}
            )
            if stream_callback:
                formatted = format_single_tool_execution(tool_entry)
                await stream_callback(formatted)

        return tool_log, buffered_persist

    async def _execute_tool_call(
        self,
        tool_call: ChatCompletionMessageFunctionToolCall,
        chat_id: str,
        workspace_dir: Path,
        send_media: SendMediaCallback | None,
    ) -> tuple[str, str, ToolLogEntry]:
        """Execute a single tool call, returning result data for message assembly.

        Returns:
            Tuple of ``(tool_call_id, result_content, tool_log_entry)``.
            Never raises — returns error content on any failure.
        """
        tc_id = tool_call.id
        try:
            if not tool_call.function or not tool_call.function.name:
                raise AttributeError("tool_call.function or name is missing")

            # Belt-and-suspenders: verify workspace_dir hasn't escaped WORKSPACE_DIR
            # (defends against a malicious chat_id that bypasses sanitization).
            ws_resolved = workspace_dir.resolve()
            root_resolved = Path(WORKSPACE_DIR).resolve()
            if not ws_resolved.is_relative_to(root_resolved):
                log.error(
                    "Path traversal detected: workspace_dir %s is outside %s for chat %s",
                    ws_resolved,
                    root_resolved,
                    chat_id,
                    extra={"chat_id": chat_id},
                )
                return (
                    tc_id,
                    "⚠️ Workspace path validation failed. This incident has been logged.",
                    ToolLogEntry(
                        name=tool_call.function.name,
                        args={},
                        result="Path traversal blocked.",
                    ),
                )

            result = await self._tool_executor.execute(
                chat_id=chat_id,
                tool_call=tool_call,
                workspace_dir=workspace_dir,
                send_media=send_media,
            )
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return (
                tc_id,
                result,
                ToolLogEntry(name=tool_call.function.name, args=args, result=result),
            )

        except (AttributeError, TypeError) as exc:
            log.error(
                "Malformed tool_call structure in chat %s: %s",
                chat_id,
                exc,
                extra={"chat_id": chat_id, "correlation_id": get_correlation_id()},
                exc_info=True,
            )
            return (
                tc_id,
                "⚠️ Malformed tool call: function name or "
                "arguments were missing or invalid. "
                "Please retry with properly formatted tool calls.",
                ToolLogEntry(
                    name=getattr(tool_call.function, "name", "unknown"),
                    args={},
                    result="Malformed tool call — skipped.",
                ),
            )

    # ── helpers ────────────────────────────────────────────────────────────

    def _load_instruction(self, filename: str) -> str:
        """Load instruction content via the InstructionLoader."""
        return self._instruction_loader.load(filename)
