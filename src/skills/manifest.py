"""
skills/manifest.py — Skill manifest validation and dependency checking.

Each skill/plugin can ship a manifest.json declaring metadata,
dependencies, permissions, and version range. On load the manifest is
validated, dependencies are checked against the registry, and invalid
skills are rejected before registration.

Manifest format::

    {
        "name": "my_skill",
        "version": "1.2.0",
        "description": "Does something useful",
        "dependencies": [
            {"name": "web_search", "version_range": ">=1.0.0"}
        ],
        "permissions": ["file_read", "shell"],
        "api_requirements": ["openai"]
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DependencyDecl:
    """A single dependency declaration within a manifest."""

    name: str
    version_range: str = ""


@dataclass(slots=True)
class SkillManifest:
    """Parsed skill manifest with validated fields."""

    name: str
    version: str = "1.0.0"
    description: str = ""
    dependencies: list[DependencyDecl] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    api_requirements: list[str] = field(default_factory=list)


def load_manifest(skill_dir: Path) -> SkillManifest | None:
    """Load and parse a manifest.json from a skill directory.

    Returns None (with a logged warning) if the file is missing or
    cannot be parsed.
    """
    manifest_path = skill_dir / "manifest.json"
    if not manifest_path.is_file():
        log.debug("No manifest.json in %s — skipping validation", skill_dir)
        return None

    try:
        raw: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read manifest in %s: %s", skill_dir, exc)
        return None

    errors = validate_manifest_dict(raw)
    if errors:
        for err in errors:
            log.warning("Manifest validation error in %s: %s", skill_dir, err)
        return None

    deps = [
        DependencyDecl(name=d["name"], version_range=d.get("version_range", ""))
        for d in raw.get("dependencies", [])
    ]

    return SkillManifest(
        name=raw["name"],
        version=raw.get("version", "1.0.0"),
        description=raw.get("description", ""),
        dependencies=deps,
        permissions=raw.get("permissions", []),
        api_requirements=raw.get("api_requirements", []),
    )


def validate_manifest_dict(data: dict[str, Any]) -> list[str]:
    """Validate a raw manifest dict, returning a list of error strings."""
    errors: list[str] = []

    if not isinstance(data.get("name"), str) or not data["name"].strip():
        errors.append("Missing or empty 'name' field")

    version = data.get("version")
    if version is not None and not isinstance(version, str):
        errors.append("'version' must be a string")

    for dep in data.get("dependencies", []):
        if not isinstance(dep, dict):
            errors.append(f"Dependency entry must be an object: {dep}")
            continue
        if not isinstance(dep.get("name"), str) or not dep["name"].strip():
            errors.append("Dependency missing 'name' field")

    if not isinstance(data.get("permissions", []), list):
        errors.append("'permissions' must be a list")

    if not isinstance(data.get("api_requirements", []), list):
        errors.append("'api_requirements' must be a list")

    return errors


def check_dependencies(
    manifest: SkillManifest,
    available_skills: dict[str, str],
) -> list[str]:
    """Return names of unmet dependencies.

    Args:
        manifest: The parsed manifest to check.
        available_skills: Mapping of skill_name → version for all
            currently registered skills.

    Returns:
        List of dependency names that are missing or cannot satisfy
        the declared version range.
    """
    missing: list[str] = []
    for dep in manifest.dependencies:
        if dep.name not in available_skills:
            missing.append(dep.name)
            continue
        if dep.version_range:
            installed = available_skills[dep.name]
            if not _version_satisfies(installed, dep.version_range):
                missing.append(
                    f"{dep.name} (installed {installed}, need {dep.version_range})"
                )
    return missing


def _version_satisfies(installed: str, range_spec: str) -> bool:
    """Minimal version range check (supports >=X.Y.Z).

    Only operator-style ranges (e.g. ``>=1.0.0``) are supported.
    Anything more complex defaults to True to avoid false negatives.
    """
    range_spec = range_spec.strip()
    if range_spec.startswith(">="):
        required = range_spec[2:].strip()
        return _parse_version(installed) >= _parse_version(required)
    if range_spec.startswith(">"):
        required = range_spec[1:].strip()
        return _parse_version(installed) > _parse_version(required)
    if range_spec.startswith("=="):
        required = range_spec[2:].strip()
        return _parse_version(installed) == _parse_version(required)
    # Unknown operator — assume satisfied
    return True


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple."""
    try:
        return tuple(int(p) for p in version.split(".") if p.isdigit())
    except Exception:
        return (0,)
