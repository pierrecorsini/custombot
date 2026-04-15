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

import json
import logging
import time
import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from src.channels.base import IncomingMessage
from src.config import Config

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
from src.constants import (
    MAX_LRU_CACHE_SIZE,
    MAX_MESSAGE_LENGTH,
    MEMORY_CHECK_INTERVAL_SECONDS,
    MEMORY_CRITICAL_THRESHOLD_PERCENT,
    MEMORY_WARNING_THRESHOLD_PERCENT,
    WORKSPACE_DIR,
)
from src.logging import set_correlation_id, clear_correlation_id
from src.rate_limiter import RateLimiter
from src.utils.type_guards import is_incoming_message
from src.utils import LRULockCache
from src.db import Database
from src.llm import LLMClient
from src.memory import Memory
from src.message_queue import MessageQueue
from src.routing import RoutingEngine
from src.monitoring import get_metrics_collector, PerformanceMetrics
from src.skills import SkillRegistry
from src.core.tool_executor import ToolExecutor
from src.core.context_builder import build_context
from src.core.tool_formatter import (
    format_response_with_tool_log,
    format_single_tool_execution,
)
from src.core.instruction_loader import InstructionLoader
from src.core.project_context import ProjectContextLoader
from src.core.topic_cache import TopicCache, parse_meta

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


class Bot:
    def __init__(
        self,
        config: Config,
        db: Database,
        llm: LLMClient,
        memory: Memory,
        skills: SkillRegistry,
        routing: RoutingEngine | None = None,
        instructions_dir: str = "",
        message_queue: MessageQueue | None = None,
        project_store: Any = None,
        project_ctx: Any = None,
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
        self._chat_locks = LRULockCache(max_size=MAX_LRU_CACHE_SIZE)
        # Rate limiter for skill execution
        self._rate_limiter = RateLimiter()
        # Per-chat message rate limiter
        self._chat_rate_limiter = RateLimiter()
        # Memory monitor for tracking resource usage
        self._memory_monitor: Any = None
        # Performance metrics collector
        self._metrics: PerformanceMetrics = get_metrics_collector()
        # Tool executor (delegates to skill registry with rate limiting and error handling)
        self._tool_executor = ToolExecutor(
            skills_registry=skills,
            rate_limiter=self._rate_limiter,
            metrics=self._metrics,
        )
        # Instruction file loader (mtime-cached)
        self._instruction_loader = InstructionLoader(self._instructions_dir)
        # Project context loader — prefer injected shared instance
        self._project_ctx = project_ctx or ProjectContextLoader(project_store)
        # Per-chat topic summary cache
        self._topic_cache = TopicCache(WORKSPACE_DIR)

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

    async def _get_project_context(self, chat_id: str) -> str | None:
        return await self._project_ctx.get(chat_id)

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
            self._memory_monitor.register_cache(
                "chat_locks", lambda: len(self._chat_locks._cache)
            )
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

    # ── crash recovery ────────────────────────────────────────────────────────

    async def recover_pending_messages(
        self, timeout_seconds: int | None = None
    ) -> dict:
        """
        Recover and process stale pending messages from previous crash.

        Should be called during bot startup to handle messages that were
        interrupted by a crash or restart.

        Args:
            timeout_seconds: Custom timeout for stale detection (uses queue default if not provided).

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
        for queued_msg in stale_messages:
            try:
                # Reconstruct IncomingMessage from queued data
                from src.channels.base import IncomingMessage as IM

                recovered_msg = IM(
                    message_id=queued_msg.message_id,
                    chat_id=queued_msg.chat_id,
                    text=queued_msg.text,
                    sender_name=queued_msg.sender_name,
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
            "Recovery complete: %d/%d messages recovered, %d failed",
            recovered_count,
            len(stale_messages),
            len(failures),
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
        if not is_incoming_message(msg):
            return PreflightResult(passed=False, reason="invalid")

        if not msg.text or not msg.text.strip():
            return PreflightResult(passed=False, reason="empty")

        if await self._db.message_exists(msg.message_id):
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
        Returns None if the message was a duplicate or filtered.

        Args:
            msg: The incoming message to process.
            channel: Optional channel instance for getting channel-specific prompts.
            stream_callback: Optional async callback for streaming tool executions in real-time.
        """
        # Set correlation ID for request tracing (use custom ID if provided)
        correlation_id = set_correlation_id(msg.correlation_id)

        # Runtime validation for incoming message
        if not is_incoming_message(msg):
            log.warning("Invalid incoming message received: %r", msg)
            clear_correlation_id()
            return None

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

        # Deduplicate
        if await self._db.message_exists(msg.message_id):
            log.debug("Duplicate message %s, skipping.", msg.message_id)
            clear_correlation_id()
            return None

        # Check per-chat message rate limit (30 messages per minute)
        rate_result = self._chat_rate_limiter.check_message_rate(
            msg.chat_id, limit=30, window_seconds=60
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

        # Get or create per-chat lock (bounded LRU cache)
        chat_lock = await self._chat_locks.get_or_create(msg.chat_id)
        async with chat_lock:
            # Track message processing time
            start_time = time.perf_counter()

            # Enqueue message for crash recovery (before processing)
            if self._message_queue:
                await self._message_queue.enqueue(msg)

            # Reset per-request routing flag
            _routing_show_errors_var.set(True)

            try:
                result = await self._process(
                    msg, channel=channel, stream_callback=stream_callback
                )
                # Mark message as completed after successful processing
                if self._message_queue:
                    await self._message_queue.complete(msg.message_id)

                # Track message processing latency
                processing_time = time.perf_counter() - start_time
                self._metrics.track_message_latency(processing_time)

                # Update queue depth if available
                if self._message_queue:
                    queue_depth = await self._message_queue.get_pending_count()
                    self._metrics.update_queue_depth(queue_depth)

                log.info(
                    "Message %s processed successfully in %.2fs",
                    msg.message_id,
                    processing_time,
                    extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
                )
                return result
            except Exception as exc:
                # Log error but leave message pending for crash recovery
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

                # Suppress error to channel if routing rule disables it
                if not _routing_show_errors_var.get():
                    log.info(
                        "Error suppressed (showErrors=false) for message %s",
                        msg.message_id,
                    )
                    return None

                # Re-raise to let caller handle the error
                # Message stays in pending state for recovery
                raise
            finally:
                # Always clear correlation ID when done
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
        correlation_id = set_correlation_id(f"sched_{chat_id}")

        log.info(
            "Processing scheduled task for chat %s",
            chat_id,
            extra={"chat_id": chat_id},
        )

        # Ensure per-chat workspace directory exists
        workspace_dir = self._memory.ensure_workspace(chat_id)

        # Get channel-specific prompt (e.g. WhatsApp formatting rules)
        channel_prompt = channel.get_channel_prompt() if channel else None

        # Build LLM context with the scheduled prompt as the user message
        memory_content = await self._memory.read_memory(chat_id)
        agents_content = await self._memory.read_agents_md(chat_id)
        project_context = await self._get_project_context(chat_id)
        topic_summary = self._topic_cache.read(chat_id)
        messages = await build_context(
            db=self._db,
            config=self._cfg,
            chat_id=chat_id,
            memory_content=memory_content,
            agents_md=agents_content,
            channel_prompt=channel_prompt,
            project_context=project_context,
            topic_summary=topic_summary,
        )
        # Append the scheduled prompt as the user turn
        messages.append({"role": "user", "content": prompt})

        # Run the ReAct loop
        tools = self._skills.tool_definitions
        try:
            response_text, _ = await self._react_loop(
                chat_id=chat_id,
                messages=messages,
                tools=tools if tools else None,
                workspace_dir=workspace_dir,
                channel=channel,
            )

            # Parse topic META from response before persisting
            response_text, meta = parse_meta(response_text)
            if meta:
                self._handle_topic_meta(chat_id, meta)

            # Persist both turns in conversation history
            await self._db.upsert_chat(chat_id, "Scheduler")
            await self._db.save_message(
                chat_id=chat_id,
                role="user",
                content=prompt,
                name="Scheduler",
                message_id=f"sched_{int(time.time() * 1000)}",
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

    async def _process(
        self,
        msg: IncomingMessage,
        channel: "BaseChannel | None" = None,
        stream_callback: StreamCallback | None = None,
    ) -> str:
        log.debug(
            "Starting _process for chat %s",
            msg.chat_id,
            extra={"chat_id": msg.chat_id},
        )
        # 1. Persist user turn
        await self._db.upsert_chat(msg.chat_id, msg.sender_name)
        await self._db.save_message(
            chat_id=msg.chat_id,
            role="user",
            content=msg.text,
            name=msg.sender_name,
            message_id=msg.message_id,
        )

        # 2. Ensure per-chat workspace directory exists (seeds AGENTS.md)
        workspace_dir = self._memory.ensure_workspace(msg.chat_id)

        # 3. Match routing rule to get instruction file
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
        instruction_content = self._load_instruction(instruction_filename)

        # Get channel-specific prompt
        channel_prompt = channel.get_channel_prompt() if channel else None

        # 4. Build LLM message list (async — fetches history from DB)
        memory_content = await self._memory.read_memory(msg.chat_id)
        agents_content = await self._memory.read_agents_md(msg.chat_id)
        project_context = await self._get_project_context(msg.chat_id)
        topic_summary = self._topic_cache.read(msg.chat_id)
        messages = await build_context(
            db=self._db,
            config=self._cfg,
            chat_id=msg.chat_id,
            memory_content=memory_content,
            agents_md=agents_content,
            instruction=instruction_content,
            channel_prompt=channel_prompt,
            project_context=project_context,
            topic_summary=topic_summary,
        )

        # 5. Run the ReAct loop with optional streaming
        tools = self._skills.tool_definitions
        verbose = matched_rule.skillExecVerbose
        stream_cb = stream_callback if verbose == "full" else None
        raw_response, tool_log = await self._react_loop(
            chat_id=msg.chat_id,
            messages=messages,
            tools=tools if tools else None,
            workspace_dir=workspace_dir,
            stream_callback=stream_cb,
            channel=channel,
        )

        # 6. Parse topic detection META from response
        response_text, meta = parse_meta(raw_response)
        if meta:
            self._handle_topic_meta(msg.chat_id, meta)

        # 7. Append tool summary if skillExecVerbose == "summary"
        if verbose == "summary" and tool_log:
            response_text = format_response_with_tool_log(response_text, tool_log)

        # 8. Persist assistant turn
        await self._db.save_message(
            chat_id=msg.chat_id,
            role="assistant",
            content=response_text,
        )

        return response_text

    # ── ReAct loop ─────────────────────────────────────────────────────────

    async def _react_loop(
        self,
        chat_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: "BaseChannel | None" = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """
        Run the ReAct loop and return response text with tool execution log.

        Args:
            chat_id: Chat identifier for logging.
            messages: Conversation history for LLM context.
            tools: Available tool definitions.
            workspace_dir: Workspace directory for skill execution.
            stream_callback: Optional callback to stream tool executions in real-time.
            channel: Optional channel for media-sending callback injection.

        Returns:
            Tuple of (response_text, tool_log) where tool_log contains
            dicts with 'name', 'args', and 'result' keys.
        """
        max_iter = self._cfg.llm.max_tool_iterations
        tool_log: list[dict[str, Any]] = []

        for iteration in range(max_iter):
            # Track LLM latency
            llm_start = time.perf_counter()
            completion = await self._llm.chat(messages, tools=tools)
            llm_latency = time.perf_counter() - llm_start
            self._metrics.track_llm_latency(llm_latency)

            choice = completion.choices[0]
            finish = choice.finish_reason

            # Use pattern matching for finish reason handling
            match finish:
                case "tool_calls":
                    iteration_log = await self._process_tool_calls(
                        choice,
                        messages,
                        chat_id,
                        workspace_dir,
                        stream_callback,
                        channel,
                    )
                    tool_log.extend(iteration_log)
                case _ if choice.message.tool_calls:
                    # Has tool calls but finish isn't "tool_calls" (edge case)
                    iteration_log = await self._process_tool_calls(
                        choice,
                        messages,
                        chat_id,
                        workspace_dir,
                        stream_callback,
                        channel,
                    )
                    tool_log.extend(iteration_log)
                case _:
                    # LLM is done — return the final text
                    return choice.message.content or "(no response)", tool_log

        log.warning(
            "Reached max tool iterations (%d) for chat %s",
            max_iter,
            chat_id,
            extra={"chat_id": chat_id, "max_iterations": max_iter},
        )
        return "(Max tool iterations reached, change configuration or try again)", tool_log

    async def _process_tool_calls(
        self,
        choice: Any,
        messages: list[dict[str, Any]],
        chat_id: str,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: "BaseChannel | None" = None,
    ) -> list[dict[str, Any]]:
        """
        Process tool calls from an LLM response and append results to messages.

        Args:
            choice: The LLM response choice containing tool calls.
            messages: Conversation history to append results to.
            chat_id: Chat identifier for logging.
            workspace_dir: Workspace directory for skill execution.
            stream_callback: Optional callback to stream tool executions in real-time.
            channel: Optional channel for creating the send_media callback.

        Returns:
            List of dicts with 'name', 'args', and 'result' keys for logging.
        """
        tool_log: list[dict[str, Any]] = []

        # Append the assistant's tool-call turn to context
        messages.append(self._llm.tool_call_to_dict(choice.message))

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
                    log.error("send_media callback failed: %s", exc)

            send_media = _send_media

        # Execute each requested tool
        for tool_call in choice.message.tool_calls or []:
            result = await self._tool_executor.execute(
                chat_id=chat_id,
                tool_call=tool_call,
                workspace_dir=workspace_dir,
                send_media=send_media,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            # Collect tool execution info for logging
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_entry = {
                "name": tool_call.function.name,
                "args": args,
                "result": result,
            }
            tool_log.append(tool_entry)

            # Stream tool execution in real-time if callback provided
            if stream_callback:
                formatted = format_single_tool_execution(tool_entry)
                await stream_callback(formatted)

        return tool_log

    # ── helpers ────────────────────────────────────────────────────────────

    def _handle_topic_meta(self, chat_id: str, meta: dict) -> None:
        """Process topic-change metadata from LLM response.

        If the LLM signals a topic change, save the old-topic summary
        to the per-chat cache. Next call will use the summary instead
        of full history, saving tokens.
        """
        if meta.get("topic_changed") and meta.get("old_topic_summary"):
            self._topic_cache.write(chat_id, meta["old_topic_summary"])
            log.info("Topic changed in chat %s — summary cached", chat_id)

    def _load_instruction(self, filename: str) -> str:
        """Load instruction content via the InstructionLoader."""
        return self._instruction_loader.load(filename)
