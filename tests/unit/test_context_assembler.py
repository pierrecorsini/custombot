"""
Tests for src/core/context_assembler.py — ContextAssembler.

Covers:
- assemble() with all context sources returning values (happy path)
- assemble() with all context sources returning None (empty context)
- assemble() tolerates individual context-read failures (gather errors)
- _handle_gather_result() returns value on success, default on exception
- _async_topic_read() delegates to TopicCache.read()
- _async_compressed_summary() delegates to db.get_compressed_summary()
- finalize_turn() with topic change writes to cache and returns clean text
- finalize_turn() without topic change returns original text
- finalize_turn() with malformed META returns original text
- finalize_turn() with topic_changed=false does not write to cache
- assemble() passes instruction and channel_prompt metadata through
- assemble() with compressed_summary tracks metrics
- Concurrent assembly calls for different chats do not interfere
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import BotConfig
from src.core.context_assembler import ContextAssembler, ContextResult
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_config(
    system_prompt_prefix: str = "You are a helpful assistant.",
    memory_max_history: int = 50,
) -> BotConfig:
    """Create a BotConfig for testing."""
    return BotConfig(
        max_tool_iterations=10,
        memory_max_history=memory_max_history,
        system_prompt_prefix=system_prompt_prefix,
    )


def _make_db(rows: list[dict] | None = None) -> AsyncMock:
    """Create a mock Database with compressed summary support."""
    db = AsyncMock()
    db.get_recent_messages = AsyncMock(return_value=rows or [])
    db.get_compressed_summary = AsyncMock(return_value=None)
    return db


def _make_memory(
    memory_content: str | None = None,
    agents_content: str = "",
) -> AsyncMock:
    """Create a mock MemoryProtocol."""
    memory = AsyncMock()
    memory.read_memory = AsyncMock(return_value=memory_content)
    memory.read_agents_md = AsyncMock(return_value=agents_content)
    return memory


def _make_project_ctx(content: str | None = None) -> AsyncMock:
    """Create a mock ProjectContextLoader."""
    loader = AsyncMock()
    loader.get = AsyncMock(return_value=content)
    return loader


def _make_assembler(
    tmp_path: Path,
    config: BotConfig | None = None,
    db: AsyncMock | None = None,
    memory: AsyncMock | None = None,
    project_ctx: AsyncMock | None = None,
) -> ContextAssembler:
    """Create a ContextAssembler with sensible defaults."""
    return ContextAssembler(
        db=db or _make_db(),
        config=config or _make_config(),
        memory=memory or _make_memory(),
        project_ctx=project_ctx or _make_project_ctx(),
        workspace_root=str(tmp_path),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test _handle_gather_result
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleGatherResult:
    """Tests for the static _handle_gather_result() method."""

    def test_returns_value_on_success(self) -> None:
        result = ContextAssembler._handle_gather_result(
            "some content",
            "read_memory",
            "chat_1",
            default=None,
        )
        assert result == "some content"

    def test_returns_none_value(self) -> None:
        result = ContextAssembler._handle_gather_result(
            None,
            "read_memory",
            "chat_1",
            default="fallback",
        )
        assert result is None

    def test_returns_default_on_exception(self) -> None:
        exc = RuntimeError("db connection lost")
        result = ContextAssembler._handle_gather_result(
            exc,
            "read_memory",
            "chat_1",
            default=None,
        )
        assert result is None

    def test_returns_custom_default_on_exception(self) -> None:
        exc = OSError("file not found")
        result = ContextAssembler._handle_gather_result(
            exc,
            "read_agents_md",
            "chat_1",
            default="fallback agents",
        )
        assert result == "fallback agents"

    def test_logs_on_exception(self) -> None:
        exc = ValueError("bad data")
        with patch("src.core.context_assembler.log") as mock_log:
            ContextAssembler._handle_gather_result(
                exc,
                "read_memory",
                "chat_1",
                default=None,
            )
            mock_log.log.assert_called_once()
            args = mock_log.log.call_args
            assert args[0][0] == 30  # WARNING level

    def test_uses_custom_log_level(self) -> None:
        exc = ValueError("bad data")
        with patch("src.core.context_assembler.log") as mock_log:
            import logging

            ContextAssembler._handle_gather_result(
                exc,
                "read_agents_md",
                "chat_1",
                default=None,
                log_level=logging.DEBUG,
            )
            args = mock_log.log.call_args
            assert args[0][0] == 10  # DEBUG level

    def test_handles_base_exception_subclass(self) -> None:
        exc = KeyboardInterrupt()
        result = ContextAssembler._handle_gather_result(
            exc,
            "source",
            "chat_1",
            default="safe",
        )
        assert result == "safe"


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleHappyPath:
    """Tests for assemble() with all sources returning values."""

    async def test_returns_context_result_with_messages(self, tmp_path: Path) -> None:
        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[{"role": "user", "content": "Hello"}]),
            memory=_make_memory(memory_content="User likes Python.", agents_content="# Guide"),
            project_ctx=_make_project_ctx("Project info."),
        )

        result = await assembler.assemble("chat_1")

        assert isinstance(result, ContextResult)
        assert len(result.messages) >= 1
        assert result.messages[0].role == "system"

    async def test_system_prompt_contains_all_context(self, tmp_path: Path) -> None:
        assembler = _make_assembler(
            tmp_path,
            db=_make_db(),
            memory=_make_memory(
                memory_content="Remember this.",
                agents_content="# Agent Rules",
            ),
            project_ctx=_make_project_ctx("Knowledge base."),
        )

        result = await assembler.assemble("chat_1")

        system_content = result.messages[0].content
        assert "Remember this." in system_content
        assert "Agent Rules" in system_content
        assert "Knowledge base." in system_content

    async def test_metadata_passed_through(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        result = await assembler.assemble(
            "chat_1",
            channel_prompt="Channel prompt",
            instruction="Follow instructions.",
            rule_id="rule-42",
        )

        assert result.instruction_used == "Follow instructions."
        assert result.rule_id == "rule-42"
        assert result.channel_prompt == "Channel prompt"

    async def test_instruction_and_channel_prompt_in_system_message(
        self,
        tmp_path: Path,
    ) -> None:
        assembler = _make_assembler(tmp_path)

        result = await assembler.assemble(
            "chat_1",
            channel_prompt="Be brief.",
            instruction="Answer in English.",
        )

        system_content = result.messages[0].content
        assert "Be brief." in system_content
        assert "Answer in English." in system_content


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — empty / missing context
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleEmptyContext:
    """Tests for assemble() when context sources return None/empty."""

    async def test_all_sources_none_still_returns_system_message(
        self,
        tmp_path: Path,
    ) -> None:
        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            memory=_make_memory(memory_content=None, agents_content=""),
            project_ctx=_make_project_ctx(None),
        )

        result = await assembler.assemble("chat_1")

        # At minimum, system message with META prompt
        assert len(result.messages) >= 1
        assert result.messages[0].role == "system"
        assert "---META---" in result.messages[0].content

    async def test_no_history_returns_only_system_message(self, tmp_path: Path) -> None:
        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
        )

        result = await assembler.assemble("chat_1")

        assert len(result.messages) == 1
        assert result.messages[0].role == "system"


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — error tolerance (gather exceptions)
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleGatherErrors:
    """Tests for assemble() tolerating individual context-read failures."""

    async def test_memory_read_failure_returns_result(self, tmp_path: Path) -> None:
        memory = _make_memory()
        memory.read_memory = AsyncMock(side_effect=RuntimeError("disk error"))

        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            memory=memory,
        )

        # Should not raise — gather catches and _handle_gather_result logs
        result = await assembler.assemble("chat_1")
        assert isinstance(result, ContextResult)

    async def test_agents_md_failure_uses_default(self, tmp_path: Path) -> None:
        memory = _make_memory()
        memory.read_agents_md = AsyncMock(side_effect=FileNotFoundError("not found"))

        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            memory=memory,
        )

        result = await assembler.assemble("chat_1")
        system_content = result.messages[0].content
        # DEFAULT_AGENTS_MD content should be in the system prompt
        assert "Agent Instructions" in system_content

    async def test_project_ctx_failure_still_assembles(self, tmp_path: Path) -> None:
        project_ctx = _make_project_ctx()
        project_ctx.get = AsyncMock(side_effect=OSError("unavailable"))

        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            project_ctx=project_ctx,
        )

        result = await assembler.assemble("chat_1")
        assert isinstance(result, ContextResult)

    async def test_all_sources_fail_still_returns_result(self, tmp_path: Path) -> None:
        memory = _make_memory()
        memory.read_memory = AsyncMock(side_effect=RuntimeError("fail"))
        memory.read_agents_md = AsyncMock(side_effect=RuntimeError("fail"))

        project_ctx = _make_project_ctx()
        project_ctx.get = AsyncMock(side_effect=RuntimeError("fail"))

        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(side_effect=RuntimeError("fail"))

        assembler = _make_assembler(
            tmp_path,
            db=db,
            memory=memory,
            project_ctx=project_ctx,
        )

        result = await assembler.assemble("chat_1")
        assert isinstance(result, ContextResult)
        assert len(result.messages) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — graceful degradation (one failure, others unaffected)
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleGracefulDegradation:
    """Tests that a single failing read is substituted with its default
    and the remaining reads are completely unaffected."""

    async def test_memory_read_failure_others_unaffected(
        self,
        tmp_path: Path,
    ) -> None:
        """When read_memory raises OSError, other sources still contribute."""
        memory = _make_memory()
        memory.read_memory = AsyncMock(side_effect=OSError("disk read failed"))
        memory.read_agents_md = AsyncMock(return_value="# Custom Agents Guide")

        project_ctx = _make_project_ctx("Project context data.")

        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(return_value="Archived summary.")

        assembler = _make_assembler(
            tmp_path,
            db=db,
            memory=memory,
            project_ctx=project_ctx,
        )

        result = await assembler.assemble("chat_1")

        # Arrange: valid ContextResult returned despite one failure
        assert isinstance(result, ContextResult)
        assert len(result.messages) >= 1
        assert result.messages[0].role == "system"

        system_content = result.messages[0].content

        # Failed read_memory → default None substituted, so no memory content
        # Other sources should be completely unaffected:
        assert "# Custom Agents Guide" in system_content
        assert "Project context data." in system_content
        assert "Archived summary." in system_content

    async def test_project_ctx_failure_others_unaffected(
        self,
        tmp_path: Path,
    ) -> None:
        """When project_ctx.get raises OSError, memory and agents still contribute."""
        memory = _make_memory(
            memory_content="Important memory note.",
            agents_content="# Agent Directives",
        )

        project_ctx = _make_project_ctx()
        project_ctx.get = AsyncMock(side_effect=OSError("project store unavailable"))

        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            memory=memory,
            project_ctx=project_ctx,
        )

        result = await assembler.assemble("chat_1")

        assert isinstance(result, ContextResult)
        system_content = result.messages[0].content

        assert "Important memory note." in system_content
        assert "# Agent Directives" in system_content

    async def test_compressed_summary_failure_others_unaffected(
        self,
        tmp_path: Path,
    ) -> None:
        """When compressed summary raises OSError, other sources contribute."""
        memory = _make_memory(
            memory_content="User prefers Python.",
            agents_content="# Rules",
        )
        project_ctx = _make_project_ctx("Project metadata.")

        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(side_effect=OSError("db unavailable"))

        assembler = _make_assembler(
            tmp_path,
            db=db,
            memory=memory,
            project_ctx=project_ctx,
        )

        result = await assembler.assemble("chat_1")

        assert isinstance(result, ContextResult)
        system_content = result.messages[0].content

        assert "User prefers Python." in system_content
        assert "# Rules" in system_content
        assert "Project metadata." in system_content


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — topic cache integration
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleTopicCache:
    """Tests for topic cache read during assemble()."""

    async def test_topic_summary_included_in_system_prompt(
        self,
        tmp_path: Path,
    ) -> None:
        assembler = _make_assembler(tmp_path, db=_make_db(rows=[]))

        # Write a topic summary file so TopicCache.read() returns it
        topic_dir = tmp_path / "whatsapp_data" / "chat_1"
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / ".topic_summary.md").write_text(
            "Previous discussion about Python async.", encoding="utf-8"
        )

        result = await assembler.assemble("chat_1")
        system_content = result.messages[0].content
        assert "Previous discussion about Python async." in system_content

    async def test_topic_summary_absent_does_not_error(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path, db=_make_db(rows=[]))

        result = await assembler.assemble("chat_nonexistent")
        assert isinstance(result, ContextResult)


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — compressed summary
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleCompressedSummary:
    """Tests for compressed summary integration."""

    @patch("src.core.context_assembler.get_metrics_collector")
    async def test_compressed_summary_tracks_metrics(
        self,
        mock_get_metrics: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(return_value="Archived conversation...")

        assembler = _make_assembler(tmp_path, db=db)
        await assembler.assemble("chat_1")

        mock_metrics.track_compression_summary_used.assert_called_once()

    @patch("src.core.context_assembler.get_metrics_collector")
    async def test_no_compressed_summary_does_not_track_metrics(
        self,
        mock_get_metrics: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(return_value=None)

        assembler = _make_assembler(tmp_path, db=db)
        await assembler.assemble("chat_1")

        mock_metrics.track_compression_summary_used.assert_not_called()

    async def test_compressed_summary_in_system_prompt(self, tmp_path: Path) -> None:
        db = _make_db(rows=[])
        db.get_compressed_summary = AsyncMock(return_value="Summary of old messages.")

        assembler = _make_assembler(tmp_path, db=db)
        result = await assembler.assemble("chat_1")

        system_content = result.messages[0].content
        assert "Summary of old messages." in system_content


# ─────────────────────────────────────────────────────────────────────────────
# Test _async_topic_read
# ─────────────────────────────────────────────────────────────────────────────


class TestAsyncTopicRead:
    """Tests for _async_topic_read() wrapper."""

    async def test_returns_topic_cache_value(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        # Write a topic file
        topic_dir = tmp_path / "whatsapp_data" / "chat_1"
        topic_dir.mkdir(parents=True, exist_ok=True)
        (topic_dir / ".topic_summary.md").write_text("Cached topic.", encoding="utf-8")

        result = await assembler._async_topic_read("chat_1")
        assert result == "Cached topic."

    async def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)
        result = await assembler._async_topic_read("nonexistent_chat")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Test _async_compressed_summary
# ─────────────────────────────────────────────────────────────────────────────


class TestAsyncCompressedSummary:
    """Tests for _async_compressed_summary() wrapper."""

    async def test_delegates_to_db(self, tmp_path: Path) -> None:
        db = _make_db()
        db.get_compressed_summary = AsyncMock(return_value="compressed data")

        assembler = _make_assembler(tmp_path, db=db)
        result = await assembler._async_compressed_summary("chat_1")

        assert result == "compressed data"
        db.get_compressed_summary.assert_awaited_once_with("chat_1")

    async def test_returns_none_when_no_summary(self, tmp_path: Path) -> None:
        db = _make_db()
        db.get_compressed_summary = AsyncMock(return_value=None)

        assembler = _make_assembler(tmp_path, db=db)
        result = await assembler._async_compressed_summary("chat_1")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Test finalize_turn
# ─────────────────────────────────────────────────────────────────────────────


class TestFinalizeTurn:
    """Tests for finalize_turn() — META parsing and topic cache writes."""

    def test_topic_change_writes_cache(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        response = 'Here\'s the answer.\n---META---\n{"topic_changed": true, "old_topic_summary": "Discussing testing patterns."}'

        result = assembler.finalize_turn("chat_1", response)

        assert result == "Here's the answer."
        # Verify topic was written
        topic_file = tmp_path / "whatsapp_data" / "chat_1" / ".topic_summary.md"
        assert topic_file.exists()
        assert "Discussing testing patterns." in topic_file.read_text(encoding="utf-8")

    def test_no_meta_returns_original_text(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        response = "Just a plain response with no META block."
        result = assembler.finalize_turn("chat_1", response)

        assert result == response

    def test_topic_not_changed_does_not_write(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        response = 'Answer.\n---META---\n{"topic_changed": false}'
        result = assembler.finalize_turn("chat_1", response)

        assert result == "Answer."
        topic_file = tmp_path / "whatsapp_data" / "chat_1" / ".topic_summary.md"
        assert not topic_file.exists()

    def test_malformed_meta_json_returns_original(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        response = "Answer.\n---META---\n{invalid json}"
        result = assembler.finalize_turn("chat_1", response)

        # Falls back to returning original response
        assert result == response

    def test_topic_changed_without_summary_does_not_write(
        self,
        tmp_path: Path,
    ) -> None:
        assembler = _make_assembler(tmp_path)

        response = 'Answer.\n---META---\n{"topic_changed": true}'
        result = assembler.finalize_turn("chat_1", response)

        assert result == "Answer."
        topic_file = tmp_path / "whatsapp_data" / "chat_1" / ".topic_summary.md"
        assert not topic_file.exists()

    def test_multiple_finalize_turns_overwrite_topic(self, tmp_path: Path) -> None:
        assembler = _make_assembler(tmp_path)

        response1 = 'First.\n---META---\n{"topic_changed": true, "old_topic_summary": "Topic A"}'
        assembler.finalize_turn("chat_1", response1)

        response2 = 'Second.\n---META---\n{"topic_changed": true, "old_topic_summary": "Topic B"}'
        result = assembler.finalize_turn("chat_1", response2)

        assert result == "Second."
        topic_file = tmp_path / "whatsapp_data" / "chat_1" / ".topic_summary.md"
        assert "Topic B" in topic_file.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Test ContextResult
# ─────────────────────────────────────────────────────────────────────────────


class TestContextResult:
    """Tests for the ContextResult dataclass."""

    def test_frozen_immutable(self) -> None:
        result = ContextResult(
            messages=[],
            instruction_used="test",
            rule_id="rule-1",
            channel_prompt="prompt",
        )
        with pytest.raises(AttributeError):
            result.instruction_used = "modified"  # type: ignore[misc]

    def test_slots_no_dict(self) -> None:
        result = ContextResult(
            messages=[],
            instruction_used="",
            rule_id=None,
            channel_prompt=None,
        )
        assert not hasattr(result, "__dict__")

    def test_fields_set_correctly(self) -> None:
        result = ContextResult(
            messages=["msg1", "msg2"],  # type: ignore[list-item]
            instruction_used="instr",
            rule_id="r1",
            channel_prompt="cp",
        )
        assert result.messages == ["msg1", "msg2"]
        assert result.instruction_used == "instr"
        assert result.rule_id == "r1"
        assert result.channel_prompt == "cp"

    def test_none_optional_fields(self) -> None:
        result = ContextResult(
            messages=[],
            instruction_used="",
            rule_id=None,
            channel_prompt=None,
        )
        assert result.rule_id is None
        assert result.channel_prompt is None


# ─────────────────────────────────────────────────────────────────────────────
# Test concurrent assembly calls
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentAssembly:
    """Tests for concurrent assembly calls on different chats."""

    async def test_concurrent_assembles_do_not_interfere(self, tmp_path: Path) -> None:
        """Two simultaneous assemble() calls for different chats produce correct results."""
        db = _make_db(
            rows=[
                {"role": "user", "content": "Chat A message"},
            ]
        )

        memory = _make_memory(
            memory_content="Memory for chat.",
            agents_content="# Guide",
        )
        project_ctx = _make_project_ctx("Project info.")

        assembler = _make_assembler(
            tmp_path,
            db=db,
            memory=memory,
            project_ctx=project_ctx,
        )

        # Run two assemblies concurrently
        results = await asyncio.gather(
            assembler.assemble("chat_a", instruction="Instr A"),
            assembler.assemble("chat_b", instruction="Instr B"),
        )

        assert len(results) == 2
        assert results[0].instruction_used == "Instr A"
        assert results[1].instruction_used == "Instr B"
        # Both should have valid system messages
        assert results[0].messages[0].role == "system"
        assert results[1].messages[0].role == "system"


# ─────────────────────────────────────────────────────────────────────────────
# Test assemble() — full integration with finalize_turn
# ─────────────────────────────────────────────────────────────────────────────


class TestAssembleAndFinalizeIntegration:
    """Integration tests for assemble() + finalize_turn() lifecycle."""

    async def test_assemble_reads_cached_topic_from_finalize(
        self,
        tmp_path: Path,
    ) -> None:
        """After finalize_turn writes a topic, the next assemble reads it."""
        assembler = _make_assembler(
            tmp_path,
            db=_make_db(rows=[]),
            memory=_make_memory(memory_content=None, agents_content=""),
            project_ctx=_make_project_ctx(None),
        )

        # First: finalize a turn that changes topic
        response = 'New topic answer.\n---META---\n{"topic_changed": true, "old_topic_summary": "Previous: discussing databases."}'
        assembler.finalize_turn("chat_1", response)

        # Second: assemble should pick up the cached topic summary
        result = await assembler.assemble("chat_1")
        system_content = result.messages[0].content
        assert "Previous: discussing databases." in system_content
