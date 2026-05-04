"""
src/utils/registry.py — Lightweight DI registry for construction-phase contexts.

Replaces ``field: X | None = None`` mutable bag patterns with a dict-backed
store that surfaces missing required dependencies at *access* time rather than
at a deferred validation step.

Usage::

    registry = ComponentRegistry()
    registry.register("db", db_instance)
    db = registry.require("db")       # returns db_instance
    vm = registry.get("vector_memory") # returns None if not registered
"""

from __future__ import annotations

from typing import Any


class ComponentRegistry:
    """Typed component registry for mutable construction-phase contexts.

    Stores components by string key and provides two access patterns:

    - ``require(key)`` — raises immediately if the component has not been
      registered, surfacing missing dependencies at the point of use.
    - ``get(key)`` — returns the component or ``None`` for optional deps.

    Typically used via ``__getattr__`` / ``__setattr__`` on context classes
    so that step functions can use natural ``ctx.db`` / ``ctx.db = db`` syntax.
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def register(self, key: str, component: Any) -> None:
        """Register a component."""
        self._store[key] = component

    def require(self, key: str) -> Any:
        """Get a required component. Raises if not yet registered."""
        if key not in self._store:
            raise RuntimeError(f"Required component '{key}' is not available")
        return self._store[key]

    def get(self, key: str) -> Any | None:
        """Get an optional component. Returns None if not registered."""
        return self._store.get(key)

    def has(self, key: str) -> bool:
        """Check if a component has been registered."""
        return key in self._store

    def validate_required(self, keys: tuple[str, ...]) -> None:
        """Validate that all required keys are present."""
        missing = [k for k in keys if k not in self._store]
        if missing:
            raise RuntimeError(
                f"Context incomplete — missing components: {', '.join(missing)}"
            )

    def snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of all registered components."""
        return dict(self._store)
