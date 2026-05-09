"""
singleton.py — Thread-safe singleton pattern utilities.

Provides registry-based singleton management for consistent
singleton creation and reset across the codebase.

Usage:
    from src.utils.singleton import get_or_create_singleton, reset_singleton

    instance = get_or_create_singleton(MyService, arg1, arg2)
    reset_singleton(MyService)  # useful in tests
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from src.utils.locking import ThreadLock

T = TypeVar("T")

# ─────────────────────────────────────────────────────────────────────────────
# Module-level registry for singleton instances
# ─────────────────────────────────────────────────────────────────────────────

_singleton_registry: dict[type, Any] = {}
_registry_lock = ThreadLock()


def get_or_create_singleton(
    cls: type[T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """
    Get or create a singleton instance of a class.

    Thread-safe singleton retrieval/creation using module-level lock.
    If the instance exists, returns it (ignoring args/kwargs).
    If not, creates it with the provided arguments.

    Args:
        cls: The class to instantiate as singleton
        *args: Constructor arguments (used only on first creation)
        **kwargs: Constructor keyword arguments (used only on first creation)

    Returns:
        The singleton instance of the class
    """
    with _registry_lock:
        if cls not in _singleton_registry:
            _singleton_registry[cls] = cls(*args, **kwargs)
        return cast("T", _singleton_registry[cls])


def reset_singleton(cls: type) -> None:
    """
    Reset a singleton instance (useful for testing).

    Args:
        cls: The class whose singleton instance should be removed
    """
    with _registry_lock:
        _singleton_registry.pop(cls, None)


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "get_or_create_singleton",
    "reset_singleton",
]
