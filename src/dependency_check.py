"""
src/dependency_check.py — Verify and auto-update dependencies at startup.

Ensures all packages from requirements.txt are installed and up-to-date,
with special emphasis on the WhatsApp library (neonize) which must stay
current for reliable communication.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from src.ui.cli_output import cli as cli_output

REQUIREMENTS_FILE = Path(__file__).resolve().parent.parent / "requirements.txt"

CRITICAL_PACKAGES = {"neonize"}


def _parse_requirements(path: Path) -> list[tuple[str, Optional[str]]]:
    """Parse requirements.txt → list of (package_name, min_version_or_None)."""
    packages: list[tuple[str, Optional[str]]] = []
    if not path.exists():
        return packages
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*>=?\s*([0-9][0-9A-Za-z.*-]*)", line)
        if match:
            packages.append((match.group(1).lower(), match.group(2)))
        else:
            name = re.split(r"[<>=!~\[]", line)[0].strip()
            if name:
                packages.append((name.lower(), None))
    return packages


def _pip_installed_versions(package_names: list[str]) -> dict[str, Optional[str]]:
    """Query pip for currently installed versions of the given packages."""
    if not package_names:
        return {}
    cmd = [sys.executable, "-m", "pip", "show"] + package_names
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError:
        return {n: None for n in package_names}

    versions: dict[str, Optional[str]] = {}
    current_name: Optional[str] = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Name:"):
            current_name = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Version:") and current_name:
            versions[current_name] = line.split(":", 1)[1].strip()
            current_name = None

    for n in package_names:
        if n not in versions:
            versions[n] = None
    return versions


def _pip_upgrade(packages: list[str]) -> tuple[list[str], list[str]]:
    """Run pip install --upgrade on the given packages.

    Returns (upgraded, failed) lists of package names.
    """
    if not packages:
        return [], []
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet"] + packages
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("pip upgrade failed: %s", exc)
        return [], packages

    failed = []
    if result.returncode != 0:
        logging.getLogger(__name__).warning(
            "pip upgrade returned %d: %s", result.returncode, result.stderr.strip()
        )
        for pkg in packages:
            if f"Failed to build" in result.stderr or result.returncode != 0:
                if pkg.lower() in result.stderr.lower():
                    failed.append(pkg)
        if not failed:
            failed = packages

    upgraded = [p for p in packages if p not in failed]
    return upgraded, failed


def check_dependencies(
    auto_update: bool = True,
    critical_only: bool = False,
) -> bool:
    """Check and optionally update dependencies.

    Args:
        auto_update: If True, automatically upgrade outdated packages.
        critical_only: If True, only check/update CRITICAL_PACKAGES (neonize).

    Returns:
        True if all checked packages are satisfactory, False otherwise.
    """
    log = logging.getLogger(__name__)
    requirements = _parse_requirements(REQUIREMENTS_FILE)

    if critical_only:
        requirements = [(n, v) for n, v in requirements if n in CRITICAL_PACKAGES]

    if not requirements:
        log.debug("No requirements to check")
        return True

    names = [name for name, _ in requirements]
    installed = _pip_installed_versions(names)

    missing: list[str] = []
    outdated: list[str] = []

    for name, min_ver in requirements:
        ver = installed.get(name)
        if ver is None:
            missing.append(name)
        elif min_ver:
            try:
                from packaging.version import Version

                if Version(ver) < Version(min_ver):
                    outdated.append(name)
            except (ValueError, TypeError):
                if ver != min_ver:
                    outdated.append(name)

    if not missing and not outdated:
        log.debug("All dependencies up-to-date")
        return True

    all_issues = missing + outdated

    label = "critical " if critical_only else ""
    if missing:
        cli_output.warning(f"Missing {label}dependencies: {', '.join(missing)}")
    if outdated:
        cli_output.warning(f"Outdated {label}dependencies: {', '.join(outdated)}")

    if not auto_update:
        return False

    cli_output.loading(f"Updating {label}dependencies...")
    upgraded, failed = _pip_upgrade(all_issues)

    if upgraded:
        cli_output.success(f"Updated: {', '.join(upgraded)}")

    if failed:
        cli_output.error(f"Failed to update: {', '.join(failed)}")
        return False

    return True
