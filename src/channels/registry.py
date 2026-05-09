"""
registry.py — Channel registry for dynamic channel registration and creation.

Provides a centralized registry where channel implementations register
themselves by name, and the application creates channels from config
without importing concrete classes.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.channels.base import BaseChannel

log = logging.getLogger(__name__)

__all__ = ["ChannelRegistry", "ChannelState"]


class ChannelState(StrEnum):
    """Lifecycle states for a channel."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class ChannelRegistry:
    """Dynamic channel registry with lifecycle validation.

    Channels register themselves by name. The registry creates instances
    from config and validates that all registered channels implement the
    required lifecycle hooks.
    """

    __slots__ = ("_registry",)

    def __init__(self) -> None:
        self._registry: dict[str, type[BaseChannel]] = {}

    def register(self, name: str, channel_class: type[BaseChannel]) -> None:
        """Register a channel class by name.

        Validates that the class implements the required lifecycle hooks
        before registration.
        """
        _validate_lifecycle_hooks(channel_class)
        self._registry[name] = channel_class
        log.info("Registered channel: %s (%s)", name, channel_class.__name__)

    def create(self, name: str, **config: Any) -> BaseChannel:
        """Create a channel instance by registered name.

        Args:
            name: The registered channel name (e.g. 'whatsapp', 'cli').
            **config: Arguments forwarded to the channel constructor.

        Raises:
            KeyError: If the channel name is not registered.
        """
        channel_class = self._registry.get(name)
        if channel_class is None:
            available = ", ".join(sorted(self._registry))
            raise KeyError(
                f"Unknown channel '{name}'. Registered: {available}"
            )
        return channel_class(**config)

    def has(self, name: str) -> bool:
        """Check if a channel name is registered."""
        return name in self._registry

    def registered_names(self) -> list[str]:
        """Return sorted list of registered channel names."""
        return sorted(self._registry)

    def validate_all(self) -> list[str]:
        """Validate all registered channels implement lifecycle hooks.

        Returns list of error messages (empty if all valid).
        """
        errors: list[str] = []
        for name, cls in self._registry.items():
            try:
                _validate_lifecycle_hooks(cls)
            except TypeError as exc:
                errors.append(f"{name}: {exc}")
        return errors


# Required abstract methods that BaseChannel subclasses must implement.
_REQUIRED_HOOKS = ("start", "close", "_send_message", "send_typing", "request_shutdown")


def _validate_lifecycle_hooks(channel_class: type) -> None:
    """Verify the class implements all required lifecycle hooks."""
    missing = [
        hook for hook in _REQUIRED_HOOKS
        if not _has_method(channel_class, hook)
    ]
    if missing:
        raise TypeError(
            f"{channel_class.__name__} missing lifecycle hooks: {', '.join(missing)}"
        )


def _has_method(cls: type, name: str) -> bool:
    """Check if a class (not its ABC base) provides a concrete method."""
    attr = cls.__dict__.get(name)
    if attr is None:
        return False
    return callable(attr)
