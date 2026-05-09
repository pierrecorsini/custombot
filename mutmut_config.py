"""Mutation testing configuration for mutmut.

Usage::

    # Run against all configured paths
    mutmut run

    # Run against a specific module
    mutmut run --paths-to-mutate src/core/dedup.py

    # View results
    mutmut results
    mutmut show <id>

Running locally::

    make mutation-test

    # Or directly:
    mutmut run --paths-to-mutate src/core/dedup.py,src/routing.py \\
        --test-command "python -m pytest tests/unit/test_dedup.py tests/unit/test_routing.py -x -q --tb=no"

Configuration reference: https://mutmut.readthedocs.io/

Expected mutant counts (update as coverage improves):
    - dedup.py:        ~40-60 mutants, target >85% killed
    - event_bus.py:    ~30-50 mutants, target >80% killed
    - routing.py:      ~50-70 mutants, target >80% killed
    - react_loop.py:   ~80-120 mutants, target >75% killed
"""

from __future__ import annotations


def pre_mutation(context: dict) -> dict:
    """Pre-mutation hook: skip files that shouldn't be mutated."""
    filename = context.get("filename", "")

    # Skip test files
    if "test_" in filename or "_test.py" in filename:
        context["skip"] = True
        return context

    # Skip __init__.py files (usually just imports)
    if filename.endswith("__init__.py"):
        context["skip"] = True
        return context

    # Skip constant-only files
    if "constants" in filename:
        context["skip"] = True
        return context

    return context


def post_mutation(context: dict) -> dict:
    """Post-mutation hook: called after each mutation."""
    return context


# Source modules to mutate — core business logic with strong test coverage.
paths_to_mutate = [
    "src/core/dedup.py",
    "src/core/event_bus.py",
    "src/routing.py",
    "src/bot/react_loop.py",
    "src/rate_limiter.py",
]

# Test command — runs unit tests only for speed.
test_command = "python -m pytest tests/unit/ -x -q --tb=no"
