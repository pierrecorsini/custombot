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

    from src.builder import build_bot, BotComponents

    components: BotComponents = await build_bot(config, session_metrics=metrics)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, Protocol, Sequence

from src.constants import EMBEDDING_CONNECT_TIMEOUT, EMBEDDING_REQUEST_TIMEOUT, WORKSPACE_DIR
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.orchestrator import StepOrchestrator
from src.lifecycle import (
    _log_skills_loaded,
)
from src.progress import ProgressBar, maybe_spinner_async
from src.security.url_sanitizer import sanitize_url_for_logging
from src.utils.registry import ComponentRegistry, RegistryBackedMixin

if TYPE_CHECKING:
    from src.config import Config
    from src.bot import Bot
    from src.core.dedup import DeduplicationService
    from src.core.instruction_loader import InstructionLoader
    from src.core.project_context import ProjectContextLoader
    from src.db import Database
    from src.llm import LLMProvider, TokenUsage
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
    """Named container for the components returned by build_bot()."""

    bot: Bot
    db: Database
    vector_memory: Optional[VectorMemory]
    project_store: ProjectStore
    token_usage: TokenUsage
    message_queue: MessageQueue
    llm: LLMProvider
    dedup: DeduplicationService
    routing_engine: Optional[RoutingEngine] = None
    component_durations: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class BuilderContext(RegistryBackedMixin):
    """Mutable state bag shared across all builder steps.

    Uses a ``ComponentRegistry`` to store dynamically-populated
    components.  Required fields (``config``, ``workspace``) are
    typed dataclass fields; step-populated components (``db``,
    ``llm``, etc.) are stored in the registry and accessed via
    natural ``ctx.db`` / ``ctx.db = db`` attribute syntax.

    Call ``freeze()`` after building to obtain an immutable snapshot.
    """

    _OWN_SLOTS: ClassVar[frozenset[str]] = frozenset((
        "config", "workspace", "session_metrics",
        "component_durations", "_registry", "_frozen",
    ))

    config: Config
    workspace: Path
    session_metrics: SessionMetrics | None = None

    # Tracking
    component_durations: dict[str, float] = field(default_factory=dict)

    # Dynamic component storage — replaces per-field ``X | None = None``
    _registry: ComponentRegistry = field(default_factory=ComponentRegistry, repr=False)
    _frozen: bool = field(default=False, repr=False)

    def freeze(self) -> None:
        """Seal the context against further mutation.

        After calling ``freeze()``, any ``__setattr__`` on registry-backed
        attributes raises ``RuntimeError``.  Own slots (``config``,
        ``session_metrics``, etc.) remain mutable for safety — only the
        dynamic registry is locked.
        """
        self._frozen = True

    def __setattr__(self, name: str, value: Any) -> None:
        """Store known slots normally; everything else in the registry."""
        if name in self._OWN_SLOTS:
            object.__setattr__(self, name, value)
        else:
            if self._frozen:
                raise RuntimeError(
                    f"Cannot set '{name}' — BuilderContext is frozen. "
                    f"Call occurs after freeze()."
                )
            self._registry.register(name, value)

    def to_bot_components(self) -> BotComponents:
        """Build the immutable result from the populated registry."""
        self._registry.validate_required((
            "bot", "db", "project_store", "token_usage",
            "message_queue", "llm", "dedup",
        ))
        return BotComponents(
            bot=self._registry.require("bot"),
            db=self._registry.require("db"),
            vector_memory=self._registry.get("vector_memory"),
            project_store=self._registry.require("project_store"),
            token_usage=self._registry.require("token_usage"),
            message_queue=self._registry.require("message_queue"),
            llm=self._registry.require("llm"),
            dedup=self._registry.require("dedup"),
            routing_engine=self._registry.get("routing"),
            component_durations=self.component_durations,
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


async def _step_sqlite_pool(ctx: BuilderContext) -> str | None:
    """Initialize the shared SQLite connection pool.

    Must run before any component that uses SqliteHelper (VectorMemory,
    ProjectStore) so those components delegate connection creation to the
    pool factory for consistent WAL-mode configuration and centralized
    lifecycle management.
    """
    from src.db.sqlite_pool import SqliteConnectionPool
    from src.db.sqlite_utils import SqliteHelper

    pool = SqliteConnectionPool()
    SqliteHelper.set_pool(pool)
    return "initialized"


async def _step_database(ctx: BuilderContext) -> str | None:
    """Create and connect the Database; wire DeduplicationService."""
    from src.core.dedup import DeduplicationService
    from src.db import Database
    from src.db.event_store import EventStore

    db = Database(data_dir=str(ctx.workspace / ".data"))
    async with maybe_spinner_async("Connecting to database..."):
        await db.connect()
        # Pre-warm file handles for all known chats so crash recovery
        # doesn't pay N serialized open() syscalls on first write.
        await asyncio.to_thread(db.warm_file_handles)
    ctx.db = db
    # Wire dedup service (needs DB for inbound checks)
    ctx.dedup = DeduplicationService(db=db)

    # Wire event store if enabled in config
    event_store_enabled = getattr(ctx.config, "event_store_enabled", False)
    event_store = EventStore(
        data_dir=str(ctx.workspace / ".data"),
        enabled=event_store_enabled,
    )
    ctx.event_store = event_store

    return f"path={ctx.workspace / '.data'}"


async def _step_llm_client(ctx: BuilderContext) -> str | None:
    """Create the LLM client and token-usage tracker."""
    from src.llm import LLMClient
    from src.llm import TokenUsage

    token_usage = TokenUsage(max_per_chat_size=ctx.config.per_chat_token_tracking_size)
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

    # Typed component references (populated by upstream builder steps)
    llm: LLMProvider = ctx._registry.require("llm")
    db: Database = ctx._registry.require("db")

    embed_http: httpx.AsyncClient | None = None
    try:
        embed_cfg = ctx.config.llm
        embed_base_url = embed_cfg.embedding_base_url or embed_cfg.base_url
        embed_api_key = embed_cfg.embedding_api_key or embed_cfg.api_key

        if embed_cfg.embedding_base_url:
            # When the embedding URL matches the LLM URL, share the LLM's
            # existing httpx connection pool instead of opening a second one
            # to the same host.  Normalise trailing slashes for comparison.
            embed_url_norm = embed_cfg.embedding_base_url.rstrip("/")
            llm_url_norm = embed_cfg.base_url.rstrip("/")
            if embed_url_norm == llm_url_norm:
                embed_client = AsyncOpenAI(
                    api_key=embed_api_key or "not-configured",
                    base_url=embed_base_url,
                    http_client=llm.http_client,
                )
                embed_source = f"pooled with LLM ({embed_cfg.embedding_base_url})"
            else:
                embed_http = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=embed_cfg.embedding_max_connections,
                        max_keepalive_connections=embed_cfg.embedding_max_keepalive_connections,
                    ),
                    timeout=httpx.Timeout(timeout=EMBEDDING_REQUEST_TIMEOUT, connect=EMBEDDING_CONNECT_TIMEOUT),
                )
                embed_client = AsyncOpenAI(
                    api_key=embed_api_key or "not-configured",
                    base_url=embed_base_url,
                    http_client=embed_http,
                )
                embed_source = f"dedicated ({embed_cfg.embedding_base_url})"
        else:
            embed_client = llm.openai_client
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
        db.set_vector_memory(vm)
        embed_http = None  # ownership transferred to AsyncOpenAI / VectorMemory
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
    finally:
        if embed_http is not None:
            try:
                await embed_http.aclose()
            except Exception:
                log_noncritical(
                    NonCriticalCategory.CLEANUP,
                    "Failed to close dedicated embedding HTTP client during degradation",
                    logger=log,
                )


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
        await routing.load_rules()
    ctx.routing = routing
    # Create shared ProjectContextLoader and InstructionLoader
    project_store: ProjectStore = ctx._registry.require("project_store")
    ctx.project_ctx = ProjectContextLoader(project_store)
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

    # Typed component references (populated by upstream builder steps)
    db: Database = ctx._registry.require("db")
    project_store: ProjectStore = ctx._registry.require("project_store")
    llm: LLMProvider = ctx._registry.require("llm")

    skills = SkillRegistry()
    async with maybe_spinner_async("Loading built-in skills..."):
        skills.load_builtins(
            db=db,
            vector_memory=ctx.vector_memory,
            project_store=project_store,
            project_ctx=ctx._registry.get("project_ctx"),
            routing_engine=ctx._registry.get("routing"),
            instruction_loader=ctx._registry.get("instruction_loader"),
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
    skills.wire_llm_clients(llm)
    _log_skills_loaded(skills)
    return detail


def _build_bot_deps(ctx: BuilderContext, bot_config: BotConfig) -> Any:
    """Construct and wire all Bot collaborators into a ``BotDeps`` instance.

    This factory extracts collaborator wiring from ``_step_bot`` so the
    construction logic can be tested in isolation.  It builds:

    - ``LRULockCache`` for per-chat locks
    - ``RateLimiter`` with expensive-skill registration
    - ``ToolExecutor`` with skill registry and metrics
    - ``ContextAssembler`` with DB, memory, and project context
    - ``BotDeps`` packing all collaborators for ``Bot.__init__``

    Args:
        ctx: Populated builder context with all upstream components.
        bot_config: Resolved ``BotConfig`` derived from application config.

    Returns:
        A fully-wired ``BotDeps`` ready to pass to ``Bot(deps)``.
    """
    from src.bot import BotDeps
    from src.constants import EvictionPolicy
    from src.core.context_assembler import ContextAssembler
    from src.core.project_context import ProjectContextLoader as _ProjectContextLoaderImpl
    from src.core.tool_executor import ToolExecutor
    from src.monitoring import get_metrics_collector
    from src.rate_limiter import RateLimiter
    from src.utils import LRULockCache

    # Typed component references from the registry — direct registry access
    # bypasses __getattr__ (which returns Any | None) so mypy infers the
    # correct types from the variable annotations below.
    db: Database = ctx._registry.require("db")
    llm: LLMProvider = ctx._registry.require("llm")
    memory: Memory = ctx._registry.require("memory")
    skills: SkillRegistry = ctx._registry.require("skills")
    routing: RoutingEngine | None = ctx._registry.get("routing")
    project_store: ProjectStore = ctx._registry.require("project_store")
    message_queue: MessageQueue = ctx._registry.require("message_queue")
    dedup: DeduplicationService = ctx._registry.require("dedup")
    project_ctx: ProjectContextLoader | None = ctx._registry.get("project_ctx")
    instruction_loader: InstructionLoader | None = ctx._registry.get("instruction_loader")
    session_metrics = ctx.session_metrics  # proper dataclass field

    eviction_policy = EvictionPolicy(ctx.config.max_chat_lock_eviction_policy)
    chat_locks = LRULockCache(
        max_size=ctx.config.max_chat_lock_cache_size,
        eviction_policy=eviction_policy,
        ttl=ctx.config.max_chat_lock_cache_ttl,
    )

    rate_limiter = RateLimiter()
    for skill in skills.all():
        if skill.expensive:
            rate_limiter.register_expensive_skill(skill.name)

    metrics = get_metrics_collector()
    project_ctx_resolved = project_ctx or _ProjectContextLoaderImpl(project_store)

    tool_executor = ToolExecutor(
        skills_registry=skills,
        rate_limiter=rate_limiter,
        metrics=metrics,
        on_skill_executed=session_metrics.increment_skills
        if session_metrics
        else None,
        audit_log_dir=Path(WORKSPACE_DIR) / "logs",
    )
    context_assembler = ContextAssembler(
        db=db,
        config=bot_config,
        memory=memory,
        project_ctx=project_ctx_resolved,
        workspace_root=WORKSPACE_DIR,
    )

    return BotDeps(
        config=bot_config,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        project_store=project_store,
        project_ctx=project_ctx,
        instructions_dir=str(ctx.workspace / "instructions"),
        message_queue=message_queue,
        session_metrics=session_metrics,
        instruction_loader=instruction_loader,
        dedup=dedup,
        chat_locks=chat_locks,
        rate_limiter=rate_limiter,
        tool_executor=tool_executor,
        context_assembler=context_assembler,
    )


async def _step_bot(ctx: BuilderContext) -> str | None:
    """Create the Bot orchestrator."""
    from src.bot import Bot, BotConfig

    bot_config = BotConfig(
        max_tool_iterations=ctx.config.llm.max_tool_iterations,
        memory_max_history=ctx.config.memory_max_history,
        system_prompt_prefix=ctx.config.llm.system_prompt_prefix,
        stream_response=ctx.config.llm.stream_response,
        per_chat_timeout=ctx.config.per_chat_timeout,
        react_loop_timeout=ctx.config.react_loop_timeout,
        max_concurrent_messages=ctx.config.max_concurrent_messages,
    )
    deps = _build_bot_deps(ctx, bot_config)
    bot = Bot(deps)
    ctx.bot = bot
    return "orchestrator initialized"


# ── Default step registry ───────────────────────────────────────────────


DEFAULT_BUILDER_STEPS: list[BuilderComponentSpec] = [
    BuilderComponentSpec(name="Workspace Integrity", factory=_step_workspace_integrity),
    BuilderComponentSpec(name="SQLite Pool", factory=_step_sqlite_pool),
    BuilderComponentSpec(name="Database", factory=_step_database),
    BuilderComponentSpec(
        name="LLM Client",
        factory=_step_llm_client,
    ),
    BuilderComponentSpec(name="Memory", factory=_step_memory),
    BuilderComponentSpec(
        name="Vector Memory",
        factory=_step_vector_memory,
        depends_on=("SQLite Pool", "Database", "LLM Client"),
    ),
    BuilderComponentSpec(
        name="Project Store",
        factory=_step_project_store,
        depends_on=("SQLite Pool",),
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

        # Freeze the context to prevent post-build mutation
        self._ctx.freeze()

        return self._ctx.to_bot_components()


# ── Public entry point ──────────────────────────────────────────────────


async def build_bot(
    config: Config, session_metrics: "SessionMetrics | None" = None
) -> BotComponents:
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
