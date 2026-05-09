"""
src/utils/protocols.py — Protocol definitions for structural subtyping.

Defines interfaces using typing.Protocol to enable duck-typing semantics
and better type checking without requiring explicit inheritance.

Protocols:
    - Channel: Interface for messaging channel implementations
    - MemoryProtocol: Interface for per-chat memory operations
    - Skill: Interface for executable tool-skills
    - Storage: Interface for database operations

Usage:
    from src.utils.protocols import Channel, MemoryProtocol, Skill, Storage

    def process_messages(channel: Channel, memory: MemoryProtocol) -> None:
        # Accepts any object with the respective protocol methods
        ...

    # Runtime check (limited - only checks method existence)
    if isinstance(my_obj, Channel):
        await my_obj.start(handler)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
    TYPE_CHECKING,
)

if TYPE_CHECKING:
    from pathlib import Path
    import asyncio

# Type alias for message handlers (re-exported for convenience)
MessageHandler = Callable[..., Awaitable[None]]


@runtime_checkable
class Channel(Protocol):
    """
    Protocol for messaging channel implementations.

    Any class that implements these methods (start, send_message, send_typing)
    satisfies this protocol, enabling structural subtyping without inheritance.

    This allows different channel implementations (WhatsApp, Telegram, etc.)
    to be used interchangeably with the bot core.

    Methods:
        start: Initialize and run the channel (polling or webhook)
        send_message: Send a text message to a chat
        send_typing: Send typing indicator (optional, may be no-op)
        get_channel_prompt: Return channel-specific prompt instructions (optional)

    Example:
        class WhatsAppChannel:
            async def start(self, handler: MessageHandler) -> None:
                # Poll for messages and call handler
                ...

            async def send_message(self, chat_id: str, text: str) -> None:
                # Send via WhatsApp API
                ...

        # WhatsAppChannel satisfies Channel protocol without inheriting
        channel: Channel = WhatsAppChannel()
    """

    async def start(self, handler: Callable[..., Awaitable[None]]) -> None:
        """
        Start the channel and process incoming messages.

        For polling channels, this enters an infinite loop.
        For webhook channels, this starts the HTTP server.

        Args:
            handler: Async callback invoked for each incoming message.
                     Receives the normalized message object.
        """
        ...

    async def send_message(self, chat_id: str, text: str) -> None:
        """
        Send a text message to the specified chat.

        Args:
            chat_id: Target chat/conversation identifier.
            text: Message content to send.
        """
        ...

    async def send_typing(self, chat_id: str) -> None:
        """
        Send a typing indicator to the specified chat.

        Implementations may be a no-op if the channel doesn't support
        typing indicators.

        Args:
            chat_id: Target chat/conversation identifier.
        """
        ...

    def get_channel_prompt(self) -> Optional[str]:
        """
        Return channel-specific prompt instructions to inject before other prompts.

        Override this method to provide formatting or behavioral instructions
        specific to this channel (e.g., WhatsApp formatting, Telegram markdown).

        Returns:
            Channel-specific prompt content, or None if no prompt needed.
        """
        ...


@runtime_checkable
class Skill(Protocol):
    """
    Protocol for executable tool-skills.

    Skills are tools that can be invoked by the LLM. Any class with
    the required attributes and methods satisfies this protocol.

    Attributes:
        name: Tool name exposed to the LLM (must be valid Python identifier)
        description: Human-readable description for LLM tool selection
        parameters: JSON Schema object describing function parameters

    Methods:
        execute: Run the skill and return a string result
        to_tool_definition: Return OpenAI tools-array entry

    Example:
        class WeatherSkill:
            name = "get_weather"
            description = "Get current weather for a location"
            parameters = {
                "type": "object",
                "properties": {"location": {"type": "string"}},
                "required": ["location"]
            }

            async def execute(self, workspace_dir: Path, **kwargs) -> str:
                location = kwargs["location"]
                return f"Weather for {location}: Sunny"

        # WeatherSkill satisfies Skill protocol
        skill: Skill = WeatherSkill()
    """

    name: str
    description: str
    parameters: Dict[str, Any]
    expensive: bool

    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        """
        Execute the skill with the provided arguments.

        All file I/O and subprocess execution MUST use workspace_dir
        as the working directory for per-chat isolation.

        Args:
            workspace_dir: Working directory for file operations.
            **kwargs: Skill-specific parameters from LLM tool call.

        Returns:
            String result fed back to the LLM as a tool response.
        """
        ...

    def to_tool_definition(self) -> Dict[str, Any]:
        """
        Return the OpenAI tools-array entry for this skill.

        Returns:
            Dict with 'type' and 'function' keys containing name,
            description, and parameters schema.
        """
        ...


@runtime_checkable
class Storage(Protocol):
    """
    Protocol for database/persistence operations.

    Defines the core storage interface for messages, chats, and routing rules.
    Any implementation providing these methods can be used with the bot.

    Methods:
        connect: Initialize storage and load existing data
        close: Flush pending writes and close connections
        message_exists: Check if a message ID exists (for dedup)
        save_message: Append a message to chat history
        get_recent_messages: Retrieve recent messages for context
        upsert_chat: Create or update chat metadata
        list_chats: List all chats sorted by activity

    Example:
        class PostgresStorage:
            async def connect(self) -> None:
                self._pool = await asyncpg.create_pool(DATABASE_URL)

            async def save_message(self, chat_id: str, role: str,
                                   content: str, name: Optional[str] = None,
                                   message_id: Optional[str] = None) -> str:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO messages (...) VALUES (...)",
                        ...
                    )
                return message_id

        # PostgresStorage satisfies Storage protocol
        storage: Storage = PostgresStorage()
    """

    async def connect(self) -> None:
        """
        Initialize storage and load existing data.

        Must be called before any other storage operations.
        Creates necessary directories/tables if they don't exist.
        """
        ...

    async def close(self) -> None:
        """
        Flush pending writes and close connections.

        After calling this, connect() must be called again before
        any storage operations.
        """
        ...

    async def message_exists(self, message_id: str) -> bool:
        """
        Check if a message ID exists across all chats.

        Used for duplicate message detection.

        Args:
            message_id: Unique message identifier to check.

        Returns:
            True if message ID exists, False otherwise.
        """
        ...

    async def batch_message_exists(self, message_ids: list[str]) -> dict[str, bool]:
        """
        Batch-check which message IDs exist across all chats.

        More efficient than calling ``message_exists`` N times when
        checking many IDs at once (e.g. crash-recovery backlog).

        Args:
            message_ids: List of unique message identifiers to check.

        Returns:
            Dict mapping each requested ID to True (exists) or False.
        """
        ...

    async def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """
        Save a message to chat history.

        Args:
            chat_id: Chat/conversation identifier.
            role: Message role ('user', 'assistant', 'tool').
            content: Message content.
            name: Optional sender name or tool name.
            message_id: Optional message ID (generated if not provided).

        Returns:
            The message ID.
        """
        ...

    async def get_recent_messages(self, chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieve recent messages for a chat.

        Args:
            chat_id: Chat/conversation identifier.
            limit: Maximum number of messages to return.

        Returns:
            List of message dicts with 'role', 'content', 'name' keys,
            ordered oldest first.
        """
        ...

    async def upsert_chat(self, chat_id: str, name: Optional[str] = None) -> None:
        """
        Create or update chat metadata.

        Args:
            chat_id: Unique chat identifier.
            name: Optional display name for the chat.
        """
        ...

    async def list_chats(self) -> List[Dict[str, Any]]:
        """
        List all chats sorted by last activity (most recent first).

        Returns:
            List of chat dicts with 'chat_id', 'name', 'created_at',
            'last_active' keys.
        """
        ...


# Protocol version info for documentation purposes
__all__ = [
    "BackgroundService",
    "Channel",
    "Closeable",
    "LockProvider",
    "MemoryMonitor",
    "MemoryProtocol",
    "MessageHandler",
    "Skill",
    "Stoppable",
    "Storage",
    "ProjectStore",
    "ProjectContextLoader",
]


# Storage is now defined in src.db.storage_protocol.StorageProvider.
# Re-export from here for backward compatibility.
# (The @runtime_checkable class below remains for existing code that
# imports from this module directly.)


@runtime_checkable
class ProjectStore(Protocol):
    """
    Protocol for project/knowledge persistence.

    Provides CRUD operations for projects and knowledge entries.
    """

    def close(self) -> None:
        """Flush and close the store."""
        ...


@runtime_checkable
class ProjectContextLoader(Protocol):
    """
    Protocol for loading project context for LLM injection.
    """

    async def get(self, chat_id: str) -> Optional[str]:
        """Load project context for a chat. Returns None if no context."""
        ...


@runtime_checkable
class MemoryProtocol(Protocol):
    """
    Protocol for per-chat memory operations.

    Decouples Bot from the concrete Memory class so alternative
    implementations (e.g., Redis-backed, database-backed) can be
    swapped in without touching Bot.

    Methods:
        ensure_workspace: Create per-chat workspace directory and seed files
        read_memory: Read the MEMORY.md file for a chat
        read_agents_md: Read the AGENTS.md file for a chat

    Example:
        class RedisMemory:
            def __init__(self, redis_url: str) -> None:
                self._redis = redis.asyncio.from_url(redis_url)

            def ensure_workspace(self, chat_id: str) -> Path:
                # Create workspace dir and return Path
                ...

            async def read_memory(self, chat_id: str) -> Optional[str]:
                content = await self._redis.get(f"memory:{chat_id}")
                return content

            async def read_agents_md(self, chat_id: str) -> str:
                content = await self._redis.get(f"agents:{chat_id}")
                return content or "Default instructions"

        # RedisMemory satisfies MemoryProtocol
        memory: MemoryProtocol = RedisMemory("redis://localhost")
    """

    def ensure_workspace(self, chat_id: str) -> Path:
        """
        Create the per-chat workspace directory and seed initial files.

        Args:
            chat_id: Chat/conversation identifier.

        Returns:
            Path to the chat's workspace directory.
        """
        ...

    async def read_memory(self, chat_id: str) -> Optional[str]:
        """
        Read the MEMORY.md content for a chat.

        Args:
            chat_id: Chat/conversation identifier.

        Returns:
            Memory content string, or None if no memory exists.
        """
        ...

    async def read_agents_md(self, chat_id: str) -> str:
        """
        Read the AGENTS.md content for a chat.

        Args:
            chat_id: Chat/conversation identifier.

        Returns:
            Agent instructions content string.

        Raises:
            FileNotFoundError: If AGENTS.md has not been seeded yet.
        """
        ...


@runtime_checkable
class LockProvider(Protocol):
    """
    Protocol for per-chat lock management.

    Decouples Bot from the concrete LRULockCache so alternative
    implementations (e.g., distributed lock backends, shared lock
    state for multi-process deployments) can be swapped in without
    touching Bot.

    Methods:
        get_or_create: Get an existing lock or create a new one for the key
        acquire: Async context manager that ref-tracks, acquires, and releases
        release: Decrement reference count for a previously obtained lock
        __len__: Return the current number of cached locks
        active_count: Return the number of currently held (ref_count > 0) locks

    Example:
        class RedisLockProvider:
            def __init__(self, redis_url: str) -> None:
                self._redis = redis.asyncio.from_url(redis_url)

            async def get_or_create(self, key: str) -> asyncio.Lock:
                return asyncio.Lock()

            def __len__(self) -> int:
                return 0
    """

    async def get_or_create(self, key: str) -> "asyncio.Lock":
        """
        Get an existing lock for the key or create a new one.

        Args:
            key: Unique identifier for the lock (e.g., chat_id).

        Returns:
            An asyncio.Lock for the given key.
        """
        ...

    @asynccontextmanager
    async def acquire(self, key: str) -> AsyncIterator[None]:
        """
        Context manager: get, ref-track, acquire, and release a lock.

        Combines get_or_create(), lock acquisition, and release() into
        a single async context manager so callers never miss the release step.
        """
        ...
        yield  # pragma: no cover

    def release(self, key: str) -> None:
        """Decrement the reference count for a previously obtained lock."""
        ...

    def __len__(self) -> int:
        """Return the current number of managed locks."""
        ...

    @property
    def active_count(self) -> int:
        """Return the number of locks currently held (ref_count > 0)."""
        ...


@runtime_checkable
class MemoryMonitor(Protocol):
    """
    Protocol for system memory monitoring.
    """

    def register_cache(self, name: str, size_fn: Callable[[], int]) -> None:
        """Register a cache for size tracking."""
        ...

    def start_periodic_check(self, interval_seconds: float) -> None:
        """Start periodic memory checks."""
        ...

    async def stop(self) -> None:
        """Stop monitoring."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle Protocols
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class Stoppable(Protocol):
    """Protocol for background services with long-running loops.

    Components that manage asyncio tasks or background loops should
    implement ``stop()`` to cancel tasks and clean up resources.

    Examples: TaskScheduler, WorkspaceMonitor, PerformanceMetrics,
    MemoryMonitor, HealthServer, ConfigWatcher.
    """

    async def stop(self) -> None:
        """Cancel background tasks and release resources."""
        ...


@runtime_checkable
class Closeable(Protocol):
    """Protocol for resources that hold connections or handles.

    Components that manage open connections, file handles, or database
    references should implement ``close()`` to release them.

    Examples: Database, BaseChannel, LLMClient, MessageQueue, EventBus.
    """

    async def close(self) -> None:
        """Release connections, handles, and other resources."""
        ...


@runtime_checkable
class BackgroundService(Protocol):
    """Protocol for long-running background services with managed tasks.

    Standardizes the lifecycle of components that spawn ``asyncio.create_task``
    loops.  All such services should follow: start → loop → stop.

    Examples: TaskScheduler, WorkspaceMonitor, PerformanceMetrics,
    MemoryMonitor, HealthServer, ConfigWatcher, MessageQueue (flush loop),
    LLMClient (health probe), Channel (incoming message pump).
    """

    async def stop(self) -> None:
        """Cancel background tasks and release resources."""
        ...
