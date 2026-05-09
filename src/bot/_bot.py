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

Dependency Injection
--------------------
``Bot`` receives **all** collaborators through the ``BotDeps`` dataclass,
never constructs them internally.  This design keeps ``Bot`` agnostic of
how its dependencies are built, enabling full mock-based unit testing.

**Production path** — :func:`src.builder.build_bot` wires everything::

    from src.builder import build_bot

    components: BotComponents = await build_bot(config, session_metrics=metrics)
    bot: Bot = components.bot          # fully wired, ready to use

The builder creates ``BotConfig``, ``RateLimiter``, ``ToolExecutor``,
``ContextAssembler``, and all other required collaborators, then packs
them into ``BotDeps`` before calling ``Bot(deps)``.

**Test path** — construct ``BotDeps`` manually with mocks::

    from unittest.mock import AsyncMock, MagicMock
    from src.bot import Bot, BotConfig, BotDeps

    bot = Bot(BotDeps(
        config=BotConfig(
            max_tool_iterations=10,
            memory_max_history=50,
            system_prompt_prefix="",
        ),
        db=AsyncMock(),
        llm=AsyncMock(),
        memory=AsyncMock(),
        skills=MagicMock(),
        rate_limiter=MagicMock(),          # injected collaborator
        tool_executor=AsyncMock(),         # injected collaborator
        context_assembler=AsyncMock(),     # injected collaborator
    ))

Only the seven required fields (``config``, ``db``, ``llm``, ``memory``,
``skills``, ``rate_limiter``, ``tool_executor``, ``context_assembler``)
must be provided.  All other fields (``routing``, ``dedup``,
``message_queue``, etc.) default to ``None`` and ``Bot.__init__``
supplies sensible fallbacks.

See :class:`BotDeps` field comments for per-field details and
:mod:`src.builder` for the full production wiring sequence.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Coroutine, TypeVar

_T = TypeVar("_T")

from src.channels.base import IncomingMessage
from src.channels.base import is_group_chat as _is_group_chat
from src.constants import (
    DEFAULT_CHAT_LOCK_CACHE_SIZE,
    DEFAULT_CHAT_RATE_LIMIT,
    ERROR_REPLY_RATE_LIMIT,
    ERROR_REPLY_WINDOW_SECONDS,
    MAX_MESSAGE_LENGTH,
    MAX_RATE_LIMIT_TRACKED_CHATS,
    MEMORY_CHECK_INTERVAL_SECONDS,
    MEMORY_CRITICAL_THRESHOLD_PERCENT,
    MEMORY_WARNING_THRESHOLD_PERCENT,
    RATE_LIMIT_WINDOW_SECONDS,
    REACT_LOOP_MAX_RETRIES,
    REACT_LOOP_RETRY_INITIAL_DELAY,
    SCHEDULED_ERROR_PREFIXES,
)
from src.core.context_builder import ChatMessage
from src.core.dedup import NullDedupService
from src.db.db import ChatMessageParams
from src.bot._event_helpers import _emit_error_event_safe, _emit_event_safe
from src.core.instruction_loader import InstructionLoader
from src.core.project_context import ProjectContextLoader
from src.utils.validation import _validate_chat_id
from src.llm._error_classifier import RETRYABLE_LLM_ERROR_CODES as _RETRYABLE_LLM_ERROR_CODES
from src.logging import correlation_id_scope, get_correlation_id
from src.monitoring import NullMemoryMonitor, get_metrics_collector
from src.monitoring.tracing import (
    add_span_event,
    get_tracer,
    record_exception_safe,
    set_correlation_id_on_span,
)
from src.rate_limiter import SlidingWindowTracker
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
from src.constants.security import INJECTION_BLOCK_CONFIDENCE
from src.utils import LRULockCache
from src.utils.timing import elapsed as _elapsed, set_timer_start as _set_timer_start

from src.bot.context_building import (
    TurnContext,
    build_turn_context as _build_turn_context,
    routing_show_errors_var,
)
from src.bot.crash_recovery import recover_pending_messages as _recover_pending_messages
from src.bot.preflight import PreflightResult, preflight_check as _preflight_check
from src.bot.react_loop import (
    react_loop as _react_loop,
)
from src.bot.response_delivery import (
    deliver_response as _deliver_response,
    send_to_chat as _send_to_chat,
)
from src.bot.turn_orchestrator import (
    DeliveryRequest as _DeliveryRequest,
    PreparedTurn as _PreparedTurn,
    TurnOrchestrator as _TurnOrchestrator,
)

if TYPE_CHECKING:
    from src.bot.react_loop import StreamCallback
    from src.core.context_assembler import ContextAssembler
    from src.core.tool_executor import ToolExecutor
    from src.core.tool_formatter import ToolLogEntry
    from src.bot.preflight import PreflightResult
    from src.db import Database
    from src.monitoring import PerformanceMetrics, SessionMetrics
    from src.core.dedup import DeduplicationService as _DeduplicationService
    from src.message_queue import MessageQueue
    from src.rate_limiter import RateLimiter
    from src.skills import SkillRegistry
    from src.utils.protocols import (
        LockProvider,
        MemoryMonitor,
        MemoryProtocol,
        ProjectStore,
    )
    from src.channels.base import BaseChannel, SendMediaCallback
    from src.llm import LLMProvider
    from src.routing import RoutingEngine


log = logging.getLogger(__name__)

lifecycle_log = logging.getLogger("lifecycle.bot")

__all__ = ["Bot", "BotConfig", "BotDeps", "TurnContext"]



@dataclass(slots=True, frozen=True)
class _ReactLoopParams:
    """Parameter bag for :meth:`Bot._react_loop`.

    Groups the 6 positional arguments into a single immutable dataclass,
    keeping the call-site readable and satisfying PLR0913.
    """

    chat_id: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    workspace_dir: Path
    stream_callback: StreamCallback | None = None
    channel: BaseChannel | None = None


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
    parallel_tool_execution: bool = True


@dataclass(slots=True)
class BotDeps:
    """Structured parameter bag for ``Bot.__init__``.

    Replaces the former 15-parameter constructor signature with a single
    dataclass, mirroring ``ShutdownContext`` from ``src/lifecycle.py``.

    Required Fields
    ~~~~~~~~~~~~~~~
    These are always available in production and must be provided for
    every ``Bot`` instantiation — including tests.

    ``config`` (:class:`BotConfig`)
        Tuning knobs read from the full application config.  Controls
        iteration limits, timeouts, and streaming behaviour.

    ``db`` (:class:`Database`)
        Async persistence layer for chat metadata and message history.

    ``llm`` (:class:`LLMProvider`)
        Async LLM client used by the ReAct loop for completions.

    ``memory`` (:class:`MemoryProtocol`)
        Per-chat memory manager (workspace directories, context window).

    ``skills`` (:class:`SkillRegistry`)
        Registry of available tools/skills exposed to the LLM.

    ``rate_limiter`` (:class:`RateLimiter`)
        Per-chat sliding-window rate limiter — constructed in
        :mod:`src.builder` and injected for testability.

    ``tool_executor`` (:class:`ToolExecutor`)
        Executes tool calls produced by the ReAct loop — constructed in
        :mod:`src.builder` and injected for testability.

    ``context_assembler`` (:class:`ContextAssembler`)
        Assembles the full LLM message payload (system prompt, history,
        channel prompt) — constructed in :mod:`src.builder` and injected
        for testability.

    Optional Fields
    ~~~~~~~~~~~~~~~
    Default to ``None`` or empty values.  ``Bot.__init__`` supplies
    sensible fallbacks when these are omitted.

    ``routing`` (:class:`RoutingEngine` | ``None``, default ``None``)
        Routing engine for instruction matching.  When ``None``, Bot
        logs a warning and drops all messages in ``_build_turn_context``.

    ``instructions_dir`` (``str``, default ``""``)
        Filesystem path to instruction YAML files.  Passed to
        :class:`InstructionLoader` when no ``instruction_loader`` is
        provided.

    ``message_queue`` (:class:`MessageQueue` | ``None``, default ``None``)
        Persistent queue for crash-recovery.  When ``None``, crash
        recovery is skipped.

    ``project_store`` (:class:`ProjectStore` | ``None``, default ``None``)
        Project-specific data store.  When ``None``, project context
        features are disabled.

    ``project_ctx`` (:class:`ProjectContextLoader` | ``None``, default ``None``)
        Pre-built project context loader.  Falls back to
        ``ProjectContextLoader(project_store)`` when ``None``.

    ``session_metrics`` (:class:`SessionMetrics` | ``None``, default ``None``)
        Per-session metric collector.  When ``None``, the global
        singleton from :func:`get_metrics_collector` is used.

    ``instruction_loader`` (:class:`InstructionLoader` | ``None``, default ``None``)
        Shared instruction loader instance.  Falls back to
        ``InstructionLoader(instructions_dir)`` when ``None``.

    ``chat_locks`` (:class:`LockProvider` | ``None``, default ``None``)
        Per-chat asyncio lock provider.  Falls back to
        ``LRULockCache`` with default capacity when ``None``.

    ``dedup`` (:class:`DeduplicationService` | ``None``, default ``None``)
        Inbound + outbound deduplication service.  Falls back to
        :class:`NullDedupService` (no-op) when ``None``.

    Construction Examples
    ~~~~~~~~~~~~~~~~~~~~~
    **Production** — use :func:`src.builder.build_bot`::

        from src.builder import build_bot
        components = await build_bot(config, session_metrics=metrics)
        bot = components.bot

    **Testing** — construct manually with mocks::

        from unittest.mock import AsyncMock, MagicMock
        from src.bot import Bot, BotConfig, BotDeps

        bot = Bot(BotDeps(
            config=BotConfig(max_tool_iterations=10, memory_max_history=50, system_prompt_prefix=""),
            db=AsyncMock(),
            llm=AsyncMock(),
            memory=AsyncMock(),
            skills=MagicMock(),
            rate_limiter=MagicMock(),
            tool_executor=AsyncMock(),
            context_assembler=AsyncMock(),
        ))

    **Extension Pattern** — to add a new dependency:

    1. Add the field to this dataclass with a ``None`` default.
    2. Add a fallback in ``Bot.__init__`` (or raise if truly required).
    3. Wire the field in :func:`src.builder.build_bot`.
    4. Update tests to provide a mock for the new field.
    """

    # Required
    config: BotConfig
    db: Database
    llm: LLMProvider
    memory: MemoryProtocol
    skills: SkillRegistry

    # Required — constructed in builder.py, injected for testability
    rate_limiter: "RateLimiter"
    tool_executor: "ToolExecutor"
    context_assembler: "ContextAssembler"

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
        # (content-hash) strategies.  Defaults to NullDedupService so Bot
        # always has a non-None collaborator (eliminates scattered None-checks).
        self._dedup: _DeduplicationService = (
            deps.dedup if deps.dedup is not None else NullDedupService()
        )
        # All three collaborators are now constructed in builder.py and injected
        # via BotDeps — no fallback construction in Bot.
        self._rate_limiter = deps.rate_limiter
        self._tool_executor = deps.tool_executor
        self._context_assembler = deps.context_assembler
        # Memory monitor for tracking resource usage
        self._memory_monitor: MemoryMonitor = NullMemoryMonitor()
        # Performance metrics collector (singleton — same instance used by ToolExecutor)
        self._metrics: PerformanceMetrics = get_metrics_collector()
        # Instruction file loader — prefer injected shared instance
        self._instruction_loader = deps.instruction_loader or InstructionLoader(
            self._instructions_dir
        )
        # Project context loader — prefer injected shared instance
        self._project_ctx = deps.project_ctx or ProjectContextLoader(deps.project_store)

        # Turn orchestrator: encapsulates turn-level preparation, ReAct, delivery
        self._turn_orchestrator = _TurnOrchestrator(
            db=self._db,
            memory=self._memory,
            skills=self._skills,
            context_assembler=self._context_assembler,
            dedup=self._dedup,
            max_tool_iterations=self._cfg.max_tool_iterations,
            stream_response=self._cfg.stream_response,
        )

        # Error-reply rate limiter: per-chat sliding window to prevent
        # amplification attacks via error message flooding.
        self._error_reply_trackers: OrderedDict[str, SlidingWindowTracker] = OrderedDict()

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
            dedup=self._dedup,
        )

    # ── preflight ─────────────────────────────────────────────────────────────

    async def preflight_check(self, msg: IncomingMessage) -> PreflightResult:
        """Run read-only filter checks before expensive processing.

        Delegates to :func:`src.bot.preflight.preflight_check`.
        """
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

        with correlation_id_scope(msg.correlation_id) as correlation_id:
            if not msg.acl_passed:
                log.warning(
                    "Rejecting message %s from %s in chat %s — ACL not passed. "
                    "Messages must go through a channel that enforces access control.",
                    msg.message_id,
                    msg.sender_id,
                    msg.chat_id,
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
                await _emit_event_safe(
                    "message_dropped",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "acl_rejected",
                    },
                    "Bot.handle_message",
                    correlation_id,
                )
                audit_log(
                    "acl_rejected",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "message_id": msg.message_id,
                    },
                )
                self._metrics.track_message_rejected("acl_rejected")
                return None

            if not msg.text or not msg.text.strip():
                log.debug(
                    "Empty message from %s in chat %s, skipping",
                    msg.sender_name,
                    msg.chat_id,
                    extra={"chat_id": msg.chat_id},
                )
                self._metrics.track_message_rejected("empty_text")
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
                await _emit_event_safe(
                    "message_dropped",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "message_too_long",
                        "message_length": len(msg.text),
                    },
                    "Bot.handle_message",
                    correlation_id,
                )
                self._metrics.track_message_rejected("message_too_long")
                return None

            log.info(
                "Processing message %s from %s in chat %s",
                msg.message_id,
                msg.sender_name,
                msg.chat_id,
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )

            if await self._dedup.is_inbound_duplicate(msg.message_id):
                log.debug(
                    "Duplicate message %s from chat %s, skipping",
                    msg.message_id,
                    msg.chat_id,
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
                self._metrics.track_message_rejected("inbound_duplicate")
                return None

            rate_result = self._rate_limiter.check_message_rate(
                msg.chat_id,
                limit=DEFAULT_CHAT_RATE_LIMIT,
                window_seconds=int(RATE_LIMIT_WINDOW_SECONDS),
            )
            if not rate_result.allowed:
                is_group = _is_group_chat(msg.chat_id)
                if is_group:
                    log.debug(
                        "Rate-limit response suppressed for group chat %s",
                        msg.chat_id,
                        extra={"chat_id": msg.chat_id},
                    )
                else:
                    log.warning(
                        "Message rate limit exceeded for chat %s (%d messages/min)",
                        msg.chat_id,
                        rate_result.limit_value,
                        extra={"chat_id": msg.chat_id, "rate_limit": rate_result.limit_value},
                    )
                    await self._send_error_reply(
                        msg.chat_id,
                        "⚠️ You're sending messages too quickly. Please wait a moment.",
                        channel=channel,
                    )
                await _emit_event_safe(
                    "message_dropped",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "rate_limited",
                        "limit_type": rate_result.limit_type,
                        "limit_value": rate_result.limit_value,
                    },
                    "Bot.handle_message",
                    correlation_id,
                )
                self._metrics.track_message_rejected("rate_limited")
                return None

            # Injection detection: block high-confidence attempts before
            # expensive processing (persistence, context assembly, LLM call).
            injection_result = detect_injection(msg.text)
            if injection_result.detected and injection_result.confidence >= INJECTION_BLOCK_CONFIDENCE:
                log.warning(
                    "Message %s from %s in chat %s blocked: high-confidence "
                    "injection detected (confidence=%.1f, patterns=%s)",
                    msg.message_id,
                    msg.sender_id,
                    msg.chat_id,
                    injection_result.confidence,
                    injection_result.matched_patterns,
                    extra={
                        "chat_id": msg.chat_id,
                        "injection_patterns": injection_result.matched_patterns,
                        "confidence": injection_result.confidence,
                    },
                )
                audit_log(
                    "injection_blocked",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "confidence": injection_result.confidence,
                        "patterns": injection_result.matched_patterns,
                    },
                )
                await _emit_event_safe(
                    "message_dropped",
                    {
                        "chat_id": msg.chat_id,
                        "sender_id": msg.sender_id,
                        "reason": "injection_blocked",
                        "confidence": injection_result.confidence,
                        "patterns": injection_result.matched_patterns,
                    },
                    "Bot.handle_message",
                    correlation_id,
                )
                self._metrics.track_message_rejected("injection_blocked")
                return None

            return await self._handle_message_inner(
                msg, channel=channel, stream_callback=stream_callback, correlation_id=correlation_id
            )

    async def _send_error_reply(
        self,
        chat_id: str,
        text: str,
        channel: "BaseChannel | None",
    ) -> None:
        """Send an error reply with per-chat sliding window rate limiting.

        Prevents error-message amplification attacks by capping error replies
        per chat within a sliding window. When the limit is exceeded, the
        error reply is silently dropped with a warning log.
        """
        tracker = self._error_reply_trackers.get(chat_id)
        if tracker is None:
            tracker = SlidingWindowTracker(
                window_size_seconds=ERROR_REPLY_WINDOW_SECONDS,
                max_limit=ERROR_REPLY_RATE_LIMIT,
            )
            if len(self._error_reply_trackers) >= MAX_RATE_LIMIT_TRACKED_CHATS:
                self._error_reply_trackers.popitem(last=False)
            self._error_reply_trackers[chat_id] = tracker
        else:
            self._error_reply_trackers.move_to_end(chat_id)

        allowed, _, _ = tracker.check_only()
        if not allowed:
            log.warning(
                "Error reply rate limit exceeded for chat %s, silently dropping",
                chat_id,
                extra={"chat_id": chat_id},
            )
            return

        tracker.record()
        await _send_to_chat(chat_id, text, dedup=self._dedup, channel=channel)

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
                add_span_event(
                    proc_span,
                    "message_received",
                    {"messaging.message.id": msg.message_id},
                )
                generation = self._db.get_generation(msg.chat_id)

                # Request dedup: short-window content-hash check within the
                # per-chat lock scope.  Catches double-sends with slightly
                # different message_ids and scheduled-vs-manual collisions
                # that inbound (message-id) dedup cannot detect.
                if msg.text:
                    if self._dedup.check_and_record_request(msg.chat_id, msg.text):
                        log.info(
                            "Request dedup: skipping duplicate request in chat %s "
                            "(message %s, content hash matched within TTL)",
                            msg.chat_id,
                            msg.message_id,
                            extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                        )
                        return None

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
                    self._metrics.track_message_latency(processing_time, chat_id=msg.chat_id)
                    self._metrics.track_chat_message(msg.chat_id)

                    if self._message_queue:
                        queue_depth = await self._message_queue.get_pending_count()
                        self._metrics.update_queue_depth(queue_depth)

                    self._metrics.update_active_chat_count(len(self._chat_locks))
                    proc_span.set_attribute(
                        "custombot.processing_time_ms",
                        round(processing_time * 1000, 2),
                    )
                    add_span_event(
                        proc_span,
                        "response_delivered",
                        {
                            "custombot.processing_time_ms": round(processing_time * 1000, 2),
                            "messaging.message.id": msg.message_id,
                        },
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
                    # Notify monitoring subscribers about the stuck turn.
                    await _emit_error_event_safe(
                        asyncio.TimeoutError(
                            f"per_chat_timeout={timeout}s exceeded for message "
                            f"{msg.message_id} in chat {msg.chat_id}"
                        ),
                        "Bot._handle_message_inner",
                        extra_data={
                            "chat_id": msg.chat_id,
                            "message_id": msg.message_id,
                            "timeout_seconds": timeout,
                        },
                        correlation_id=correlation_id,
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
                    # Best-effort: ensure queue entry is completed on ALL exit
                    # paths.  The success and TimeoutError branches already
                    # call complete() above; this catches CancelledError and
                    # unexpected Exception paths that would otherwise leave the
                    # entry PENDING, causing duplicate reprocessing after crash
                    # recovery.  complete() is idempotent — redundant calls are
                    # safe.
                    if self._message_queue:
                        try:
                            await self._message_queue.complete(msg.message_id)
                        except Exception:
                            log.warning(
                                "Failed to complete queue entry for message %s "
                                "in finally cleanup",
                                msg.message_id,
                                extra={"chat_id": msg.chat_id},
                            )
                    routing_show_errors_var.set(True)  # reset to default

    # ── scheduled task processing ──────────────────────────────────────────

    async def process_scheduled(
        self,
        chat_id: str,
        prompt: str,
        channel: "BaseChannel | None" = None,
        prompt_hmac: str | None = None,
    ) -> str | None:
        """Process a scheduled task prompt directly, bypassing routing and dedup.

        When SCHEDULER_HMAC_SECRET is set, the prompt_hmac parameter is
        mandatory and the task is rejected if missing or invalid.
        """
        _validate_chat_id(chat_id)

        with correlation_id_scope(f"sched_{chat_id}_{uuid.uuid4().hex[:8]}") as correlation_id:
            start = time.monotonic()

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
                    return None
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
                    await _emit_event_safe(
                        "error_occurred",
                        {
                            "error_type": "hmac_verification_failure",
                            "chat_id": chat_id,
                        },
                        "Bot.process_scheduled",
                        correlation_id,
                    )
                    return None

            log.info(
                "Processing scheduled task for chat %s",
                chat_id,
                extra={"chat_id": chat_id},
            )

            await _emit_event_safe(
                "scheduled_task_started",
                {"chat_id": chat_id, "prompt_length": len(prompt)},
                "Bot.process_scheduled",
                correlation_id,
            )

            async with self._chat_locks.acquire(chat_id):
                # Request dedup: prevent a scheduled task from re-processing
                # the same prompt that a user message just handled (or vice
                # versa) within the short TTL window.
                if prompt:
                    if self._dedup.check_and_record_request(chat_id, prompt):
                        log.info(
                            "Request dedup: skipping duplicate scheduled task in chat %s "
                            "(content hash matched within TTL)",
                            chat_id,
                            extra={"chat_id": chat_id},
                        )
                        return None

                timeout = self._cfg.per_chat_timeout

                try:
                    result = await self._run_with_timeout(
                        self._process_scheduled_turn(
                            chat_id=chat_id,
                            prompt=prompt,
                            channel=channel,
                            correlation_id=correlation_id,
                        ),
                        chat_id=chat_id,
                        source="Bot.process_scheduled",
                        correlation_id=correlation_id,
                    )
                    self._metrics.track_scheduled_task_latency(time.monotonic() - start)
                    return result

                except Exception as exc:
                    log.error(
                        "Scheduled task failed for chat %s: %s",
                        chat_id,
                        exc,
                        exc_info=True,
                        extra={"chat_id": chat_id, "correlation_id": correlation_id},
                    )
                    await _emit_event_safe(
                        "scheduled_task_failed",
                        {
                            "chat_id": chat_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:200],
                        },
                        "Bot.process_scheduled",
                        correlation_id,
                    )
                    return None

    # ── timeout helper ────────────────────────────────────────────────────

    async def _run_with_timeout(
        self,
        coro: Coroutine[Any, Any, _T],
        chat_id: str,
        source: str,
        correlation_id: str | None = None,
    ) -> _T | None:
        """Run *coro* with ``per_chat_timeout``; emit error event on timeout."""
        timeout = self._cfg.per_chat_timeout
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except asyncio.TimeoutError:
            log.error(
                "%s TIMED OUT after %.1fs (per_chat_timeout=%.1fs) "
                "in chat %s",
                source,
                timeout,
                timeout,
                chat_id,
                extra={
                    "chat_id": chat_id,
                    "correlation_id": correlation_id,
                    "timeout_seconds": timeout,
                },
            )
            await _emit_error_event_safe(
                asyncio.TimeoutError(
                    f"per_chat_timeout={timeout}s exceeded for "
                    f"{source} in chat {chat_id}"
                ),
                source,
                extra_data={
                    "chat_id": chat_id,
                    "timeout_seconds": timeout,
                },
                correlation_id=correlation_id,
            )
            return None

    # ── scheduled task helpers ─────────────────────────────────────────────

    async def _process_scheduled_turn(
        self,
        chat_id: str,
        prompt: str,
        channel: "BaseChannel | None",
        correlation_id: str,
    ) -> str | None:
        """Execute scheduled task LLM processing and persistence.

        Extracted from :meth:`process_scheduled` so the caller can wrap
        the turn in :func:`asyncio.wait_for` with ``per_chat_timeout``.
        """
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
            if injection_result.confidence >= INJECTION_BLOCK_CONFIDENCE:
                log.error(
                    "Scheduled task for chat %s blocked: high-confidence "
                    "injection detected (confidence=%.1f, patterns=%s) — "
                    "rejecting prompt",
                    chat_id,
                    injection_result.confidence,
                    injection_result.matched_patterns,
                    extra={
                        "chat_id": chat_id,
                        "injection_patterns": injection_result.matched_patterns,
                    },
                )
                audit_log(
                    "scheduled_prompt_injection_blocked",
                    {
                        "chat_id": chat_id,
                        "confidence": injection_result.confidence,
                        "patterns": injection_result.matched_patterns,
                    },
                )
                await _emit_event_safe(
                    "error_occurred",
                    {
                        "error_type": "injection_blocked",
                        "chat_id": chat_id,
                        "confidence": injection_result.confidence,
                        "patterns": injection_result.matched_patterns,
                    },
                    "Bot.process_scheduled",
                    correlation_id,
                )
                return None
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
            audit_log(
                "scheduled_prompt_injection_low_confidence",
                {
                    "chat_id": chat_id,
                    "confidence": injection_result.confidence,
                    "patterns": injection_result.matched_patterns,
                },
            )

        messages = result.messages
        messages.append(ChatMessage(role="user", content=safe_prompt))

        tools = self._skills.tool_definitions
        response_text, _, _ = await self._react_loop(
            _ReactLoopParams(
                chat_id=chat_id,
                messages=[m.to_api_dict() for m in messages],
                tools=tools if tools else None,
                workspace_dir=workspace_dir,
                channel=channel,
            )
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

        await _emit_event_safe(
            "scheduled_task_completed",
            {
                "chat_id": chat_id,
                "response_length": len(response_text) if response_text else 0,
            },
            "Bot.process_scheduled",
            correlation_id,
        )

        return response_text

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
            await _emit_event_safe(
                "message_dropped",
                {
                    "chat_id": msg.chat_id,
                    "sender_id": msg.sender_id,
                    "reason": "no_routing",
                },
                "Bot._build_turn_context",
                get_correlation_id(),
            )
            self._metrics.track_message_rejected("no_routing")
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

        Raises on user-message persistence failure — failing fast prevents
        the LLM call from proceeding with a conversation history gap that
        would be invisible on restart.
        """
        await _emit_event_safe(
            "message_received",
            {"chat_id": msg.chat_id, "sender": msg.sender_name},
            "Bot._prepare_turn",
            get_correlation_id(),
        )

        try:
            await self._db.upsert_chat_and_save_message(
                ChatMessageParams(
                    chat_id=msg.chat_id,
                    sender_name=msg.sender_name,
                    role="user",
                    content=msg.text,
                    name=msg.sender_name,
                    message_id=msg.message_id,
                )
            )
        except Exception as exc:
            log.error(
                "Failed to persist user turn for chat %s (message %s) — "
                "aborting LLM call: %s",
                msg.chat_id,
                msg.message_id,
                exc,
                exc_info=True,
                extra={
                    "chat_id": msg.chat_id,
                    "message_id": msg.message_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise

        workspace_dir = self._memory.ensure_workspace(msg.chat_id)

        ctx = await self._build_turn_context(msg, channel)
        if not ctx:
            return None

        return _PreparedTurn(
            ctx=ctx,
            workspace_dir=workspace_dir,
        )

    async def _process(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
        generation: int = 0,
    ) -> str | None:
        """Orchestrate a single message turn: prepare → react → deliver."""
        turn_ctx = await self._build_turn_context(msg, channel)
        workspace_dir = self._memory.ensure_workspace(msg.chat_id)

        prepared = await self._turn_orchestrator.prepare_turn(
            msg, turn_ctx, workspace_dir,
        )
        if not prepared:
            return None

        ctx = prepared.ctx
        tools = self._skills.tool_definitions
        verbose = ctx.skill_exec_verbose
        raw_response, tool_log, buffered_persist = (
            await self._turn_orchestrator.run_react_loop(
                self._react_loop,
                chat_id=msg.chat_id,
                messages=[m.to_api_dict() for m in ctx.messages],
                tools=tools if tools else None,
                workspace_dir=prepared.workspace_dir,
                stream_callback=stream_callback,
                channel=channel,
                verbose=verbose,
            )
        )

        return await self._turn_orchestrator.deliver_response(
            _DeliveryRequest(
                chat_id=msg.chat_id,
                raw_response=raw_response,
                tool_log=tool_log,
                buffered_persist=buffered_persist,
                generation=generation,
                verbose=verbose,
                channel=channel,
                persistence_failed=prepared.persistence_failed,
            )
        )

    async def _deliver_response(self, req: _DeliveryRequest) -> str | None:
        """Post-ReAct response delivery: format, dedup, persist, emit.

        Delegates to :func:`src.bot.response_delivery.deliver_response`.
        """
        return await _deliver_response(
            req.chat_id,
            req.raw_response,
            req.tool_log,
            req.buffered_persist,
            req.generation,
            req.verbose,
            context_assembler=self._context_assembler,
            db=self._db,
            dedup=self._dedup,
            channel=req.channel,
            persistence_failed=req.persistence_failed,
        )

    # ── ReAct loop (delegation to react_loop.py) ──────────────────────────

    async def _react_loop(
        self,
        params: _ReactLoopParams,
    ) -> tuple[str, list[ToolLogEntry], list[dict[str, Any]]]:
        """Delegate to :func:`src.bot.react_loop.react_loop`."""
        return await _react_loop(
            llm=self._llm,
            metrics=self._metrics,
            tool_executor=self._tool_executor,
            chat_id=params.chat_id,
            messages=params.messages,  # type: ignore[arg-type]
            tools=params.tools,  # type: ignore[arg-type]
            workspace_dir=params.workspace_dir,
            max_tool_iterations=self._cfg.max_tool_iterations,
            stream_response=self._cfg.stream_response,
            max_retries=REACT_LOOP_MAX_RETRIES,
            initial_delay=REACT_LOOP_RETRY_INITIAL_DELAY,
            retryable_codes=_RETRYABLE_LLM_ERROR_CODES,
            stream_callback=params.stream_callback,
            channel=params.channel,
            react_loop_timeout=self._cfg.react_loop_timeout,
            parallel_tool_execution=self._cfg.parallel_tool_execution,
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
        if new_cfg.per_chat_timeout <= 0:
            raise ValueError(
                f"per_chat_timeout must be positive, got {new_cfg.per_chat_timeout}"
            )
        if new_cfg.react_loop_timeout < 0:
            raise ValueError(
                f"react_loop_timeout must be non-negative, got {new_cfg.react_loop_timeout}"
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
