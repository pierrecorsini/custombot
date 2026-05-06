#!/usr/bin/env python3
"""Verify that config.example.json is in sync with CONFIG_SCHEMA.

Two-directional check:
  1. Every field in config.example.json must exist in CONFIG_SCHEMA (catches stale fields).
  2. Every property in CONFIG_SCHEMA must appear in config.example.json (catches undocumented fields).

Keys prefixed with ``_`` in the example file are treated as human-readable
comments and silently ignored.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.config_schema_defs import CONFIG_SCHEMA  # noqa: E402


# ---------------------------------------------------------------------------
# Path extraction helpers
# ---------------------------------------------------------------------------

def _extract_example_paths(obj: dict, prefix: str = "") -> set[str]:
    """Recursively collect leaf key paths from a JSON object."""
    paths: set[str] = set()
    for key, value in obj.items():
        if key.startswith("_"):
            continue  # comment keys
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths.update(_extract_example_paths(value, path))
        else:
            paths.add(path)
    return paths


def _extract_schema_paths(schema: dict, prefix: str = "") -> set[str]:
    """Recursively collect leaf property paths from a JSON Schema definition."""
    paths: set[str] = set()
    for key, prop in schema.get("properties", {}).items():
        if key.startswith("$"):
            continue  # schema metadata
        path = f"{prefix}.{key}" if prefix else key
        if prop.get("type") == "object" and "properties" in prop:
            paths.update(_extract_schema_paths(prop, path))
        else:
            paths.add(path)
    return paths


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    example_path = PROJECT_ROOT / "config.example.json"
    if not example_path.exists():
        print("ERROR: config.example.json not found at project root")
        return 1

    with open(example_path, encoding="utf-8") as fh:
        example = json.load(fh)

    example_paths = _extract_example_paths(example)
    schema_paths = _extract_schema_paths(CONFIG_SCHEMA)

    has_errors = False

    # Direction 1: stale / invalid fields in the example
    stale = sorted(example_paths - schema_paths)
    if stale:
        print("ERROR: config.example.json contains fields not in CONFIG_SCHEMA:")
        for p in stale:
            print(f"  - {p}")
        has_errors = True

    # Direction 2: undocumented schema fields
    undocumented = sorted(schema_paths - example_paths)
    if undocumented:
        print("ERROR: CONFIG_SCHEMA fields missing from config.example.json:")
        for p in undocumented:
            print(f"  - {p}")
        has_errors = True

    if has_errors:
        print(
            "\nconfig.example.json is out of sync with CONFIG_SCHEMA.\n"
            "Update config.example.json to match src/config/config_schema_defs.py"
        )
        return 1

    print(f"OK: config.example.json is in sync with CONFIG_SCHEMA")
    print(f"  Schema fields : {len(schema_paths)}")
    print(f"  Example fields: {len(example_paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
