#!/usr/bin/env python3
"""Verify that test coverage meets or exceeds the stored floor threshold.

Reads ``coverage.xml`` (Cobertura format produced by ``pytest-cov``) and
compares the line-rate percentage against ``.coverage-floor`` at the project
root.  Fails the CI run if coverage has regressed below the floor.

Usage::

    python scripts/check_coverage_floor.py [--update]

Options:

  ``--update``   When coverage exceeds the floor, write the new higher value
                 back to ``.coverage-floor``.  Used on the main branch to
                 automatically ratchet the threshold upward.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_XML = PROJECT_ROOT / "coverage.xml"
COVERAGE_FLOOR_FILE = PROJECT_ROOT / ".coverage-floor"


def _read_floor() -> float:
    """Return the coverage floor percentage from ``.coverage-floor``."""
    raw = COVERAGE_FLOOR_FILE.read_text(encoding="utf-8").strip()
    try:
        return float(raw)
    except ValueError:
        print(f"ERROR: .coverage-floor contains invalid value: {raw!r}")
        sys.exit(1)


def _read_actual() -> float:
    """Return actual coverage percentage from ``coverage.xml``."""
    if not COVERAGE_XML.exists():
        print("ERROR: coverage.xml not found — did pytest generate it?")
        sys.exit(1)

    tree = ET.parse(COVERAGE_XML)
    root = tree.getroot()
    line_rate = float(root.attrib["line-rate"])
    return round(line_rate * 100, 2)


def _update_floor(new_value: float) -> None:
    """Write *new_value* to ``.coverage-floor``."""
    COVERAGE_FLOOR_FILE.write_text(f"{new_value:.0f}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Coverage regression gate")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update .coverage-floor when coverage exceeds the floor",
    )
    args = parser.parse_args()

    floor = _read_floor()
    actual = _read_actual()

    print(f"Coverage floor : {floor:.0f}%")
    print(f"Actual coverage: {actual:.2f}%")

    if actual < floor:
        delta = floor - actual
        print(
            f"\n::error::Coverage regression detected: "
            f"{actual:.2f}% is {delta:.2f}pp below the floor ({floor:.0f}%)."
        )
        print(
            "Add tests to restore coverage, or lower .coverage-floor "
            "if the drop is intentional."
        )
        return 1

    if actual > floor and args.update:
        print(f"Updating .coverage-floor: {floor:.0f}% -> {actual:.0f}%")
        _update_floor(actual)

    margin = actual - floor
    print(f"OK: Coverage is {margin:.2f}pp above the floor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
