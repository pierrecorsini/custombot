"""
src/ui/rich_messages.py — Rich message formatting for WhatsApp.

Provides text-based rich message builders (lists, buttons, carousels)
that degrade gracefully on WhatsApp's plain-text renderer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RichList:
    """Structured list with title and rows."""

    title: str
    rows: list[str]


@dataclass(frozen=True)
class RichButton:
    """Text with numbered selectable options."""

    text: str
    buttons: list[str]


@dataclass(frozen=True)
class RichCarousel:
    """Scrollable card list (text fallback: numbered sections)."""

    cards: list[dict[str, str]]


class ListBuilder:
    """Build a RichList incrementally."""

    def __init__(self, title: str) -> None:
        self._title = title
        self._rows: list[str] = []

    def add_row(self, text: str) -> "ListBuilder":
        self._rows.append(text)
        return self

    def build(self) -> RichList:
        return RichList(title=self._title, rows=list(self._rows))


class ButtonBuilder:
    """Build a RichButton incrementally."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._buttons: list[str] = []

    def add_button(self, label: str) -> "ButtonBuilder":
        self._buttons.append(label)
        return self

    def build(self) -> RichButton:
        return RichButton(text=self._text, buttons=list(self._buttons))


# ── Formatting functions ─────────────────────────────────────────────────────


def format_as_rich_list(title: str, items: list[str]) -> str:
    """Format *items* as a WhatsApp-compatible text list."""
    if not items:
        return f"*{title}*"

    lines = [f"*{title}*", ""]
    for i, item in enumerate(items, 1):
        lines.append(f"  {i}. {item}")
    return "\n".join(lines)


def format_as_buttons(text: str, options: list[str]) -> str:
    """Format *text* with numbered selectable options."""
    if not options:
        return text

    lines = [text, ""]
    for i, opt in enumerate(options, 1):
        lines.append(f"  {i}. {opt}")
    return "\n".join(lines)


def format_as_carousel(cards: list[dict[str, str]]) -> str:
    """Format *cards* as numbered sections with title + description."""
    if not cards:
        return ""

    parts: list[str] = []
    for i, card in enumerate(cards, 1):
        title = card.get("title", "")
        desc = card.get("description", "")
        entry = f"*{i}. {title}*"
        if desc:
            entry += f"\n   {desc}"
        parts.append(entry)
    return "\n\n".join(parts)


def format_response(text: str, format_type: str = "plain") -> str:
    """Apply *format_type* formatting to *text*.

    Supported types: ``plain``, ``list``, ``button``, ``carousel``.
    For structured types, *text* is returned as-is (structured data must
    be formatted via the dedicated functions above).
    """
    if format_type == "plain":
        return text
    # For structured formats the caller should use the specific formatters.
    return text
