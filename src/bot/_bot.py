"""
_bot.py — Core bot orchestrator.

Thin ``Bot`` class that wires together the extracted submodules:

- :mod:`src.bot.preflight` — lightweight pre-filter checks
- :mod:`src.bot.crash_recovery` — stale message recovery
- :mod:`src.bot.react_loop` — ReAct (Reason + Act) loop
- :mod:`src.bot.context_building` — routing match + context assembly
- :mod:`src.bot.response_delivery` — post-ReAct response delivery pipeline

The ``Bot`` class owns construction, lifecycle, diagnostics, and the
public entry points (``handle_message``, ``process_scheduled``).  Heavy
logic is delegated to the standalone functions in each submodule to keep
this file navigable and reduce merge conflicts.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.channels.base import IncomingMessage
from src.constants import (
    DEFAULT_CHAT_LOCK_CACHE_SIZE,
    DEFAULT_CHAT_RATE_LIMIT,
    MAX_MESSAGE_LENGTH,
    MEMORY_CHECK_INTERVAL_SECONDS,
    MEMORY_CRITICAL_THRESHOLD_PERCENT,
    MEMORY_WARNING_THRESHOLD_PERCENT,
    RATE_LIMIT_WINDOW_SECONDS,
    REACT_LOOP_MAX_RETRIES,
    REACT_LOOP_RETRY_INITIAL_DELAY,
    SCHEDULED_ERROR_PREFIXES,
    WORKSPACE_DIR,
)
from src.core.context_assembler import ContextAssembler
from src.core.context_builder import ChatMessage
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import Event, get_event_bus
from src.core.instruction_loader import InstructionLoader
from src.core.project_context import ProjectContextLoader as _ProjectContextLoaderImpl
from src.core.tool_executor import ToolExecutor
from src.utils.validation import _validate_chat_id
from src.exceptions import ErrorCode
from src.logging import clear_correlation_id, get_correlation_id, set_correlation_id
from src.monitoring import NullMemoryMonitor, get_metrics_collector
from src.monitoring.tracing import (
    get_tracer,
    record_exception_safe,
    set_correlation_id_on_span,
)
from src.rate_limiter import RateLimiter
from src.security.prompt_injection import (
    detect_injection,
    filter_response_content,
    sanitize_user_input,
)
from src.security.signing import (
    get_scheduler_secret,
    verify_payload,
)
from src.security.audit import audit_log
from src.utils import LRULockCache
from src.utils.timing import elapsed as _elapsed, set_timer_start as _set_timer_start

from src.bot.context_building import (
    TurnContext,
    build_turn_context as _build_turn_context,
    routing_show_errors_var,
)
from src.bot.crash_recovery import recover_pending_messages as _recover_pending_messages
from src.bot.preflight import preflight_check as _preflight_check
from src.bot.react_loop import (
    react_loop as _react_loop,
)
from src.bot.response_delivery import (
    deliver_response as _deliver_response,
    send_to_chat as _send_to_chat,
)

if TYPE_CHECKING:
    from src.bot.react_loop import StreamCallback
    from src.core.tool_formatter import ToolLogEntry
    from src.bot.preflight import PreflightResult
    from src.db import Database
    from src.monitoring import PerformanceMetrics, SessionMetrics
    from src.core.dedup import DeduplicationService
    from src.message_queue import MessageQueue
    from src.skills import SkillRegistry
    from src.utils.protocols import (
        LockProvider,
        MemoryMonitor,
        MemoryProtocol,
        ProjectContextLoader,
        ProjectStore,
    )
    from src.channels.base import BaseChannel, SendMediaCallback
    from src.llm import LLMProvider
    from src.routing import RoutingEngine


log = logging.getLogger(__name__)

lifecycle_log = logging.getLogger("lifecycle.bot")

__all__ = ["Bot", "BotConfig", "BotDeps", "TurnContext"]

# LLM error codes that are transient and worth retrying.
_RETRYABLE_LLM_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.LLM_RATE_LIMITED,
        ErrorCode.LLM_TIMEOUT,
        ErrorCode.LLM_CONNECTION_FAILED,
    }
)


@dataclass(slots=True, frozen=True)
class _PreparedTurn:
    """Immutable result of turn-preparation (persist + workspace + routing).

    Built by ``Bot._prepare_turn()`` so that the preparation stage
    (user-message persistence, workspace seeding, context assembly) can be
    tested independently of the ReAct loop.
    """

    ctx: TurnContext
    workspace_dir: Path


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
    per_chat_timeout: float = 300.0
    react_loop_timeout: float = 0.0
    max_concurrent_messages: int = 10


@dataclass(slots=True)
class BotDeps:
    """Structured parameter bag for ``Bot.__init__``.

    Replaces the former 15-parameter constructor signature with a single
    dataclass, mirroring ``ShutdownContext`` from ``src/lifecycle.py``.
    Required fields correspond to components that are always available;
    optional fields default to ``None`` and the ``Bot`` constructor
    supplies sensible fallbacks (e.g. ``LRULockCache`` for ``chat_locks``).
    """

    # Required
    config: BotConfig
    db: Database
    llm: LLMProvider
    memory: MemoryProtocol
    skills: SkillRegistry

    # Optional — Bot supplies defaults when not provided
    routing: RoutingEngine | None = None
    instructions_dir: str = ""
    message_queue: MessageQueue | None = None
    project_store: ProjectStore | None = None
    project_ctx: ProjectContextLoader | None = None
    session_metrics: "SessionMetrics | None" = None
    instruction_loader: InstructionLoader | None = None
    chat_locks: LockProvider | None = None
    dedup: DeduplicationService | None = None


class Bot:
    def __init__(self, deps: BotDeps) -> None:
        self._cfg = deps.config
        self._db = deps.db
        self._llm = deps.llm
        self._memory = deps.memory
        self._skills = deps.skills
        self._routing = deps.routing
        self._instructions_dir = Path(deps.instructions_dir)
        self._message_queue = deps.message_queue
        self._project_store = deps.project_store
        # Semaphore: only one active LLM call per chat at a time (bounded LRU cache)
        self._chat_locks: LockProvider = (
            deps.chat_locks
            if deps.chat_locks is not None
            else LRULockCache(max_size=DEFAULT_CHAT_LOCK_CACHE_SIZE)
        )
        # Unified dedup service — wraps both inbound (message-id) and outbound
        # (content-hash) strategies.  Falls back to direct DB check when not
        # provided (backward-compat for tests that construct Bot directly).
        self._dedup: DeduplicationService | None = deps.dedup
        # Unified rate limiter for both skill execution and per-chat message
        # rate limiting.
        self._rate_limiter = RateLimiter()
        # Memory monitor for tracking resource usage
        self._memory_monitor: MemoryMonitor = NullMemoryMonitor()
        # Performance metrics collector
        self._metrics: PerformanceMetrics = get_metrics_collector()
        # Tool executor (delegates to skill registry with rate limiting and error handling)
        self._tool_executor = ToolExecutor(
            skills_registry=deps.skills,
            rate_limiter=self._rate_limiter,
            metrics=self._metrics,
            on_skill_executed=deps.session_metrics.increment_skills
            if deps.session_metrics
            else None,
            audit_log_dir=Path(WORKSPACE_DIR) / "logs",
        )
        # Instruction file loader (mtime-cached) — prefer injected shared instance
        self._instruction_loader = deps.instruction_loader or InstructionLoader(
            self._instructions_dir
        )
        # Project context loader — prefer injected shared instance
        self._project_ctx = deps.project_ctx or _ProjectContextLoaderImpl(deps.project_store)
        # Context assembler (stateless service — owns topic cache lifecycle)
        self._context_assembler = ContextAssembler(
            db=deps.db,
            config=deps.config,
            memory=deps.memory,
            project_ctx=self._project_ctx,
            workspace_root=WORKSPACE_DIR,
        )

        # Log bot initialization with component summary
        lifecycle_log.info(
            "Bot instance created - components: db=%s, llm=%s, memory=%s, skills=%d, routing=%s, projects=%s",
            type(deps.db).__name__,
            type(deps.llm).__name__,
            type(deps.memory).__name__,
            len(deps.skills.all()),
            "enabled" if deps.routing else "disabled",
            "enabled" if deps.project_store else "disabled",
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start_memory_monitoring(self) -> None:
        """Start memory monitoring for this bot instance."""
        try:
            from src.monitoring import get_global_monitor

            self._memory_monitor = get_global_monitor(
                warning_threshold_percent=MEMORY_WARNING_THRESHOLD_PERCENT,
                critical_threshold_percent=MEMORY_CRITICAL_THRESHOLD_PERCENT,
            )
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
        except Exception as exc:
            log.error("Failed to start memory monitoring: %s", exc, exc_info=True)

    async def stop_memory_monitoring(self) -> None:
        """Stop memory monitoring for this bot instance."""
        await self._memory_monitor.stop()
        self._memory_monitor = NullMemoryMonitor()
        log.info("Memory monitoring stopped")

    def close_executor(self) -> None:
        """Close the tool executor's audit logger during shutdown."""
        self._tool_executor.close()

    # ── wiring validation ────────────────────────────────────────────────────

    def validate_wiring(self) -> list[tuple[str, bool, str]]:
        """Validate that all core components are wired correctly."""
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

    # ── crash recovery ────────────────────────────────────────────────────────

    async def recover_pending_messages(
        self,
        timeout_seconds: int | None = None,
        channel: "BaseChannel | None" = None,
    ) -> dict[str, Any]:
        """Recover and process stale pending messages from previous crash.

        Delegates to :func:`src.bot.crash_recovery.recover_pending_messages`.
        """
        if not self._message_queue:
            log.debug("No message queue configured, skipping recovery")
            return {"total_found": 0, "recovered": 0, "failed": 0, "failures": []}

        return await _recover_pending_messages(
            message_queue=self._message_queue,
            handle_message=self.handle_message,
            timeout_seconds=timeout_seconds,
            channel=channel,
            max_concurrent=self._cfg.max_concurrent_messages,
        )

    # ── preflight ─────────────────────────────────────────────────────────────

    async def preflight_check(self, msg: IncomingMessage) -> PreflightResult:
        """Run read-only filter checks before expensive processing.

        Delegates to :func:`src.bot.preflight.preflight_check`.
        """
        if self._dedup is None:
            return PreflightResult(passed=False, reason="no_dedup")
        return await _preflight_check(
            msg=msg,
            dedup=self._dedup,
            routing=self._routing,
        )

    # ── public entry point ─────────────────────────────────────────────────

    async def handle_message(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
    ) -> str | None:
        """Process an incoming message and return the response text.

        Performs dedup, rate limiting, and the full ReAct loop. Safe to call
        directly — ``preflight_check()`` is an optional pre-filter to avoid
        showing typing indicators for messages that will be rejected anyway.

        Returns None if the message was a duplicate or filtered.
        """
        if not isinstance(msg, IncomingMessage):
            log.warning("Invalid incoming message received: %r", msg)
            return None

        if not msg.acl_passed:
            log.warning(
                "Rejecting message %s from %s in chat %s — ACL not passed. "
                "Messages must go through a channel that enforces access control.",
                msg.message_id,
                msg.sender_id,
                msg.chat_id,
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            clear_correlation_id()
            return None

        correlation_id = set_correlation_id(msg.correlation_id)

        if not msg.text or not msg.text.strip():
            log.debug(
                "Empty message from %s in chat %s, skipping",
                msg.sender_name,
                msg.chat_id,
                extra={"chat_id": msg.chat_id},
            )
            clear_correlation_id()
            return None

        if len(msg.text) > MAX_MESSAGE_LENGTH:
            log.warning(
                "Message from %s in chat %s exceeds length limit (%d > %d chars), rejecting",
                msg.sender_name,
                msg.chat_id,
                len(msg.text),
                MAX_MESSAGE_LENGTH,
                extra={"chat_id": msg.chat_id, "message_length": len(msg.text)},
            )
            await get_event_bus().emit(
                Event(
                    name="message_dropped",
                    data={
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "message_too_long",
                        "message_length": len(msg.text),
                    },
                    source="Bot.handle_message",
                    correlation_id=get_correlation_id(),
                )
            )
            clear_correlation_id()
            return None

        log.info(
            "Processing message %s from %s in chat %s",
            msg.message_id,
            msg.sender_name,
            msg.chat_id,
            extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
        )

        try:
            if self._dedup is not None and await self._dedup.is_inbound_duplicate(msg.message_id):
                log.debug(
                    "Duplicate message %s from chat %s, skipping",
                    msg.message_id,
                    msg.chat_id,
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
                clear_correlation_id()
                return None
        except Exception:
            # Don't clear correlation_id here — the finally block in
            # _handle_message_inner will do it, and clearing early loses
            # the ID for error tracking if the exception propagates.
            raise

        rate_result = self._rate_limiter.check_message_rate(
            msg.chat_id,
            limit=DEFAULT_CHAT_RATE_LIMIT,
            window_seconds=int(RATE_LIMIT_WINDOW_SECONDS),
        )
        if not rate_result.allowed:
            log.warning(
                "Message rate limit exceeded for chat %s (%d messages/min)",
                msg.chat_id,
                rate_result.limit_value,
                extra={"chat_id": msg.chat_id, "rate_limit": rate_result.limit_value},
            )
            await _send_to_chat(
                msg.chat_id,
                "⚠️ You're sending messages too quickly. Please wait a moment.",
                dedup=self._dedup,
                channel=channel,
            )
            clear_correlation_id()
            return None

        return await self._handle_message_inner(
            msg, channel=channel, stream_callback=stream_callback, correlation_id=correlation_id
        )

    async def _handle_message_inner(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
        correlation_id: str | None = None,
    ) -> str | None:
        """Core message processing: acquire lock, enqueue, run ReAct loop, track metrics."""
        tracer = get_tracer()
        with tracer.start_as_current_span(
            "message.process",
            attributes={
                "messaging.destination": msg.chat_id,
                "messaging.message.id": msg.message_id,
            },
        ) as proc_span:
            set_correlation_id_on_span(proc_span, correlation_id)

            async with self._chat_locks.acquire(msg.chat_id):
                _set_timer_start()
                generation = self._db.get_generation(msg.chat_id)

                if self._message_queue:
                    await self._message_queue.enqueue(msg)

                routing_show_errors_var.set(True)

                try:
                    # Wrap the processing pipeline in a per-chat timeout.
                    # A stuck LLM call or tool execution holds the per-chat
                    # lock indefinitely — this cancels the turn and releases
                    # the lock so subsequent messages can be processed.
                    timeout = self._cfg.per_chat_timeout
                    if timeout and timeout > 0:
                        result = await asyncio.wait_for(
                            self._process(
                                msg,
                                channel=channel,
                                stream_callback=stream_callback,
                                generation=generation,
                            ),
                            timeout=timeout,
                        )
                    else:
                        result = await self._process(
                            msg,
                            channel=channel,
                            stream_callback=stream_callback,
                            generation=generation,
                        )

                    if self._message_queue:
                        await self._message_queue.complete(msg.message_id)

                    processing_time = _elapsed()
                    self._metrics.track_message_latency(processing_time)
                    self._metrics.track_chat_message(msg.chat_id)

                    if self._message_queue:
                        queue_depth = await self._message_queue.get_pending_count()
                        self._metrics.update_queue_depth(queue_depth)

                    self._metrics.update_active_chat_count(len(self._chat_locks))
                    proc_span.set_attribute(
                        "custombot.processing_time_ms",
                        round(processing_time * 1000, 2),
                    )

                    log.info(
                        "Message %s processed successfully in %.2fs",
                        msg.message_id,
                        processing_time,
                        extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                    )
                    return result
                except asyncio.TimeoutError:
                    processing_time = _elapsed()
                    record_exception_safe(proc_span, asyncio.TimeoutError())
                    log.error(
                        "Message %s TIMED OUT after %.1fs (per_chat_timeout=%.1fs) "
                        "in chat %s — stuck turn cancelled, lock released",
                        msg.message_id,
                        processing_time,
                        timeout,
                        msg.chat_id,
                        extra={
                            "chat_id": msg.chat_id,
                            "message_id": msg.message_id,
                            "correlation_id": correlation_id,
                            "timeout_seconds": timeout,
                        },
                    )
                    # Best-effort: mark the queue message as completed so it
                    # doesn't remain PENDING and trigger duplicate reprocessing
                    # on crash recovery.
                    if self._message_queue:
                        try:
                            await self._message_queue.complete(msg.message_id)
                        except Exception:
                            log.warning(
                                "Failed to complete queue entry for timed-out message %s",
                                msg.message_id,
                                extra={"chat_id": msg.chat_id},
                            )
                    return None
                except asyncio.CancelledError:
                    log.info(
                        "Message %s cancelled in chat %s (shutdown or timeout)",
                        msg.message_id,
                        msg.chat_id,
                        extra={
                            "chat_id": msg.chat_id,
                            "message_id": msg.message_id,
                            "correlation_id": correlation_id,
                        },
                    )
                    raise
                except Exception as exc:
                    processing_time = _elapsed()
                    record_exception_safe(proc_span, exc)
                    log.error(
                        "Message processing failed for %s after %.2fs: %s",
                        msg.message_id,
                        processing_time,
                        exc,
                        exc_info=True,
                        extra={
                            "chat_id": msg.chat_id,
                            "message_id": msg.message_id,
                            "correlation_id": correlation_id,
                        },
                    )

                    if not routing_show_errors_var.get():
                        log.info(
                            "Error suppressed (showErrors=false) for message %s",
                            msg.message_id,
                        )
                        return None

                    raise
                finally:
                    routing_show_errors_var.set(True)  # reset to default
                    clear_correlation_id()

    # ── scheduled task processing ──────────────────────────────────────────

    async def process_scheduled(
        self,
        chat_id: str,
        prompt: str,
        channel: "BaseChannel | None" = None,
        prompt_hmac: str | None = None,
    ) -> str | None:
        """Process a scheduled task prompt directly, bypassing routing and dedup."""
        _validate_chat_id(chat_id)
        correlation_id = set_correlation_id(f"sched_{chat_id}_{uuid.uuid4().hex[:8]}")

        # Verify prompt integrity when HMAC signing is configured.
        secret = get_scheduler_secret()
        if secret:
            if prompt_hmac is None:
                log.warning(
                    "Scheduled task for chat %s received without HMAC "
                    "(signing secret is configured)",
                    chat_id,
                    extra={"chat_id": chat_id},
                )
                audit_log(
                    "scheduled_prompt_missing_hmac",
                    {"chat_id": chat_id},
                )
            elif not verify_payload(secret, prompt.encode("utf-8"), prompt_hmac):
                log.error(
                    "Scheduled task for chat %s rejected: prompt HMAC "
                    "verification failed — possible tampering",
                    chat_id,
                    extra={"chat_id": chat_id},
                )
                audit_log(
                    "scheduled_prompt_hmac_failure",
                    {"chat_id": chat_id},
                )
                return None

        log.info(
            "Processing scheduled task for chat %s",
            chat_id,
            extra={"chat_id": chat_id},
        )

        try:
            await get_event_bus().emit(
                Event(
                    name="scheduled_task_started",
                    data={"chat_id": chat_id, "prompt_length": len(prompt)},
                    source="Bot.process_scheduled",
                    correlation_id=get_correlation_id(),
                )
            )
        except Exception:
            log_noncritical(
                NonCriticalCategory.EVENT_EMISSION,
                f"Failed to emit scheduled_task_started event for chat {chat_id}",
                logger=log,
            )

        async with self._chat_locks.acquire(chat_id):
            try:
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

                channel_prompt = channel.get_channel_prompt() if channel else None

                try:
                    result = await self._context_assembler.assemble(
                        chat_id=chat_id,
                        channel_prompt=channel_prompt,
                    )
                except Exception as exc:
                    log.warning(
                        "Scheduled task for chat %s aborted: context assembly failed: %s",
                        chat_id,
                        exc,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                if result is None:
                    log.warning(
                        "Scheduled task for chat %s aborted: context assembly "
                        "returned None (build_context failure)",
                        chat_id,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

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

                tools = self._skills.tool_definitions
                response_text, _, _ = await self._react_loop(
                    chat_id=chat_id,
                    messages=[m.to_api_dict() for m in messages],
                    tools=tools if tools else None,
                    workspace_dir=workspace_dir,
                    channel=channel,
                )

                if response_text and any(
                    response_text.startswith(prefix) for prefix in SCHEDULED_ERROR_PREFIXES
                ):
                    log.warning(
                        "Scheduled task for chat %s produced an error response, "
                        "skipping persistence: %.80s",
                        chat_id,
                        response_text,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                if response_text is None:
                    log.warning(
                        "Scheduled task for chat %s produced None response, skipping persistence",
                        chat_id,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    return None

                response_text = self._context_assembler.finalize_turn(chat_id, response_text)

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

                await self._db.upsert_chat(chat_id, "Scheduler")
                _ids = await self._db.save_messages_batch(
                    chat_id=chat_id,
                    messages=[
                        {
                            "role": "user",
                            "content": safe_prompt,
                            "name": "Scheduler",
                            "message_id": f"sched_{uuid.uuid4().hex[:8]}",
                        },
                        {"role": "assistant", "content": response_text},
                    ],
                )

                log.info(
                    "Scheduled task for chat %s completed successfully",
                    chat_id,
                    extra={"chat_id": chat_id},
                )

                try:
                    await get_event_bus().emit(
                        Event(
                            name="scheduled_task_completed",
                            data={
                                "chat_id": chat_id,
                                "response_length": len(response_text) if response_text else 0,
                            },
                            source="Bot.process_scheduled",
                            correlation_id=get_correlation_id(),
                        )
                    )
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.EVENT_EMISSION,
                        f"Failed to emit scheduled_task_completed event for chat {chat_id}",
                        logger=log,
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

        Delegates to :func:`src.bot.context_building.build_turn_context`.
        """
        if not self._routing:
            log.warning("No routing engine configured, skipping message")
            await get_event_bus().emit(
                Event(
                    name="message_dropped",
                    data={
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "no_routing",
                    },
                    source="Bot._build_turn_context",
                    correlation_id=get_correlation_id(),
                )
            )
            return None

        return await _build_turn_context(
            msg,
            routing=self._routing,
            instruction_loader=self._instruction_loader,
            context_assembler=self._context_assembler,
            channel=channel,
        )

    async def _prepare_turn(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
    ) -> _PreparedTurn | None:
        """Persist user message, seed workspace, and build routing context.

        Performs the turn-preparation steps that run before the ReAct loop:

        1. Emit ``message_received`` event
        2. Upsert chat metadata + persist user message (batched)
        3. Ensure workspace directory exists
        4. Build turn context (routing match + context assembly)

        Returns ``None`` when routing produces no match (message dropped).
        """
        await get_event_bus().emit(
            Event(
                name="message_received",
                data={"chat_id": msg.chat_id, "sender": msg.sender_name},
                source="Bot._prepare_turn",
                correlation_id=get_correlation_id(),
            )
        )

        try:
            await self._db.upsert_chat_and_save_message(
                chat_id=msg.chat_id,
                sender_name=msg.sender_name,
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

        workspace_dir = self._memory.ensure_workspace(msg.chat_id)

        ctx = await self._build_turn_context(msg, channel)
        if not ctx:
            return None

        return _PreparedTurn(ctx=ctx, workspace_dir=workspace_dir)

    async def _process(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
        generation: int = 0,
    ) -> str | None:
        """Orchestrate a single message turn: prepare → react → deliver."""
        prepared = await self._prepare_turn(msg, channel)
        if not prepared:
            return None

        ctx = prepared.ctx
        tools = self._skills.tool_definitions
        verbose = ctx.skill_exec_verbose
        stream_cb = stream_callback if verbose == "full" else None
        raw_response, tool_log, buffered_persist = await self._react_loop(
            chat_id=msg.chat_id,
            messages=[m.to_api_dict() for m in ctx.messages],
            tools=tools if tools else None,
            workspace_dir=prepared.workspace_dir,
            stream_callback=stream_cb,
            channel=channel,
        )

        return await self._deliver_response(
            chat_id=msg.chat_id,
            raw_response=raw_response,
            tool_log=tool_log,
            buffered_persist=buffered_persist,
            generation=generation,
            verbose=verbose,
            channel=channel,
        )

    async def _deliver_response(
        self,
        chat_id: str,
        raw_response: str,
        tool_log: list[ToolLogEntry],
        buffered_persist: list[dict[str, Any]],
        generation: int,
        verbose: str,
        channel: "BaseChannel | None" = None,
    ) -> str | None:
        """Post-ReAct response delivery: format, dedup, persist, emit.

        Delegates to :func:`src.bot.response_delivery.deliver_response`.
        """
        return await _deliver_response(
            chat_id,
            raw_response,
            tool_log,
            buffered_persist,
            generation,
            verbose,
            context_assembler=self._context_assembler,
            db=self._db,
            dedup=self._dedup,
            channel=channel,
        )

    # ── ReAct loop (delegation to react_loop.py) ──────────────────────────

    async def _react_loop(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: "BaseChannel | None" = None,
    ) -> tuple[str, list[ToolLogEntry], list[dict[str, Any]]]:
        """Delegate to :func:`src.bot.react_loop.react_loop`."""
        return await _react_loop(
            llm=self._llm,
            metrics=self._metrics,
            tool_executor=self._tool_executor,
            chat_id=chat_id,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            workspace_dir=workspace_dir,
            max_tool_iterations=self._cfg.max_tool_iterations,
            stream_response=self._cfg.stream_response,
            max_retries=REACT_LOOP_MAX_RETRIES,
            initial_delay=REACT_LOOP_RETRY_INITIAL_DELAY,
            retryable_codes=_RETRYABLE_LLM_ERROR_CODES,
            stream_callback=stream_callback,
            channel=channel,
            react_loop_timeout=self._cfg.react_loop_timeout,
        )

    # ── config hot-reload ──────────────────────────────────────────────────

    def update_config(self, new_cfg: BotConfig) -> None:
        """Update the bot config with validation.

        Validates *new_cfg* (positive ``max_tool_iterations``, non-negative
        ``memory_max_history``) and replaces the internal config reference.
        Propagates the change to the :class:`ContextAssembler` so subsequent
        message processing picks up the new values immediately.

        Called by :class:`ConfigChangeApplier` during hot-reload — **do not**
        use ``object.__setattr__`` to mutate ``_cfg`` directly.
        """
        if not isinstance(new_cfg, BotConfig):
            raise TypeError(f"Expected BotConfig, got {type(new_cfg).__name__}")
        if new_cfg.max_tool_iterations <= 0:
            raise ValueError(
                f"max_tool_iterations must be positive, got {new_cfg.max_tool_iterations}"
            )
        if new_cfg.memory_max_history < 0:
            raise ValueError(
                f"memory_max_history must be non-negative, got {new_cfg.memory_max_history}"
            )
        old_cfg = self._cfg
        self._cfg = new_cfg
        # Propagate to ContextAssembler so context assembly uses updated values
        self._context_assembler.update_config(new_cfg)
        log.info(
            "Bot config updated: max_tool_iterations=%d → %d, memory_max_history=%d → %d",
            old_cfg.max_tool_iterations,
            new_cfg.max_tool_iterations,
            old_cfg.memory_max_history,
            new_cfg.memory_max_history,
        )
