"""
src/builder.py — Bot component builder with progress indicators.

Instantiates and wires all bot components: Database, LLM, Memory,
Routing, Skills, and the Bot orchestrator.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from src.config import Config
from src.constants import WORKSPACE_DIR
from src.lifecycle import (
    _log_component_init,
    _log_component_ready,
    _log_skills_loaded,
)
from src.progress import ProgressBar, maybe_spinner_async

if TYPE_CHECKING:
    from src.bot import Bot
    from src.db import Database
    from src.llm import LLMClient, TokenUsage
    from src.message_queue import MessageQueue
    from src.monitoring.performance import SessionMetrics
    from src.project.store import ProjectStore
    from src.vector_memory import VectorMemory

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotComponents:
    """Named container for the components returned by _build_bot()."""

    bot: Bot
    db: Database
    vector_memory: Optional[VectorMemory]
    project_store: ProjectStore
    token_usage: TokenUsage
    message_queue: MessageQueue
    llm: LLMClient
    component_durations: dict[str, float] = field(default_factory=dict)


async def _build_bot(config: Config, session_metrics: "SessionMetrics | None" = None) -> BotComponents:
    """Instantiate and wire all components with progress indicators."""
    from src.bot import Bot, BotConfig
    from src.db import Database
    from src.llm import LLMClient, TokenUsage
    from src.memory import Memory
    from src.message_queue import MessageQueue
    from src.project.store import ProjectStore
    from src.routing import RoutingEngine
    from src.skills import SkillRegistry
    from src.vector_memory import VectorMemory

    workspace = Path(WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)

    total_steps = 10
    component_durations: dict[str, float] = {}

    with ProgressBar("Initializing components", total=total_steps) as progress:
        # Step 0: Workspace integrity check
        _log_component_init("Workspace Integrity", "started")
        t0 = time.monotonic()
        from src.workspace_integrity import check_workspace_integrity

        integrity = await check_workspace_integrity(workspace)
        if integrity.repaired:
            log.info(
                "Workspace integrity: auto-repaired %s",
                integrity.repaired,
            )
        if integrity.warnings:
            for w in integrity.warnings:
                log.warning("Workspace integrity: %s", w)
        if integrity.errors:
            for e in integrity.errors:
                log.error("Workspace integrity: %s", e)
        component_durations["Workspace Integrity"] = time.monotonic() - t0
        _log_component_ready("Workspace Integrity", "checked")
        progress.advance()

        # Step 1: Database
        _log_component_init("Database", "started")
        t0 = time.monotonic()
        db = Database(
            data_dir=str(workspace / ".data"),
        )
        async with maybe_spinner_async("Connecting to database..."):
            await db.connect()
        component_durations["Database"] = time.monotonic() - t0
        _log_component_ready("Database", f"path={workspace / '.data'}")
        progress.advance()

        # Step 2: LLM client
        _log_component_init("LLM Client", "started")
        t0 = time.monotonic()
        token_usage = TokenUsage()
        llm = LLMClient(config.llm, log_llm=config.log_llm, token_usage=token_usage)
        component_durations["LLM Client"] = time.monotonic() - t0
        _log_component_ready("LLM Client", f"model={config.llm.model}")
        progress.advance()

        # Step 3: Memory system
        _log_component_init("Memory", "started")
        t0 = time.monotonic()
        memory = Memory(WORKSPACE_DIR)
        component_durations["Memory"] = time.monotonic() - t0
        _log_component_ready("Memory", f"workspace={WORKSPACE_DIR}")
        progress.advance()

        # Step 4: Vector memory (sqlite-vec) — graceful degradation on failure
        _log_component_init("Vector Memory", "started")
        t0 = time.monotonic()
        vector_memory: Optional[VectorMemory] = None
        try:
            vm = VectorMemory(
                db_path=str(workspace / ".data" / "vector_memory.db"),
                openai_client=llm._client,
                embedding_model=config.llm.embedding_model,
                embedding_dimensions=config.llm.embedding_dimensions,
            )
            vm.connect()
            # Validate embedding model is reachable before declaring ready
            async with maybe_spinner_async("Probing embedding model..."):
                probe_ok, probe_msg = await vm.probe_embedding_model()
            if not probe_ok:
                try:
                    vm.close()
                except Exception:
                    pass
                raise RuntimeError(f"Embedding model unavailable: {probe_msg}")
            vector_memory = vm
            _log_component_ready(
                "Vector Memory",
                f"model={config.llm.embedding_model}, {probe_msg}",
            )
        except Exception as exc:
            log.warning(
                "Vector Memory initialization failed — running in degraded mode "
                "(memory VSS skills disabled): %s: %s",
                type(exc).__name__,
                exc,
            )
            _log_component_ready(
                "Vector Memory",
                "DEGRADED — unavailable (memory VSS skills disabled)",
            )
        component_durations["Vector Memory"] = time.monotonic() - t0
        progress.advance()

        # Step 5: Project store
        _log_component_init("Project Store", "started")
        t0 = time.monotonic()
        project_store = ProjectStore(
            db_path=str(workspace / ".data" / "projects.db"),
        )
        project_store.connect()
        component_durations["Project Store"] = time.monotonic() - t0
        _log_component_ready("Project Store", f"path={workspace / '.data' / 'projects.db'}")
        progress.advance()

        # Step 6: Message queue (persistent queue for crash recovery)
        _log_component_init("Message Queue", "started")
        t0 = time.monotonic()
        message_queue = MessageQueue(str(workspace / ".data"))
        await message_queue.connect()
        pending_count = await message_queue.get_pending_count()
        component_durations["Message Queue"] = time.monotonic() - t0
        _log_component_ready(
            "Message Queue",
            f"path={workspace / '.data' / 'message_queue.jsonl'}, pending={pending_count}",
        )
        progress.advance()

        # Step 7: Routing engine (scans .md instruction files for frontmatter)
        _log_component_init("Routing Engine", "started")
        t0 = time.monotonic()
        instructions_dir = workspace / "instructions"
        routing = RoutingEngine(instructions_dir)
        async with maybe_spinner_async("Loading routing rules..."):
            routing.load_rules()
        rules_count = len(routing.rules)
        component_durations["Routing Engine"] = time.monotonic() - t0

        _log_component_ready("Routing Engine", f"rules={rules_count}")
        progress.advance()

        # Step 7b: Create shared ProjectContextLoader (before skills so they can share graph/recall)
        from src.core.instruction_loader import InstructionLoader
        from src.core.project_context import ProjectContextLoader

        project_ctx = ProjectContextLoader(project_store)
        instruction_loader = InstructionLoader(instructions_dir)

        # Step 8: Skills registry
        _log_component_init("Skills Registry", "started")
        t0 = time.monotonic()
        skills = SkillRegistry()
        async with maybe_spinner_async("Loading built-in skills..."):
            skills.load_builtins(
                db=db,
                vector_memory=vector_memory,
                project_store=project_store,
                project_ctx=project_ctx,
                routing_engine=routing,
                instruction_loader=instruction_loader,
                shell_config=config.shell,
            )
        builtin_count = len(skills.all())

        if config.skills_auto_load:
            async with maybe_spinner_async("Loading user skills..."):
                skills.load_user_skills(config.skills_user_directory)
            user_count = len(skills.all()) - builtin_count
            _log_component_ready("Skills Registry", f"builtin={builtin_count}, user={user_count}")
        else:
            _log_component_ready(
                "Skills Registry",
                f"builtin={builtin_count}, user=0 (auto_load disabled)",
            )
        component_durations["Skills Registry"] = time.monotonic() - t0

        _log_skills_loaded(skills)
        progress.advance()

        # Wire LLM into skills that need it (e.g. PromptSkill)
        skills.wire_llm_clients(llm)

        # Step 9: Bot orchestrator
        _log_component_init("Bot", "started")
        t0 = time.monotonic()
        bot_config = BotConfig(
            max_tool_iterations=config.llm.max_tool_iterations,
            memory_max_history=config.memory_max_history,
            system_prompt_prefix=config.llm.system_prompt_prefix,
            stream_response=config.llm.stream_response,
        )
        bot = Bot(
            config=bot_config,
            db=db,
            llm=llm,
            memory=memory,
            skills=skills,
            routing=routing,
            project_store=project_store,
            project_ctx=project_ctx,
            instructions_dir=str(workspace / "instructions"),
            message_queue=message_queue,
            session_metrics=session_metrics,
            instruction_loader=instruction_loader,
        )
        component_durations["Bot"] = time.monotonic() - t0
        _log_component_ready("Bot", "orchestrator initialized")
        progress.advance()

    return BotComponents(
        bot=bot,
        db=db,
        vector_memory=vector_memory,
        project_store=project_store,
        token_usage=token_usage,
        message_queue=message_queue,
        llm=llm,
        component_durations=component_durations,
    )
