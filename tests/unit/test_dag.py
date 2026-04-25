"""
Tests for src/utils/dag.py — generic topological sort utility.

Covers:
  (a) valid DAG with dependencies resolved in correct order
  (b) circular dependency detection with informative error
  (c) missing dependency detection
  (d) empty input
  (e) single node with no deps
  (f) diamond dependency (A→B, A→C, B→D, C→D)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.utils.dag import topological_sort


@dataclass
class Node:
    """Minimal spec for topological_sort tests."""

    name: str
    deps: list[str]


def _key(n: Node) -> str:
    return n.name


def _deps(n: Node) -> list[str]:
    return n.deps


# ─────────────────────────────────────────────────────────────────────────────
# (d) Empty input
# ─────────────────────────────────────────────────────────────────────────────


class TestEmptyInput:
    def test_empty_list_returns_empty(self):
        assert topological_sort([], key=_key, depends_on=_deps) == []


# ─────────────────────────────────────────────────────────────────────────────
# (e) Single node with no deps
# ─────────────────────────────────────────────────────────────────────────────


class TestSingleNode:
    def test_single_node_no_deps(self):
        node = Node("a", [])
        result = topological_sort([node], key=_key, depends_on=_deps)
        assert result == [node]


# ─────────────────────────────────────────────────────────────────────────────
# (a) Valid DAG — correct order
# ─────────────────────────────────────────────────────────────────────────────


class TestValidDAG:
    def test_linear_chain(self):
        """A depends on B, B depends on C → result is [C, B, A]."""
        c = Node("c", [])
        b = Node("b", ["c"])
        a = Node("a", ["b"])
        result = topological_sort([a, b, c], key=_key, depends_on=_deps)
        assert result.index(c) < result.index(b) < result.index(a)

    def test_independent_nodes_preserve_order(self):
        """No deps → original list order preserved among peers."""
        x = Node("x", [])
        y = Node("y", [])
        z = Node("z", [])
        result = topological_sort([x, y, z], key=_key, depends_on=_deps)
        assert result == [x, y, z]

    def test_partial_dependencies(self):
        """A depends on B; C is independent. B and C are peers in original order."""
        b = Node("b", [])
        c = Node("c", [])
        a = Node("a", ["b"])
        result = topological_sort([b, c, a], key=_key, depends_on=_deps)
        assert result.index(b) < result.index(a)
        assert c in result


# ─────────────────────────────────────────────────────────────────────────────
# (b) Circular dependency detection
# ─────────────────────────────────────────────────────────────────────────────


class TestCircularDependency:
    def test_direct_cycle_raises(self):
        """A depends on B, B depends on A → ValueError."""
        a = Node("a", ["b"])
        b = Node("b", ["a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            topological_sort([a, b], key=_key, depends_on=_deps)

    def test_indirect_cycle_raises(self):
        """A→B→C→A cycle → ValueError."""
        a = Node("a", ["b"])
        b = Node("b", ["c"])
        c = Node("c", ["a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            topological_sort([a, b, c], key=_key, depends_on=_deps)

    def test_error_includes_context_label(self):
        """The context_label appears in the error message."""
        a = Node("a", ["b"])
        b = Node("b", ["a"])
        with pytest.raises(ValueError, match="builder"):
            topological_sort([a, b], key=_key, depends_on=_deps, context_label="builder")

    def test_self_dependency_raises(self):
        """A depends on itself → ValueError."""
        a = Node("a", ["a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            topological_sort([a], key=_key, depends_on=_deps)


# ─────────────────────────────────────────────────────────────────────────────
# (c) Missing dependency detection
# ─────────────────────────────────────────────────────────────────────────────


class TestMissingDependency:
    def test_missing_dep_raises(self):
        """A depends on 'z' which is not in the spec list → ValueError."""
        a = Node("a", ["z"])
        with pytest.raises(ValueError, match="[Uu]nknown"):
            topological_sort([a], key=_key, depends_on=_deps)

    def test_missing_dep_error_includes_context_label(self):
        """context_label appears in the missing-dep error."""
        a = Node("a", ["missing"])
        with pytest.raises(ValueError, match="startup"):
            topological_sort([a], key=_key, depends_on=_deps, context_label="startup")

    def test_missing_dep_in_chain(self):
        """B depends on C which is not present → ValueError."""
        a = Node("a", ["b"])
        b = Node("b", ["c"])
        with pytest.raises(ValueError, match="[Uu]nknown"):
            topological_sort([a, b], key=_key, depends_on=_deps)


# ─────────────────────────────────────────────────────────────────────────────
# (f) Diamond dependency
# ─────────────────────────────────────────────────────────────────────────────


class TestDiamondDependency:
    """Diamond: A→B, A→C, B→D, C→D.

    Valid orderings: D must come before B and C; B and C must come before A.
    """

    def test_diamond_valid_order(self):
        d = Node("d", [])
        c = Node("c", ["d"])
        b = Node("b", ["d"])
        a = Node("a", ["b", "c"])
        result = topological_sort([a, b, c, d], key=_key, depends_on=_deps)

        # D before B and C; B and C before A
        assert result.index(d) < result.index(b)
        assert result.index(d) < result.index(c)
        assert result.index(b) < result.index(a)
        assert result.index(c) < result.index(a)
        assert len(result) == 4

    def test_diamond_all_deps_satisfied(self):
        """Every node appears after all its dependencies."""
        d = Node("d", [])
        c = Node("c", ["d"])
        b = Node("b", ["d"])
        a = Node("a", ["b", "c"])
        result = topological_sort([a, b, c, d], key=_key, depends_on=_deps)
        positions = {n.name: i for i, n in enumerate(result)}

        for node in [d, c, b, a]:
            for dep in node.deps:
                assert positions[dep] < positions[node.name]
