"""
di.py — Lightweight dependency injection container.

Supports singleton and transient scoped lifetimes with async factory
functions.  Extends the existing ComponentRegistry pattern with
automatic lifecycle management.

Usage::

    container = DIContainer()
    container.register(Database, create_database, scope="singleton")
    container.register(RateLimiter, create_rate_limiter, scope="transient")

    db = await container.resolve(Database)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, TypeVar

from src.utils.registry import ComponentRegistry

log = logging.getLogger(__name__)

__all__ = ["DIContainer", "Scope", "Registration"]

T = TypeVar("T")

# Factory type: async callable that returns the constructed instance.
Factory = Callable[[], Awaitable[Any]]


class Scope(StrEnum):
    """Supported dependency lifetimes."""

    SINGLETON = "singleton"
    TRANSIENT = "transient"


@dataclass(slots=True, frozen=True)
class Registration:
    """Immutable registration entry binding a type to a factory."""

    interface: type
    factory: Factory
    scope: Scope


class DIContainer:
    """Lightweight DI container with scoped lifetimes.

    Integrates with the existing ComponentRegistry for backward
    compatibility — resolved singletons are also stored in a shared
    registry so code using ``registry.require()`` continues to work.
    """

    __slots__ = ("_registrations", "_singletons", "_registry")

    def __init__(self, registry: ComponentRegistry | None = None) -> None:
        self._registrations: dict[type, Registration] = {}
        self._singletons: dict[type, Any] = {}
        self._registry = registry or ComponentRegistry()

    def register(
        self,
        interface: type,
        factory: Factory,
        scope: Scope | str = Scope.SINGLETON,
        name: str | None = None,
    ) -> None:
        """Register a factory for an interface type.

        Args:
            interface: The type/key to resolve against.
            factory: Async callable returning the instance.
            scope: 'singleton' (cached) or 'transient' (new each time).
            name: Optional string key for registry storage.
        """
        scope_enum = Scope(scope)
        self._registrations[interface] = Registration(
            interface=interface,
            factory=factory,
            scope=scope_enum,
        )

    async def resolve(self, interface: type) -> Any:
        """Resolve an instance for the given interface type.

        Singleton scope: create once, cache, and reuse.
        Transient scope: create a new instance each call.
        """
        reg = self._registrations.get(interface)
        if reg is None:
            raise KeyError(
                f"No registration found for {interface.__name__}. "
                f"Registered: {[r.interface.__name__ for r in self._registrations.values()]}"
            )

        if reg.scope == Scope.SINGLETON:
            cached = self._singletons.get(interface)
            if cached is not None:
                return cached

            instance = await reg.factory()
            self._singletons[interface] = instance
            # Also store in the shared registry for legacy access.
            name = interface.__name__
            self._registry.register(name, instance)
            return instance

        # Transient — always new instance
        return await reg.factory()

    def has(self, interface: type) -> bool:
        """Check if a registration exists for the interface."""
        return interface in self._registrations

    @property
    def registry(self) -> ComponentRegistry:
        """Access the underlying ComponentRegistry."""
        return self._registry
