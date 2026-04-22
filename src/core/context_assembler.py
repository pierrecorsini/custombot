"""
src/core/context_assembler.py — Context assembly service for LLM conversations.

Orchestrates the 4 async context reads (memory, agents_md, project context,
topic cache) and delegates to ``build_context()`` for token-budget trimming
and sanitization.

Owns the full topic lifecycle: reads cached summaries during assembly,
and writes updated summaries via ``finalize_turn()`` after each LLM
response.  Callers never interact with ``TopicCache`` directly.

Returns a typed ``ContextResult`` carrying the assembled messages alongside
resolution metadata (instruction, rule_id, channel_prompt) so downstream
code (metrics, logging, audit) can access the full context without
re-deriving it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.bot import BotConfig
    from src.db import Database
    from src.utils.protocols import MemoryProtocol, ProjectContextLoader

from src.core.context_builder import ChatMessage, build_context
from src.core.topic_cache import TopicCache, parse_meta

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ContextResult:
    """Immutable result of context assembly.

    Carries the built LLM message list alongside resolution metadata
    (instruction content, routing rule ID, channel prompt) so downstream
    code can log, audit, or emit metrics without re-deriving these values.
    """

    messages: list[ChatMessage]
    instruction_used: str
    rule_id: str | None
    channel_prompt: str | None


class ContextAssembler:
    """Stateless service that assembles LLM context from multiple sources.

    Coordinates 4 independent context reads (memory, agents_md, project
    context, topic cache) and delegates message-list construction to
    ``build_context()``. Designed for dependency injection and isolated
    unit testing.
    """

    def __init__(
        self,
        db: Database,
        config: BotConfig,
        memory: MemoryProtocol,
        project_ctx: ProjectContextLoader,
        workspace_root: str,
    ) -> None:
        self._db = db
        self._config = config
        self._memory = memory
        self._project_ctx = project_ctx
        self._topic_cache = TopicCache(workspace_root)

    @staticmethod
    def _handle_gather_result(
        result: object,
        source: str,
        chat_id: str,
        default: str | None,
    ) -> str | None:
        """Return *result* if it is a value, or *default* if it is an exception."""
        if isinstance(result, BaseException):
            log.warning(
                "Context read '%s' failed for chat %s: %s",
                source,
                chat_id,
                result,
            )
            return default
        return result  # type: ignore[return-value]

    async def _async_topic_read(self, chat_id: str) -> str | None:
        """Read topic summary — wrapped as coroutine for ``asyncio.gather``."""
        return self._topic_cache.read(chat_id)

    async def _async_compressed_summary(self, chat_id: str) -> str | None:
        """Read compressed history summary — wrapped as coroutine for gather."""
        return await self._db.get_compressed_summary(chat_id)

    async def assemble(
        self,
        chat_id: str,
        channel_prompt: str | None = None,
        instruction: str = "",
        rule_id: str | None = None,
    ) -> ContextResult:
        """Read all context sources and build the LLM message list.

        Args:
            chat_id: Target chat identifier.
            channel_prompt: Optional channel-specific prompt text.
            instruction: Routed instruction content (empty for scheduled tasks).
            rule_id: Matched routing rule ID (None for scheduled tasks).

        Returns:
            ``ContextResult`` with assembled messages and resolution metadata.
        """
        # Run all 5 independent reads concurrently; tolerate individual failures
        (
            memory_content,
            agents_content,
            project_context,
            topic_summary,
            compressed_summary,
        ) = await asyncio.gather(
            self._memory.read_memory(chat_id),
            self._memory.read_agents_md(chat_id),
            self._project_ctx.get(chat_id),
            self._async_topic_read(chat_id),
            self._async_compressed_summary(chat_id),
            return_exceptions=True,
        )

        memory_content = self._handle_gather_result(
            memory_content, "read_memory", chat_id, default=None,
        )
        agents_content = self._handle_gather_result(
            agents_content, "read_agents_md", chat_id, default="",
        )
        project_context = self._handle_gather_result(
            project_context, "get_project_context", chat_id, default=None,
        )
        topic_summary = self._handle_gather_result(
            topic_summary, "topic_cache_read", chat_id, default=None,
        )
        compressed_summary = self._handle_gather_result(
            compressed_summary, "compressed_summary", chat_id, default=None,
        )

        messages = await build_context(
            db=self._db,
            config=self._config,
            chat_id=chat_id,
            memory_content=memory_content,
            agents_md=agents_content,
            instruction=instruction,
            channel_prompt=channel_prompt,
            project_context=project_context,
            topic_summary=topic_summary,
            compressed_summary=compressed_summary,
        )

        return ContextResult(
            messages=messages,
            instruction_used=instruction,
            rule_id=rule_id,
            channel_prompt=channel_prompt,
        )

    def finalize_turn(self, chat_id: str, response_text: str) -> str:
        """Parse topic META from LLM response and update the topic cache.

        Extracts ``---META---`` blocks from the raw response.  When the LLM
        signals a topic change (``topic_changed: true`` with an
        ``old_topic_summary``), the summary is written to the per-chat topic
        cache so subsequent context assembly can use the compressed form.

        Args:
            chat_id: Target chat identifier.
            response_text: Raw LLM response (may contain ``---META---``).

        Returns:
            Cleaned response text with the META block stripped.
        """
        clean_text, meta = parse_meta(response_text)
        if meta and meta.get("topic_changed") and meta.get("old_topic_summary"):
            self._topic_cache.write(chat_id, meta["old_topic_summary"])
            log.info("Topic changed in chat %s — summary cached", chat_id)
        return clean_text
