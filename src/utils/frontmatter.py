"""
src/utils/frontmatter.py — YAML frontmatter parser for .md instruction files.

Parses and strips YAML frontmatter delimited by ``---`` at the top of
Markdown files. Used by the routing engine and instruction loader to
extract routing configuration from instruction files.

Format::

    ---
    routing:
      id: my-rule
      priority: 1
      fromMe: true
    ---

    # Instruction content starts here

Supports both single and multiple routing entries (list form).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Matches opening ``---`` + content + closing ``---`` at the start of a file
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class ParsedFile:
    """Result of parsing a file with optional frontmatter.

    Attributes:
        metadata: Parsed YAML frontmatter dict (empty if no frontmatter).
        content: File content with frontmatter stripped.
        source: Path of the source file.
    """

    metadata: Dict[str, Any] = field(default_factory=dict)
    content: str = ""
    source: Optional[Path] = None


def parse_frontmatter(text: str) -> ParsedFile:
    """
    Parse YAML frontmatter from a text string.

    If the text starts with ``---``, extracts the YAML block between the
    delimiters and returns the remaining content separately. If no
    frontmatter is found, returns the full text as content with empty metadata.

    Args:
        text: Raw file content, potentially with YAML frontmatter.

    Returns:
        ParsedFile with separated metadata and content.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return ParsedFile(content=text)

    yaml_block = match.group(1)
    remaining = text[match.end() :]

    try:
        import yaml

        metadata = yaml.safe_load(yaml_block)
    except (ImportError, ValueError) as exc:
        log.warning("Failed to parse frontmatter: %s", exc)
        return ParsedFile(content=text)

    if not isinstance(metadata, dict):
        metadata = {}

    return ParsedFile(metadata=metadata, content=remaining)


def parse_file(path: Path) -> ParsedFile:
    """
    Parse a file with optional YAML frontmatter.

    Args:
        path: Path to the .md file.

    Returns:
        ParsedFile with separated metadata and content.
    """
    text = path.read_text(encoding="utf-8")
    result = parse_frontmatter(text)
    result.source = path
    return result


def extract_routing_rules(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract routing rule dicts from parsed frontmatter metadata.

    Supports two forms:
    - Single rule: ``routing: {id: ..., priority: ...}``
    - List form: ``routing: [{...}, {...}]``

    Returns:
        List of routing rule dicts. Empty list if no routing key.
    """
    routing = metadata.get("routing")
    if routing is None:
        return []
    if isinstance(routing, dict):
        return [routing]
    if isinstance(routing, list):
        return routing
    return []


def dump_frontmatter(metadata: Dict[str, Any], content: str) -> str:
    """
    Serialize metadata as YAML frontmatter + content.

    Args:
        metadata: Dict to serialize as YAML.
        content: Body content (leading newlines handled).

    Returns:
        Complete file string with frontmatter.
    """
    import yaml

    yaml_block = yaml.dump(metadata, default_flow_style=False, allow_unicode=True).strip()
    body = content.lstrip("\n")
    return f"---\n{yaml_block}\n---\n\n{body}"
