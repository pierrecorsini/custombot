#!/usr/bin/env python3
"""Validate PLAN.md checkbox syntax.

Ensures every checkbox item uses the correct format:
  - ``- [ ]`` for uncompleted items
  - ``- [x]`` for completed items

Detects malformed variants like ``- [X]``, ``- [x ]``, ``- [ x]``, etc.
"""

import re
import sys
from pathlib import Path

PLAN_PATH = Path(__file__).resolve().parent.parent / "PLAN.md"

# Valid checkbox: exactly "- [ ] " or "- [x] " at start of line.
_VALID_CHECKBOX = re.compile(r"^- \[(?: |x)\] ")

# Lines that look like checkbox attempts but aren't valid.
_LIKE_CHECKBOX = re.compile(r"^- \[")


def main() -> int:
    if not PLAN_PATH.exists():
        print(f"ERROR: {PLAN_PATH} not found")
        return 1

    lines = PLAN_PATH.read_text(encoding="utf-8").splitlines()
    errors: list[str] = []
    completed = 0
    uncompleted = 0

    for i, line in enumerate(lines, 1):
        if not _LIKE_CHECKBOX.match(line):
            continue

        if _VALID_CHECKBOX.match(line):
            if "[x]" in line[:6]:
                completed += 1
            else:
                uncompleted += 1
        else:
            errors.append(f"  line {i}: {line.rstrip()}")

    if errors:
        print("ERROR: malformed checkbox lines in PLAN.md:")
        for e in errors:
            print(e)
        print()
        print("Expected format: '- [ ] ...' or '- [x] ...'")
        return 1

    total = completed + uncompleted
    print("OK: PLAN.md checkbox syntax valid")
    print(f"  Completed  : {completed}")
    print(f"  Uncompleted: {uncompleted}")
    print(f"  Total      : {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
