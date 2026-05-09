"""
storage_protocol.py — Protocol-based storage abstraction.

Defines StorageProvider as a typing.Protocol for structural subtyping,
allowing future backends (PostgreSQL, Redis, etc.) to be swapped in
without modifying consumers.  The existing Database class already
satisfies this protocol via duck typing — no inheritance needed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class StorageProvider(Protocol):
    """Protocol for message and chat persistence operations.

    Consumers should depend on this protocol (or the Storage type alias)
    rather than the concrete Database class so that alternative backends
    can be injected without code changes.
    """

    async def connect(self) -> None:
        """Initialize storage and load existing data."""
        ...

    async def close(self) -> None:
        """Flush pending writes and release resources."""
        ...

    async def message_exists(self, message_id: str) -> bool:
        """Check if a message ID exists across all chats."""
        ...

    async def batch_message_exists(self, message_ids: list[str]) -> dict[str, bool]:
        """Batch-check which message IDs exist."""
        ...

    async def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """Append a message to chat history. Returns the message ID."""
        ...

    async def save_messages_batch(
        self,
        chat_id: str,
        messages: list[dict],
    ) -> list[str]:
        """Persist multiple messages in a single lock acquisition."""
        ...

    async def get_recent_messages(self, chat_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieve recent messages for a chat, oldest first."""
        ...

    async def upsert_chat(self, chat_id: str, name: Optional[str] = None) -> None:
        """Create or update chat metadata."""
        ...

    async def upsert_chat_and_save_message(self, params: Any) -> str:
        """Upsert chat metadata and persist a message in one operation."""
        ...

    async def list_chats(self) -> List[Dict[str, Any]]:
        """List all chats sorted by last activity (most recent first)."""
        ...

    def get_generation(self, chat_id: str) -> int:
        """Return the current generation counter for a chat."""
        ...

    def check_generation(self, chat_id: str, expected: int) -> bool:
        """Return True if the chat's generation still matches expected."""
        ...


# Type alias for imports — depend on the protocol, not the concrete class.
Storage = StorageProvider

__all__ = ["StorageProvider", "Storage"]
