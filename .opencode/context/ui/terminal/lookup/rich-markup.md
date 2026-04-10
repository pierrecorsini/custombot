<!-- Context: ui/terminal/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Markup Syntax

**Core**: BBCode-style syntax `[style]text[/]` for styling console output.

## Basic Syntax

```python
print("[bold red]alert![/] normal text")
print("[bold][red]both[/]")  # Shorthand close
```

## Style Attributes

| Style | Syntax |
|-------|--------|
| Bold | `[bold]` or `[b]` |
| Italic | `[italic]` or `[i]` |
| Underline | `[underline]` or `[u]` |
| Strike | `[strike]` or `[s]` |
| Blink | `[blink]` |
| Dim | `[dim]` |

## Colors

```python
"[red]text[/]"           # Named color
"[on white]text[/]"      # Background
"[#ff0000]text[/]"       # Hex
"[bold red on white]"    # Combined
```

## Links & Emoji

```python
"[link=https://example.com]click[/link]"
":warning:"   # ⚠️
":smiley:"    # 😃
```

## Escaping

```python
from rich.markup import escape
print(escape(user_input))  # Safe
print(r"literal\[bracket]")  # Raw string
```

## Disable Markup

```python
console.print("[not markup]", markup=False)
console = Console(markup=False)
```

**Ref**: https://rich.readthedocs.io/en/stable/markup.html
