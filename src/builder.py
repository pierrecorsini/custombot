"""
src/builder.py — Bot component builder with progress indicators.

Instantiates and wires all bot components: Database, LLM, Memory,
Routing, Skills, and the Bot orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from src.config import Config
from src.constants import WORKSPACE_DIR
from src.lifecycle import (
    _log_component_init,
    _log_component_ready,
    _log_skills_loaded,
)
from src.progress import ProgressBar, maybe_spinner_async


async def _build_bot(config: Config):
    """Instantiate and wire all components with progress indicators."""
    from src.db import Database
    from src.llm import LLMClient
    from src.memory import Memory
    from src.routing import RoutingEngine
    from src.bot import Bot
    from src.skills import SkillRegistry
    from src.vector_memory import VectorMemory
    from src.project.store import ProjectStore

    workspace = Path(WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)

    total_steps = 8

    with ProgressBar("Initializing components", total=total_steps) as progress:
        # Step 1: Database
        _log_component_init("Database", "started")
        db = Database(
            data_dir=str(workspace / ".data"),
        )
        async with maybe_spinner_async("Connecting to database..."):
            await db.connect()
        _log_component_ready("Database", f"path={workspace / '.data'}")
        progress.advance()

        # Step 2: LLM client
        _log_component_init("LLM Client", "started")
        llm = LLMClient(config.llm, log_llm=config.log_llm)
        _log_component_ready("LLM Client", f"model={config.llm.model}")
        progress.advance()

        # Step 3: Memory system
        _log_component_init("Memory", "started")
        memory = Memory(WORKSPACE_DIR)
        _log_component_ready("Memory", f"workspace={WORKSPACE_DIR}")
        progress.advance()

        # Step 4: Vector memory (sqlite-vec)
        _log_component_init("Vector Memory", "started")
        vector_memory = VectorMemory(
            db_path=str(workspace / ".data" / "vector_memory.db"),
            openai_client=llm._client,
            embedding_model=config.llm.embedding_model,
            embedding_dimensions=config.llm.embedding_dimensions,
        )
        vector_memory.connect()
        _log_component_ready(
            "Vector Memory",
            f"model={config.llm.embedding_model}, dims={config.llm.embedding_dimensions}",
        )
        progress.advance()

        # Step 5: Project store
        _log_component_init("Project Store", "started")
        project_store = ProjectStore(
            db_path=str(workspace / ".data" / "projects.db"),
        )
        project_store.connect()
        _log_component_ready(
            "Project Store", f"path={workspace / '.data' / 'projects.db'}"
        )
        progress.advance()

        # Step 6: Routing engine (scans .md instruction files for frontmatter)
        _log_component_init("Routing Engine", "started")
        instructions_dir = workspace / "instructions"
        routing = RoutingEngine(instructions_dir)
        async with maybe_spinner_async("Loading routing rules..."):
            routing.load_rules()
        rules_count = len(routing.rules)

        _log_component_ready("Routing Engine", f"rules={rules_count}")
        progress.advance()

        # Step 6b: Create shared ProjectContextLoader (before skills so they can share graph/recall)
        from src.core.project_context import ProjectContextLoader
        from src.core.instruction_loader import InstructionLoader

        project_ctx = ProjectContextLoader(project_store)
        instruction_loader = InstructionLoader(instructions_dir)

        # Step 7: Skills registry
        _log_component_init("Skills Registry", "started")
        skills = SkillRegistry()
        async with maybe_spinner_async("Loading built-in skills..."):
            skills.load_builtins(
                db=db,
                vector_memory=vector_memory,
                project_store=project_store,
                project_ctx=project_ctx,
                routing_engine=routing,
                instruction_loader=instruction_loader,
            )
        builtin_count = len(skills.all())

        if config.skills_auto_load:
            async with maybe_spinner_async("Loading user skills..."):
                skills.load_user_skills(config.skills_user_directory)
            user_count = len(skills.all()) - builtin_count
            _log_component_ready(
                "Skills Registry", f"builtin={builtin_count}, user={user_count}"
            )
        else:
            _log_component_ready(
                "Skills Registry",
                f"builtin={builtin_count}, user=0 (auto_load disabled)",
            )

        _log_skills_loaded(skills)
        progress.advance()

        # Inject LLM into prompt skills
        from src.skills.prompt_skill import PromptSkill

        for skill in skills.all():
            if isinstance(skill, PromptSkill):
                skill.set_llm(llm)

        # Step 8: Bot orchestrator
        _log_component_init("Bot", "started")
        bot = Bot(
            config=config,
            db=db,
            llm=llm,
            memory=memory,
            skills=skills,
            routing=routing,
            project_store=project_store,
            project_ctx=project_ctx,
            instructions_dir=str(workspace / "instructions"),
        )
        _log_component_ready("Bot", "orchestrator initialized")
        progress.advance()

    return bot, db, vector_memory, project_store
