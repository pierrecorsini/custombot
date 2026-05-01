"""
src/builder.py — Bot component builder with progress indicators.

Instantiates and wires all bot components: Database, LLM, Memory,
Routing, Skills, and the Bot orchestrator.

Uses a declarative component registry mirroring ``StartupOrchestrator``
(in ``src/core/startup.py``): each component is described by a
``BuilderComponentSpec`` (name, factory callable, optional dependencies)
and the ``BuilderOrchestrator`` executes them in dependency order,
handling logging, timing, and progress-bar advancement.

Usage::

    from src.builder import _build_bot, BotComponents

    components: BotComponents = await _build_bot(config, session_metrics=metrics)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, Sequence

from src.config import Config
from src.constants import WORKSPACE_DIR
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.orchestrator import StepOrchestrator
from src.lifecycle import (
    _log_skills_loaded,
)
from src.progress import ProgressBar, maybe_spinner_async
from src.security.url_sanitizer import sanitize_url_for_logging

if TYPE_CHECKING:
    from src.bot import Bot
    from src.core.dedup import DeduplicationService
    from src.core.instruction_loader import InstructionLoader
    from src.core.project_context import ProjectContextLoader
    from src.db import Database
    from src.llm_provider import LLMProvider, TokenUsage
    from src.memory import Memory
    from src.message_queue import MessageQueue
    from src.monitoring.performance import SessionMetrics
    from src.project.store import ProjectStore
    from src.routing import RoutingEngine
    from src.skills import SkillRegistry
    from src.vector_memory import VectorMemory

log = logging.getLogger(__name__)


# ── Infrastructure ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class BotComponents:
    """Named container for the components returned by _build_bot()."""

    bot: Bot
    db: Database
    vector_memory: Optional[VectorMemory]
    project_store: ProjectStore
    token_usage: TokenUsage
    message_queue: MessageQueue
    llm: LLMProvider
    dedup: DeduplicationService
    component_durations: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class BuilderContext:
    """Mutable state bag shared across all builder steps.

    Each step reads from and writes to this object.  Fields start as
    ``None`` and are populated as steps execute.
    """

    config: Config
    workspace: Path
    session_metrics: SessionMetrics | None = None

    # Populated by steps
    db: Database | None = None
    dedup: DeduplicationService | None = None
    token_usage: TokenUsage | None = None
    llm: LLMProvider | None = None
    memory: Memory | None = None
    vector_memory: VectorMemory | None = None
    project_store: ProjectStore | None = None
    message_queue: MessageQueue | None = None
    routing: RoutingEngine | None = None
    project_ctx: ProjectContextLoader | None = None
    instruction_loader: InstructionLoader | None = None
    skills: SkillRegistry | None = None
    bot: Bot | None = None

    # Tracking
    component_durations: dict[str, float] = field(default_factory=dict)

    def to_bot_components(self) -> BotComponents:
        """Build the immutable result from the populated state."""
        self._validate_required()
        # Asserts for type-narrowing (validated above).
        assert self.bot is not None
        assert self.db is not None
        assert self.project_store is not None
        assert self.token_usage is not None
        assert self.message_queue is not None
        assert self.llm is not None
        assert self.dedup is not None
        return BotComponents(
            bot=self.bot,
            db=self.db,
            vector_memory=self.vector_memory,
            project_store=self.project_store,
            token_usage=self.token_usage,
            message_queue=self.message_queue,
            llm=self.llm,
            dedup=self.dedup,
            component_durations=self.component_durations,
        )

    def _validate_required(self) -> None:
        """Raise if any required component was not populated by a step."""
        missing = [
            name
            for name, val in (
                ("bot", self.bot),
                ("db", self.db),
                ("project_store", self.project_store),
                ("token_usage", self.token_usage),
                ("message_queue", self.message_queue),
                ("llm", self.llm),
                ("dedup", self.dedup),
            )
            if val is None
        ]
        if missing:
            raise RuntimeError(
                f"BuilderContext incomplete — step(s) did not populate: "
                f"{', '.join(missing)}"
            )


class BuilderStepFactory(Protocol):
    """Protocol for a factory that initialises one builder component."""

    async def __call__(self, ctx: BuilderContext) -> str | None:
        """Execute the builder step.

        Returns an optional detail string for the "READY" log line
        (e.g. ``"rules=5"``).  Return ``None`` for no detail.
        """
        ...


@dataclass(slots=True, frozen=True)
class BuilderComponentSpec:
    """Declarative description of a single builder step.

    Attributes:
        name: Human-readable name used in log lines and tracking.
        factory: Async callable that receives ``BuilderContext`` and returns
                 an optional detail string for the ready-log.
        depends_on: Names of steps that must complete before this step runs.
                    The orchestrator resolves execution order via topological
                    sort.  Empty (default) means no prerequisites.
    """

    name: str
    factory: BuilderStepFactory
    depends_on: Sequence[str] = ()


# ── Step implementations ────────────────────────────────────────────────


async def _step_workspace_integrity(ctx: BuilderContext) -> str | None:
    """Check and auto-repair workspace integrity."""
    from src.workspace_integrity import check_workspace_integrity

    integrity = await check_workspace_integrity(ctx.workspace)
    if integrity.repaired:
        log.info("Workspace integrity: auto-repaired %s", integrity.repaired)
    if integrity.warnings:
        for w in integrity.warnings:
            log.warning("Workspace integrity: %s", w)
    if integrity.errors:
        for e in integrity.errors:
            log.error("Workspace integrity: %s", e)
    return "checked"


async def _step_database(ctx: BuilderContext) -> str | None:
    """Create and connect the Database; wire DeduplicationService."""
    from src.core.dedup import DeduplicationService
    from src.db import Database

    db = Database(data_dir=str(ctx.workspace / ".data"))
    async with maybe_spinner_async("Connecting to database..."):
        await db.connect()
    ctx.db = db
    # Wire dedup service (needs DB for inbound checks)
    ctx.dedup = DeduplicationService(db=db)
    return f"path={ctx.workspace / '.data'}"


async def _step_llm_client(ctx: BuilderContext) -> str | None:
    """Create the LLM client and token-usage tracker."""
    from src.llm import LLMClient
    from src.llm_provider import TokenUsage

    token_usage = TokenUsage()
    llm = LLMClient(ctx.config.llm, log_llm=ctx.config.log_llm, token_usage=token_usage)
    ctx.token_usage = token_usage
    ctx.llm = llm
    return (
        f"model={ctx.config.llm.model}, "
        f"base_url={sanitize_url_for_logging(ctx.config.llm.base_url)}"
    )


async def _step_memory(ctx: BuilderContext) -> str | None:
    """Create the Memory system."""
    from src.memory import Memory

    ctx.memory = Memory(WORKSPACE_DIR)
    return f"workspace={WORKSPACE_DIR}"


async def _step_vector_memory(ctx: BuilderContext) -> str | None:
    """Create VectorMemory with graceful degradation on failure."""
    import httpx
    from openai import AsyncOpenAI
    from src.vector_memory import VectorMemory

    try:
        embed_cfg = ctx.config.llm
        embed_base_url = embed_cfg.embedding_base_url or embed_cfg.base_url
        embed_api_key = embed_cfg.embedding_api_key or embed_cfg.api_key

        if embed_cfg.embedding_base_url:
            embed_http = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                timeout=httpx.Timeout(timeout=30.0, connect=10.0),
            )
            embed_client = AsyncOpenAI(
                api_key=embed_api_key or "not-configured",
                base_url=embed_base_url,
                http_client=embed_http,
            )
            embed_source = f"dedicated ({embed_cfg.embedding_base_url})"
        else:
            embed_client = ctx.llm.openai_client  # type: ignore[union-attr]
            embed_source = "shared with LLM"

        vm = VectorMemory(
            db_path=str(ctx.workspace / ".data" / "vector_memory.db"),
            openai_client=embed_client,
            embedding_model=ctx.config.llm.embedding_model,
            embedding_dimensions=ctx.config.llm.embedding_dimensions,
        )
        vm.connect()
        # Validate embedding model is reachable before declaring ready
        async with maybe_spinner_async("Probing embedding model..."):
            probe_ok, probe_msg = await vm.probe_embedding_model()
        if not probe_ok:
            try:
                vm.close()
            except Exception:
                log_noncritical(
                    NonCriticalCategory.CLEANUP,
                    "Failed to close VectorMemory during startup probe cleanup",
                    logger=log,
                )
            raise RuntimeError(f"Embedding model unavailable: {probe_msg}")
        ctx.vector_memory = vm
        # Wire vector memory into DB for embedding compression summaries
        ctx.db.set_vector_memory(vm)  # type: ignore[union-attr]
        return f"model={ctx.config.llm.embedding_model}, {probe_msg} [{embed_source}]"
    except Exception as exc:
        log.warning(
            "Vector Memory initialization failed — running in degraded mode "
            "(memory VSS skills disabled): %s: %s",
            type(exc).__name__,
            exc,
        )
        ctx.vector_memory = None
        return "DEGRADED — unavailable (memory VSS skills disabled)"


async def _step_project_store(ctx: BuilderContext) -> str | None:
    """Create and connect the ProjectStore."""
    from src.project.store import ProjectStore

    project_store = ProjectStore(
        db_path=str(ctx.workspace / ".data" / "projects.db"),
    )
    project_store.connect()
    ctx.project_store = project_store
    return f"path={ctx.workspace / '.data' / 'projects.db'}"


async def _step_message_queue(ctx: BuilderContext) -> str | None:
    """Create and connect the persistent message queue."""
    from src.message_queue import MessageQueue

    mq = MessageQueue(str(ctx.workspace / ".data"))
    await mq.connect()
    pending_count = await mq.get_pending_count()
    ctx.message_queue = mq
    return f"path={ctx.workspace / '.data' / 'message_queue.jsonl'}, pending={pending_count}"


async def _step_routing(ctx: BuilderContext) -> str | None:
    """Create the RoutingEngine and shared context loaders."""
    from src.core.instruction_loader import InstructionLoader
    from src.core.project_context import ProjectContextLoader
    from src.routing import RoutingEngine

    instructions_dir = ctx.workspace / "instructions"
    routing = RoutingEngine(instructions_dir)
    async with maybe_spinner_async("Loading routing rules..."):
        routing.load_rules()
    ctx.routing = routing
    # Create shared ProjectContextLoader and InstructionLoader
    ctx.project_ctx = ProjectContextLoader(ctx.project_store)  # type: ignore[arg-type]
    ctx.instruction_loader = InstructionLoader(instructions_dir)

    if len(routing.rules) == 0:
        if not instructions_dir.is_dir():
            log.warning(
                "Instructions directory %s does not exist — "
                "no routing rules loaded. Messages will be ignored "
                "until rules are added. Create the directory and "
                "add at least a default 'chat.agent.md' with routing frontmatter. "
                "See src/templates/instructions/ for examples.",
                instructions_dir,
            )
        else:
            log.warning(
                "No routing rules loaded from %s — "
                "messages will be ignored until rules are added. "
                "Add at least a 'chat.agent.md' with YAML routing frontmatter. "
                "See src/templates/instructions/ for examples.",
                instructions_dir,
            )

    return f"rules={len(routing.rules)}"


async def _step_skills(ctx: BuilderContext) -> str | None:
    """Create the SkillRegistry, load builtins (and optional user skills)."""
    from src.skills import SkillRegistry

    skills = SkillRegistry()
    async with maybe_spinner_async("Loading built-in skills..."):
        skills.load_builtins(
            db=ctx.db,  # type: ignore[arg-type]
            vector_memory=ctx.vector_memory,
            project_store=ctx.project_store,  # type: ignore[arg-type]
            project_ctx=ctx.project_ctx,  # type: ignore[arg-type]
            routing_engine=ctx.routing,  # type: ignore[arg-type]
            instruction_loader=ctx.instruction_loader,  # type: ignore[arg-type]
            shell_config=ctx.config.shell,
        )
    builtin_count = len(skills.all())

    if ctx.config.skills_auto_load:
        async with maybe_spinner_async("Loading user skills..."):
            skills.load_user_skills(ctx.config.skills_user_directory)
        user_count = len(skills.all()) - builtin_count
        detail = f"builtin={builtin_count}, user={user_count}"
    else:
        detail = f"builtin={builtin_count}, user=0 (auto_load disabled)"

    ctx.skills = skills
    # Wire LLM into skills that need it (e.g. PromptSkill)
    skills.wire_llm_clients(ctx.llm)  # type: ignore[arg-type]
    _log_skills_loaded(skills)
    return detail


async def _step_bot(ctx: BuilderContext) -> str | None:
    """Create the Bot orchestrator."""
    from src.bot import Bot, BotConfig

    bot_config = BotConfig(
        max_tool_iterations=ctx.config.llm.max_tool_iterations,
        memory_max_history=ctx.config.memory_max_history,
        system_prompt_prefix=ctx.config.llm.system_prompt_prefix,
        stream_response=ctx.config.llm.stream_response,
    )
    from src.utils import LRULockCache
    from src.constants import EvictionPolicy

    eviction_policy = EvictionPolicy(ctx.config.max_chat_lock_eviction_policy)
    chat_locks = LRULockCache(
        max_size=ctx.config.max_chat_lock_cache_size,
        eviction_policy=eviction_policy,
    )
    bot = Bot(
        config=bot_config,
        db=ctx.db,  # type: ignore[arg-type]
        llm=ctx.llm,  # type: ignore[arg-type]
        memory=ctx.memory,  # type: ignore[arg-type]
        skills=ctx.skills,  # type: ignore[arg-type]
        routing=ctx.routing,  # type: ignore[arg-type]
        project_store=ctx.project_store,  # type: ignore[arg-type]
        project_ctx=ctx.project_ctx,  # type: ignore[arg-type]
        instructions_dir=str(ctx.workspace / "instructions"),
        message_queue=ctx.message_queue,  # type: ignore[arg-type]
        session_metrics=ctx.session_metrics,
        instruction_loader=ctx.instruction_loader,  # type: ignore[arg-type]
        dedup=ctx.dedup,  # type: ignore[arg-type]
        chat_locks=chat_locks,
    )
    ctx.bot = bot
    return "orchestrator initialized"


# ── Default step registry ───────────────────────────────────────────────


DEFAULT_BUILDER_STEPS: list[BuilderComponentSpec] = [
    BuilderComponentSpec(name="Workspace Integrity", factory=_step_workspace_integrity),
    BuilderComponentSpec(name="Database", factory=_step_database),
    BuilderComponentSpec(
        name="LLM Client",
        factory=_step_llm_client,
    ),
    BuilderComponentSpec(name="Memory", factory=_step_memory),
    BuilderComponentSpec(
        name="Vector Memory",
        factory=_step_vector_memory,
        depends_on=("Database", "LLM Client"),
    ),
    BuilderComponentSpec(
        name="Project Store",
        factory=_step_project_store,
    ),
    BuilderComponentSpec(
        name="Message Queue",
        factory=_step_message_queue,
    ),
    BuilderComponentSpec(
        name="Routing Engine",
        factory=_step_routing,
        depends_on=("Project Store",),
    ),
    BuilderComponentSpec(
        name="Skills Registry",
        factory=_step_skills,
        depends_on=(
            "Database",
            "Vector Memory",
            "Project Store",
            "Routing Engine",
            "LLM Client",
        ),
    ),
    BuilderComponentSpec(
        name="Bot",
        factory=_step_bot,
        depends_on=(
            "Skills Registry",
            "Message Queue",
            "Memory",
            "Database",
            "LLM Client",
        ),
    ),
]


# ── Orchestrator ────────────────────────────────────────────────────────


class BuilderOrchestrator(StepOrchestrator[BuilderContext, BuilderComponentSpec]):
    """Execute a sequence of ``BuilderComponentSpec`` steps in order.

    Handles logging, timing, progress-bar advancement, and error
    propagation for each step.  The pattern mirrors ``StartupOrchestrator``
    in ``src/core/startup.py``.
    """

    __slots__ = ()

    def __init__(
        self,
        ctx: BuilderContext,
        steps: Sequence[BuilderComponentSpec] | None = None,
    ) -> None:
        super().__init__(
            ctx,
            steps,
            DEFAULT_BUILDER_STEPS,
            context_label="builder dependency",
        )

    async def run_all(self) -> BotComponents:
        """Run all builder steps and return the assembled ``BotComponents``.

        Steps are executed in dependency-resolved order.  On failure, the
        exception propagates and the caller is responsible for cleaning up
        any partially-initialised components.
        """
        steps = self._resolve_order()

        with ProgressBar("Initializing components", total=len(steps)) as progress:
            for spec in steps:
                await self._execute_step(spec)
                progress.advance()

        return self._ctx.to_bot_components()


# ── Public entry point ──────────────────────────────────────────────────


async def _build_bot(config: Config, session_metrics: "SessionMetrics | None" = None) -> BotComponents:
    """Instantiate and wire all components with progress indicators."""
    workspace = Path(WORKSPACE_DIR)
    workspace.mkdir(parents=True, exist_ok=True)

    ctx = BuilderContext(
        config=config,
        workspace=workspace,
        session_metrics=session_metrics,
    )
    orchestrator = BuilderOrchestrator(ctx)
    return await orchestrator.run_all()
