#!/usr/bin/env python
"""
release.py — Semantic versioning and changelog automation.

Commands:
    python scripts/release.py bump major|minor|patch
    python scripts/release.py changelog [--from REF] [--to REF]
    python scripts/release.py release major|minor|patch

Reads the current version from pyproject.toml, bumps it, updates both
pyproject.toml and src/__version__.py, generates a changelog from git
log (grouped by conventional commit prefix), and optionally creates a
git tag and release commit.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_FILE = ROOT / "src" / "__version__.py"

# Conventional-commit prefix → display label
COMMIT_CATEGORIES: list[tuple[str, str]] = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("refactor", "Refactoring"),
    ("perf", "Performance"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Build"),
    ("ci", "CI"),
    ("chore", "Chores"),
]

VERSION_RE = re.compile(r'version\s*=\s*"(\d+\.\d+\.\d+)"')


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, cwd=ROOT, text=True).strip()


def read_version() -> str:
    match = VERSION_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        sys.exit("ERROR: cannot find version in pyproject.toml")
    return match.group(1)


def write_version(new_version: str) -> None:
    # Update pyproject.toml
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    pyproject_text = VERSION_RE.sub(f'version = "{new_version}"', pyproject_text)
    PYPROJECT.write_text(pyproject_text, encoding="utf-8")

    # Update src/__version__.py
    version_content = f'"""\nVersion information for custombot.\n\nSingle source of truth for version number, used by:\n- CLI --version flag\n- Health check endpoint\n- Package metadata\n"""\n\n__version__ = "{new_version}"\n'
    VERSION_FILE.write_text(version_content, encoding="utf-8")

    print(f"Version updated to {new_version} in pyproject.toml and src/__version__.py")


def bump_version(part: str) -> str:
    current = read_version()
    major, minor, patch = (int(x) for x in current.split("."))

    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    else:
        sys.exit(f"ERROR: invalid bump part '{part}'. Use major, minor, or patch.")

    new_version = f"{major}.{minor}.{patch}"
    write_version(new_version)
    return new_version


def generate_changelog(from_ref: str = "HEAD~20", to_ref: str = "HEAD") -> str:
    log = _run(["git", "log", "--oneline", f"{from_ref}..{to_ref}"])
    if not log:
        return "No commits found in range.\n"

    categorized: dict[str, list[str]] = {label: [] for _, label in COMMIT_CATEGORIES}
    categorized["Other"] = []
    uncategorized: list[str] = []

    for line in log.splitlines():
        # Match conventional commit prefix: type(scope)!: message
        m = re.match(r"^([0-9a-f]+)\s+(\w+)(?:\([^)]*\))?!?:\s+(.+)$", line)
        if m:
            commit_hash, prefix, message = m.groups()
            label = _prefix_to_label(prefix)
            categorized[label].append(f"  - {message} ({commit_hash})")
        else:
            commit_hash, _, message = line.partition(" ")
            uncategorized.append(f"  - {message} ({commit_hash})")

    lines: list[str] = []
    for _, label in COMMIT_CATEGORIES:
        entries = categorized[label]
        if entries:
            lines.append(f"### {label}")
            lines.extend(entries)
            lines.append("")

    other = uncategorized or categorized["Other"]
    if other:
        lines.append("### Other")
        lines.extend(other)
        lines.append("")

    return "\n".join(lines)


def _prefix_to_label(prefix: str) -> str:
    for p, label in COMMIT_CATEGORIES:
        if prefix.lower().startswith(p):
            return label
    return "Other"


def create_release(part: str) -> None:
    old_version = read_version()
    new_version = bump_version(part)

    changelog = generate_changelog(from_ref=f"v{old_version}")
    header = f"## {new_version}\n\n{changelog}"

    print(f"\n{'=' * 60}")
    print(header)
    print(f"{'=' * 60}\n")

    _run(["git", "add", str(PYPROJECT), str(VERSION_FILE)])
    _run(["git", "commit", "-m", f"release: v{new_version}"])
    _run(["git", "tag", f"v{new_version}"])

    print(f"Release v{new_version} committed and tagged.")
    print("Push with: git push origin main --tags")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]

    if command == "bump":
        if len(sys.argv) < 3:
            sys.exit("Usage: python scripts/release.py bump major|minor|patch")
        new = bump_version(sys.argv[2])
        print(f"Bumped to {new}")

    elif command == "changelog":
        from_ref = "HEAD~20"
        to_ref = "HEAD"
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--from" and i + 1 < len(sys.argv):
                from_ref = sys.argv[i + 1]
            elif arg == "--to" and i + 1 < len(sys.argv):
                to_ref = sys.argv[i + 1]
        print(generate_changelog(from_ref=from_ref, to_ref=to_ref))

    elif command == "release":
        if len(sys.argv) < 3:
            sys.exit("Usage: python scripts/release.py release major|minor|patch")
        create_release(sys.argv[2])

    else:
        sys.exit(f"ERROR: unknown command '{command}'. Use bump, changelog, or release.")


if __name__ == "__main__":
    main()
