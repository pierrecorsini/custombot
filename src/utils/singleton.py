"""
singleton.py — Thread-safe singleton pattern utilities.

Provides standardized singleton implementations for consistent
singleton management across the codebase.

Usage:
    # Decorator pattern for classes
    from src.utils.singleton import singleton

    @singleton
    class MyService:
        pass

    # Module-level singleton with get_instance()
    from src.utils.singleton import SingletonMeta, get_or_create_singleton

    class MyManager(metaclass=SingletonMeta):
        pass
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional, TypeVar, cast

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
        return cast(T, _singleton_registry[cls])


def reset_singleton(cls: type) -> None:
    """
    Reset a singleton instance (useful for testing).

    Args:
        cls: The class whose singleton instance should be removed
    """
    with _registry_lock:
        _singleton_registry.pop(cls, None)


def singleton(cls: type[T]) -> type[T]:
    """
    Thread-safe singleton decorator for classes.

    Wraps a class so that only one instance is ever created,
    regardless of how many times the class is instantiated.

    Thread safety is ensured via ThreadLock (see src.utils.locking policy).

    Args:
        cls: The class to make a singleton

    Returns:
        The wrapped class that returns the singleton instance

    Example:
        @singleton
        class Database:
            def __init__(self, url: str):
                self.url = url

        # Both calls return the same instance
        db1 = Database("postgres://localhost")
        db2 = Database("postgres://other")  # url ignored, same instance
        assert db1 is db2
    """
    _instance: Optional[T] = None
    _lock = ThreadLock()

    @functools.wraps(cls)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        nonlocal _instance
        if _instance is None:
            with _lock:
                # Double-check after acquiring lock
                if _instance is None:
                    _instance = cls(*args, **kwargs)
        return _instance

    return cast(type[T], wrapper)


class SingletonMeta(type):
    """
    Metaclass for thread-safe singleton classes.

    Classes using this metaclass will only ever have one instance.
    Thread safety is ensured via ThreadLock (see src.utils.locking policy).

    Example:
        class MyService(metaclass=SingletonMeta):
            def __init__(self, config: dict):
                self.config = config

        # Both return the same instance
        s1 = MyService({"key": "value"})
        s2 = MyService({"other": "config"})  # config ignored
        assert s1 is s2
    """

    _instances: dict[type, Any] = {}
    _lock = ThreadLock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls not in cls._instances:
            with cls._lock:
                # Double-check after acquiring lock
                if cls not in cls._instances:
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience function for module-level singletons
# ─────────────────────────────────────────────────────────────────────────────


def create_singleton_getter(
    cls: type[T],
) -> Callable[..., T]:
    """
    Create a thread-safe get_instance() function for a class.

    Returns a function that lazily creates and returns the singleton
    instance of the given class.

    Args:
        cls: The class to create a singleton getter for

    Returns:
        A function that returns the singleton instance

    Example:
        class RateLimiter:
            pass

        get_rate_limiter = create_singleton_getter(RateLimiter)
        limiter = get_rate_limiter()
    """
    _instance: Optional[T] = None
    _lock = ThreadLock()

    def get_instance(*args: Any, **kwargs: Any) -> T:
        nonlocal _instance
        if _instance is None:
            with _lock:
                # Double-check after acquiring lock
                if _instance is None:
                    _instance = cls(*args, **kwargs)
        return _instance

    return get_instance


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "singleton",
    "SingletonMeta",
    "get_or_create_singleton",
    "reset_singleton",
    "create_singleton_getter",
]
