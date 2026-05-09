"""
src/utils/dag.py — Generic topological sort for dependency-ordered execution.

Provides a single ``topological_sort()`` function used by both
``BuilderOrchestrator`` and ``StartupOrchestrator`` to resolve step
execution order from dependency declarations.
"""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def topological_sort(
    specs: Sequence[T],
    *,
    key: Callable[[T], str],
    depends_on: Callable[[T], Sequence[str]],
    context_label: str = "dependency",
) -> list[T]:
    """Return *specs* in dependency-resolved (topological) order.

    Uses recursive DFS with cycle detection via a *visiting* set.
    When no ``depends_on`` is declared for a spec the original list
    order is preserved among peers.

    Args:
        specs: Items to sort.
        key: Extracts the unique name used as the identity of each item.
        depends_on: Extracts the names of items that must precede this one.
        context_label: Short label included in ``ValueError`` messages
            (e.g. ``"builder"``, ``"startup"``) to aid debugging.

    Returns:
        A new list with items ordered so every item appears *after*
        all items it depends on.

    Raises:
        ValueError: On circular or missing dependencies.
    """
    by_name: dict[str, T] = {key(s): s for s in specs}
    resolved: list[T] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def _visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Circular {context_label}: {name}")
        visiting.add(name)
        spec = by_name.get(name)
        if spec is None:
            raise ValueError(f"Unknown {context_label}: {name}")
        for dep in depends_on(spec):
            _visit(dep)
        resolved.append(spec)
        visiting.discard(name)
        visited.add(name)

    for s in specs:
        _visit(key(s))
    return resolved
