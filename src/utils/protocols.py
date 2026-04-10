"""
src/utils/protocols.py — Protocol definitions for structural subtyping.

Defines interfaces using typing.Protocol to enable duck-typing semantics
and better type checking without requiring explicit inheritance.

Protocols:
    - Channel: Interface for messaging channel implementations
    - Skill: Interface for executable tool-skills
    - Storage: Interface for database operations

Usage:
    from src.utils.protocols import Channel, Skill, Storage

    def process_messages(channel: Channel) -> None:
        # Accepts any object with Channel protocol methods
        ...

    # Runtime check (limited - only checks method existence)
    if isinstance(my_obj, Channel):
        await my_obj.start(handler)
"""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)


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

    async def get_recent_messages(
        self, chat_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
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
    "MessageHandler",
    "Channel",
    "Skill",
    "Storage",
]
