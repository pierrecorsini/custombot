"""
bench_regression.py — Benchmark regression tests for critical hot paths.

Uses ``pytest-benchmark`` fixtures with calibrated warmup and round counts.
Each test measures a production-critical code path and can be compared
against a stored baseline via ``--benchmark-autosave`` and
``--benchmark-compare-fail=mean:10%`` in CI.

Covered hot paths:
    1. Routing rule matching  (sync, regex + cache)
    2. Embedding cache lookup (async, xxhash + LRU dict)
    3. JSONL message write    (async, file I/O)
    4. Context assembly       (async, concurrent reads + token budgeting)

Run locally:
    python -m pytest tests/unit/bench_regression.py -v --benchmark-only

Run with baseline comparison:
    python -m pytest tests/unit/bench_regression.py --benchmark-autosave
    python -m pytest tests/unit/bench_regression.py --benchmark-compare=0001 \\
        --benchmark-compare-fail=mean:10%
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.channels.base import IncomingMessage
from src.routing import MatchingContext, RoutingEngine, RoutingRule

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CHAT_ID = "1234567890@s.whatsapp.net"
_SENDER_ID = "1234567890"
_MEDIUM_TEXT = "Hello, can you help me with something today?"
_LONG_TEXT = "This is a longer message with more content " * 20


def _make_msg(
    text: str = _MEDIUM_TEXT,
    chat_id: str = _CHAT_ID,
    sender_id: str = _SENDER_ID,
    from_me: bool = False,
    to_me: bool = True,
) -> IncomingMessage:
    return IncomingMessage(
        message_id="bench-msg-001",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="BenchUser",
        text=text,
        timestamp=time.time(),
        channel_type="whatsapp",
        fromMe=from_me,
        toMe=to_me,
    )


# ===================================================================
# 1. ROUTING RULE MATCHING
# ===================================================================


@pytest.fixture()
def routing_engine(tmp_path: Path) -> RoutingEngine:
    """RoutingEngine with pre-loaded rules, no file scanning."""
    engine = RoutingEngine(tmp_path / "instructions", use_watchdog=False)

    rules = [
        # Wildcard catch-all (lowest priority)
        RoutingRule(
            id="catch-all",
            priority=100,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.md",
        ),
        # Channel-specific (medium priority)
        RoutingRule(
            id="whatsapp-rule",
            priority=10,
            sender="*",
            recipient="*",
            channel="whatsapp",
            content_regex="*",
            instruction="whatsapp.md",
        ),
        # Regex content match (high priority)
        RoutingRule(
            id="greeting",
            priority=0,
            sender="*",
            recipient="*",
            channel="whatsapp",
            content_regex="hello|hi|hey.*",
            instruction="greeting.md",
        ),
        # Specific sender (very high priority)
        RoutingRule(
            id="admin",
            priority=-1,
            sender="9999999999",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="admin.md",
        ),
    ]
    engine._rules_list = rules
    # Prevent stale-check scanning
    engine._last_stale_check = time.monotonic()
    engine._file_mtimes = {}
    return engine


@pytest.mark.benchmark(group="routing", min_rounds=50, disable_gc=True)
class TestRoutingMatch:
    """Benchmark routing rule matching at various cache states."""

    def test_match_wildcard_hit(self, benchmark: Any, routing_engine: RoutingEngine) -> None:
        msg = _make_msg(text="random message")

        @benchmark
        def _():
            routing_engine.match_with_rule(msg)

    def test_match_regex_hit(self, benchmark: Any, routing_engine: RoutingEngine) -> None:
        msg = _make_msg(text="Hello, how are you?")

        @benchmark
        def _():
            routing_engine.match_with_rule(msg)

    def test_match_cache_hit(self, benchmark: Any, routing_engine: RoutingEngine) -> None:
        msg = _make_msg(text="cached message")
        # Prime the cache
        routing_engine.match_with_rule(msg)

        @benchmark
        def _():
            routing_engine.match_with_rule(msg)

    def test_match_no_match(self, benchmark: Any, routing_engine: RoutingEngine) -> None:
        """No rule matches — engine evaluates all rules and returns None."""
        # Clear rules so nothing matches
        routing_engine._rules_list = []
        msg = _make_msg()

        @benchmark
        def _():
            routing_engine.match_with_rule(msg)


# ===================================================================
# 2. EMBEDDING CACHE LOOKUP
# ===================================================================


@pytest.fixture()
def vector_memory_with_cache():
    """VectorMemory with pre-populated embedding cache, no real API calls."""
    from src.utils import BoundedOrderedDict
    from src.vector_memory._utils import _cache_key

    mock_client = AsyncMock()
    # Create a VectorMemory without calling __init__ fully (avoid SQLite)
    vm = object.__new__(VectorMemory)
    vm._embed_cache: BoundedOrderedDict[str, list[float]] = BoundedOrderedDict(
        max_size=256, eviction="half",
    )
    vm._cache_lock = _new_thread_lock()
    vm._inflight: dict[str, asyncio.Future[list[float]]] = {}
    vm._client = mock_client
    vm._embedding_model = "text-embedding-3-small"
    vm._dimensions = 1536

    # Pre-populate cache with 128 entries (half-full)
    fake_embedding = [0.1] * 1536
    for i in range(128):
        key = _cache_key(f"cached text {i}")
        vm._embed_cache[key] = fake_embedding

    return vm, _cache_key


def _new_thread_lock():
    """Create a ThreadLock without importing the full module at module scope."""
    from src.utils.locking import ThreadLock
    return ThreadLock()


# Import VectorMemory after module-level definitions to avoid circular issues
from src.vector_memory import VectorMemory  # noqa: E402


@pytest.mark.benchmark(group="embedding-cache", min_rounds=50, disable_gc=True)
class TestEmbeddingCache:
    """Benchmark embedding cache key computation and lookup."""

    def test_cache_key_hash(self, benchmark: Any) -> None:
        from src.vector_memory._utils import _cache_key

        text = _MEDIUM_TEXT

        @benchmark
        def _():
            _cache_key(text)

    def test_cache_hit(self, benchmark: Any, vector_memory_with_cache: Any) -> None:
        vm, _cache_key_fn = vector_memory_with_cache
        cached_text = "cached text 42"
        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await vm._embed(cached_text)

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_cache_miss_key_lookup(self, benchmark: Any, vector_memory_with_cache: Any) -> None:
        """Benchmark the cache-miss path up to the point where API call would happen.

        We cannot easily benchmark a full cache miss without an API call,
        so this measures the key computation + dict lookup overhead.
        """
        vm, _cache_key_fn = vector_memory_with_cache
        text = _LONG_TEXT

        @benchmark
        def _():
            key = _cache_key_fn(text)
            with vm._cache_lock:
                _ = key in vm._embed_cache


# ===================================================================
# 3. JSONL MESSAGE WRITE
# ===================================================================


@pytest.fixture()
def message_store(tmp_path: Path):
    """MessageStore with real file I/O but no-op injected callables."""
    from src.db.file_pool import FileHandlePool, ReadHandlePool
    from src.utils import LRUDict, LRULockCache

    msgs_dir = tmp_path / "messages"
    msgs_dir.mkdir()

    async def _noop_guarded_write(fn, timeout, operation):
        await fn()

    async def _noop_run_with_timeout(coro, timeout, operation):
        return await coro

    from src.db.message_store import MessageStore

    store = MessageStore(
        messages_dir=msgs_dir,
        index_file=tmp_path / "index.json",
        file_pool=FileHandlePool(),
        read_pool=ReadHandlePool(),
        message_locks=LRULockCache(),
        message_file_cache=LRUDict(),
        check_disk_space_fn=lambda p: None,
        guarded_write_fn=_noop_guarded_write,
        run_with_timeout_fn=_noop_run_with_timeout,
        atomic_write_fn=lambda p, s: None,
    )
    return store


@pytest.mark.benchmark(group="jsonl-write", min_rounds=20, disable_gc=True)
class TestJsonlWrite:
    """Benchmark JSONL message persistence."""

    def test_save_assistant_message(self, benchmark: Any, message_store: Any) -> None:
        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await message_store.save_message(
                    _CHAT_ID, "assistant", _MEDIUM_TEXT,
                )

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_save_large_message(self, benchmark: Any, message_store: Any) -> None:
        large_content = "x" * 50_000
        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await message_store.save_message(
                    _CHAT_ID, "assistant", large_content,
                )

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_build_message_record(self, benchmark: Any) -> None:
        """Benchmark the pure record-building path (no I/O)."""
        from src.db.message_store import MessageStore

        @benchmark
        def _():
            MessageStore.build_message_record("assistant", _MEDIUM_TEXT)


# ===================================================================
# 4. CONTEXT ASSEMBLY
# ===================================================================


@pytest.fixture()
def context_assembler(tmp_path: Path):
    """ContextAssembler with fully mocked dependencies."""
    from src.core.context_assembler import ContextAssembler

    # Build 20 history messages
    history = []
    for i in range(20):
        history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"Message {i}: " + "word " * 20,
            "_sanitized": True,
        })

    db = AsyncMock()
    db.get_recent_messages = AsyncMock(return_value=history)
    db.get_compressed_summary = AsyncMock(return_value=None)

    config = MagicMock()
    config.system_prompt_prefix = "You are a helpful WhatsApp assistant."
    config.memory_max_history = 50

    memory = AsyncMock()
    memory.read_memory = AsyncMock(return_value=None)
    memory.read_agents_md = AsyncMock(return_value="")

    project_ctx = AsyncMock()
    project_ctx.get = AsyncMock(return_value=None)

    # Create a temp workspace for TopicCache
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assembler = ContextAssembler(
        db=db,
        config=config,
        memory=memory,
        project_ctx=project_ctx,
        workspace_root=str(workspace),
    )
    return assembler


@pytest.mark.benchmark(group="context-assembly", min_rounds=10, disable_gc=True)
class TestContextAssembly:
    """Benchmark the full context assembly pipeline."""

    def test_assemble_basic(self, benchmark: Any, context_assembler: Any) -> None:
        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await context_assembler.assemble(
                    _CHAT_ID,
                    instruction="Be helpful and concise.",
                    rule_id="bench-rule",
                )

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_assemble_with_memory(self, benchmark: Any, context_assembler: Any) -> None:
        context_assembler._memory.read_memory = AsyncMock(
            return_value="User prefers short answers. Previously discussed Python.",
        )

        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await context_assembler.assemble(
                    _CHAT_ID,
                    instruction="Be helpful and concise.",
                    rule_id="bench-rule",
                )

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_assemble_with_topic_summary(self, benchmark: Any, context_assembler: Any) -> None:
        # Write a topic cache file so _async_topic_read returns content.
        # TopicCache stores at: workspace_root/whatsapp_data/{safe_id}/.topic_summary.md
        from src.core.topic_cache import SUMMARY_FILENAME
        from src.utils.path import sanitize_path_component

        topic_path = context_assembler._topic_cache._summary_path(_CHAT_ID)
        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text("Previous topic: Python web frameworks")

        loop = asyncio.new_event_loop()
        try:

            async def _run():
                return await context_assembler.assemble(
                    _CHAT_ID,
                    instruction="Be helpful and concise.",
                    rule_id="bench-rule",
                )

            @benchmark
            def _():
                loop.run_until_complete(_run())
        finally:
            loop.close()
