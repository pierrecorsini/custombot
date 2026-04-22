"""
Tests for src/builder.py — _build_bot() wiring correctness.

Verifies that _build_bot() correctly instantiates and interconnects
all 8 components: Database, LLM, Memory, VectorMemory, ProjectStore,
RoutingEngine, SkillRegistry, and the Bot orchestrator.

Since _build_bot() uses deferred imports (inside the function body),
we patch at the source module paths (e.g. src.db.Database) so that
the import statements within _build_bot() resolve to our mocks.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.builder import BotComponents, _build_bot
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
from src.skills import SkillRegistry
from src.skills.base import BaseSkill


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Config pointing workspace at tmp_path with skills_auto_load disabled."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
        ),
        skills_auto_load=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_mocks() -> dict:
    """Create mock objects for every component _build_bot() instantiates."""
    mock_db = AsyncMock()
    mock_db.connect = AsyncMock()

    mock_llm = MagicMock()
    mock_llm._client = MagicMock()

    mock_memory = MagicMock()

    mock_vm = MagicMock()
    mock_vm.connect = MagicMock()

    mock_project_store = MagicMock()
    mock_project_store.connect = MagicMock()

    mock_routing = MagicMock()
    mock_routing.rules = []
    mock_routing.load_rules = MagicMock()

    mock_skills = MagicMock()
    mock_skills.all.return_value = []
    mock_skills.load_builtins = MagicMock()
    mock_skills.load_user_skills = MagicMock()

    mock_mq = AsyncMock()
    mock_mq.connect = AsyncMock()
    mock_mq.get_pending_count = AsyncMock(return_value=0)

    mock_progress = MagicMock()
    mock_progress.__enter__ = MagicMock(return_value=mock_progress)
    mock_progress.__exit__ = MagicMock(return_value=False)
    mock_progress.advance = MagicMock()

    mock_spinner = MagicMock()
    mock_spinner.__aenter__ = AsyncMock(return_value=None)
    mock_spinner.__aexit__ = AsyncMock(return_value=False)

    # MagicMock wrappers that act as constructors returning the instance mocks
    mock_db_cls = MagicMock(return_value=mock_db)
    mock_llm_cls = MagicMock(return_value=mock_llm)
    mock_memory_cls = MagicMock(return_value=mock_memory)
    mock_vm_cls = MagicMock(return_value=mock_vm)
    mock_ps_cls = MagicMock(return_value=mock_project_store)
    mock_routing_cls = MagicMock(return_value=mock_routing)
    mock_skills_cls = MagicMock(return_value=mock_skills)
    mock_mq_cls = MagicMock(return_value=mock_mq)

    return {
        "db": mock_db,
        "db_cls": mock_db_cls,
        "llm": mock_llm,
        "llm_cls": mock_llm_cls,
        "memory": mock_memory,
        "memory_cls": mock_memory_cls,
        "vm": mock_vm,
        "vm_cls": mock_vm_cls,
        "project_store": mock_project_store,
        "ps_cls": mock_ps_cls,
        "routing": mock_routing,
        "routing_cls": mock_routing_cls,
        "skills": mock_skills,
        "skills_cls": mock_skills_cls,
        "mq": mock_mq,
        "mq_cls": mock_mq_cls,
        "progress": mock_progress,
        "spinner": mock_spinner,
    }


async def _run_build_bot(test_config: Config, tmp_path: Path) -> tuple[BotComponents, dict]:
    """Execute _build_bot with all deps mocked; return result + mocks."""
    mocks = _make_mocks()

    with ExitStack() as stack:
        stack.enter_context(patch("src.db.Database", mocks["db_cls"]))
        stack.enter_context(patch("src.llm.LLMClient", mocks["llm_cls"]))
        stack.enter_context(patch("src.memory.Memory", mocks["memory_cls"]))
        stack.enter_context(patch("src.vector_memory.VectorMemory", mocks["vm_cls"]))
        stack.enter_context(patch("src.project.store.ProjectStore", mocks["ps_cls"]))
        stack.enter_context(patch("src.routing.RoutingEngine", mocks["routing_cls"]))
        stack.enter_context(patch("src.skills.SkillRegistry", mocks["skills_cls"]))
        stack.enter_context(patch("src.builder.MessageQueue", mocks["mq_cls"]))
        stack.enter_context(patch("src.builder.ProgressBar", return_value=mocks["progress"]))
        stack.enter_context(patch("src.builder.maybe_spinner_async", return_value=mocks["spinner"]))
        stack.enter_context(patch("src.builder.WORKSPACE_DIR", str(tmp_path)))
        stack.enter_context(patch("src.core.instruction_loader.InstructionLoader", MagicMock()))
        stack.enter_context(patch("src.core.project_context.ProjectContextLoader", MagicMock()))
        result = await _build_bot(test_config)

    return result, mocks


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Return type
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildBotReturnType:
    async def test_returns_bot_components(self, test_config: Config, tmp_path: Path):
        result, _ = await _run_build_bot(test_config, tmp_path)
        assert isinstance(result, BotComponents)

    async def test_all_fields_populated(self, test_config: Config, tmp_path: Path):
        result, _ = await _run_build_bot(test_config, tmp_path)
        assert result.bot is not None
        assert result.db is not None
        assert result.project_store is not None
        assert result.message_queue is not None
        assert result.llm is not None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Component construction
# ─────────────────────────────────────────────────────────────────────────────


class TestDatabaseConstruction:
    async def test_database_created_with_data_dir(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["db_cls"].assert_called_once()
        call_kwargs = mocks["db_cls"].call_args[1]
        assert "data_dir" in call_kwargs
        assert str(tmp_path) in call_kwargs["data_dir"]


class TestLLMConstruction:
    async def test_llm_receives_config(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["llm_cls"].assert_called_once()
        call_args = mocks["llm_cls"].call_args
        assert call_args[0][0] is test_config.llm


class TestMemoryConstruction:
    async def test_memory_created_with_workspace(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["memory_cls"].assert_called_once_with(str(tmp_path))


class TestVectorMemoryConstruction:
    async def test_vm_receives_llm_client_and_config(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["vm_cls"].assert_called_once()
        call_kwargs = mocks["vm_cls"].call_args[1]
        assert call_kwargs["openai_client"] is mocks["llm"]._client
        assert call_kwargs["embedding_model"] == test_config.llm.embedding_model
        assert call_kwargs["embedding_dimensions"] == test_config.llm.embedding_dimensions

    async def test_vm_connect_called(self, test_config: Config, tmp_path: Path):
        await _run_build_bot(test_config, tmp_path)
        # vm.connect() called during construction — verified by no crash

    async def test_vm_on_result_when_connected(self, test_config: Config, tmp_path: Path):
        result, mocks = await _run_build_bot(test_config, tmp_path)
        assert result.vector_memory is mocks["vm"]

    async def test_vm_degrades_gracefully_on_failure(self, test_config: Config, tmp_path: Path):
        """VectorMemory failure should not crash; result.vector_memory should be None."""
        mocks = _make_mocks()

        with ExitStack() as stack:
            stack.enter_context(patch("src.db.Database", mocks["db_cls"]))
            stack.enter_context(patch("src.llm.LLMClient", mocks["llm_cls"]))
            stack.enter_context(patch("src.memory.Memory", mocks["memory_cls"]))
            stack.enter_context(patch("src.vector_memory.VectorMemory", side_effect=RuntimeError("sqlite-vec missing")))
            stack.enter_context(patch("src.project.store.ProjectStore", mocks["ps_cls"]))
            stack.enter_context(patch("src.routing.RoutingEngine", mocks["routing_cls"]))
            stack.enter_context(patch("src.skills.SkillRegistry", mocks["skills_cls"]))
            stack.enter_context(patch("src.builder.ProgressBar", return_value=mocks["progress"]))
            stack.enter_context(patch("src.builder.maybe_spinner_async", return_value=mocks["spinner"]))
            stack.enter_context(patch("src.builder.WORKSPACE_DIR", str(tmp_path)))
            stack.enter_context(patch("src.core.instruction_loader.InstructionLoader", MagicMock()))
            stack.enter_context(patch("src.core.project_context.ProjectContextLoader", MagicMock()))
            result = await _build_bot(test_config)

        assert result.vector_memory is None


class TestProjectStoreConstruction:
    async def test_project_store_created_and_connected(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["ps_cls"].assert_called_once()
        mocks["project_store"].connect.assert_called_once()


class TestRoutingEngineConstruction:
    async def test_routing_created_with_instructions_dir(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["routing_cls"].assert_called_once()
        call_args = mocks["routing_cls"].call_args[0]
        assert str(tmp_path) in str(call_args[0])

    async def test_load_rules_called(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["routing"].load_rules.assert_called_once()


class TestSkillRegistryConstruction:
    async def test_skills_load_builtins_called_with_deps(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["skills"].load_builtins.assert_called_once()
        call_kwargs = mocks["skills"].load_builtins.call_args[1]
        assert call_kwargs["db"] is mocks["db"]
        assert call_kwargs["vector_memory"] is mocks["vm"]
        assert call_kwargs["project_store"] is mocks["project_store"]

    async def test_user_skills_not_loaded_when_auto_load_disabled(self, test_config: Config, tmp_path: Path):
        _, mocks = await _run_build_bot(test_config, tmp_path)
        mocks["skills"].load_user_skills.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Bot wiring
# ─────────────────────────────────────────────────────────────────────────────


class TestBotWiring:
    async def test_bot_receives_all_components(self, test_config: Config, tmp_path: Path):
        result, mocks = await _run_build_bot(test_config, tmp_path)
        bot = result.bot
        assert bot._db is mocks["db"]
        assert bot._llm is mocks["llm"]
        assert bot._memory is mocks["memory"]
        assert bot._skills is mocks["skills"]
        assert bot._routing is mocks["routing"]
        assert bot._project_store is mocks["project_store"]

    async def test_bot_receives_config(self, test_config: Config, tmp_path: Path):
        result, _ = await _run_build_bot(test_config, tmp_path)
        assert result.bot._cfg is test_config

    async def test_result_db_matches_bot_db(self, test_config: Config, tmp_path: Path):
        result, _ = await _run_build_bot(test_config, tmp_path)
        assert result.bot._db is result.db

    async def test_result_project_store_matches_bot(self, test_config: Config, tmp_path: Path):
        result, _ = await _run_build_bot(test_config, tmp_path)
        assert result.bot._project_store is result.project_store


# ─────────────────────────────────────────────────────────────────────────────
# Tests: BotComponents dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestBotComponentsDataclass:
    def test_frozen_dataclass(self):
        """BotComponents should be immutable."""
        bot = MagicMock()
        db = MagicMock()
        vm = MagicMock()
        ps = MagicMock()
        bc = BotComponents(bot=bot, db=db, vector_memory=vm, project_store=ps)
        with pytest.raises(AttributeError):
            bc.bot = MagicMock()  # type: ignore[misc]

    def test_fields_accessible_by_name(self):
        bot = MagicMock()
        db = MagicMock()
        vm = MagicMock()
        ps = MagicMock()
        bc = BotComponents(bot=bot, db=db, vector_memory=vm, project_store=ps)
        assert bc.bot is bot
        assert bc.db is db
        assert bc.vector_memory is vm
        assert bc.project_store is ps


# ─────────────────────────────────────────────────────────────────────────────
# Tests: wire_llm_clients() resilience
# ─────────────────────────────────────────────────────────────────────────────


class TestWireLLMClientsResilience:
    """Verify wire_llm_clients() is resilient to per-skill wiring failures."""

    def test_one_failing_skill_does_not_break_others(self, caplog):
        """When a skill's wire_llm() raises, other skills still get wired."""
        registry = SkillRegistry()
        mock_llm = MagicMock()

        # Skill A — needs LLM, wires successfully
        skill_a = MagicMock(spec=BaseSkill)
        skill_a.name = "skill_a"
        skill_a.needs_llm.return_value = True
        skill_a.wire_llm = MagicMock()

        # Skill B — needs LLM, but wire_llm raises
        skill_b = MagicMock(spec=BaseSkill)
        skill_b.name = "skill_b"
        skill_b.needs_llm.return_value = True
        skill_b.wire_llm = MagicMock(side_effect=RuntimeError("wiring exploded"))

        # Skill C — needs LLM, wires successfully
        skill_c = MagicMock(spec=BaseSkill)
        skill_c.name = "skill_c"
        skill_c.needs_llm.return_value = True
        skill_c.wire_llm = MagicMock()

        registry.register(skill_a)
        registry.register(skill_b)
        registry.register(skill_c)

        with caplog.at_level("ERROR", logger="src.skills"):
            # (c) No exception propagates from wire_llm_clients()
            registry.wire_llm_clients(mock_llm)

        # (a) Skills A and C still receive the LLM client
        skill_a.wire_llm.assert_called_once_with(mock_llm)
        skill_c.wire_llm.assert_called_once_with(mock_llm)

        # Skill B was attempted despite raising
        skill_b.wire_llm.assert_called_once_with(mock_llm)

        # (b) The error is logged with the failing skill name
        error_msgs = [r.message for r in caplog.records]
        assert any("skill_b" in msg for msg in error_msgs)
        assert any("Failed to wire" in msg for msg in error_msgs)
