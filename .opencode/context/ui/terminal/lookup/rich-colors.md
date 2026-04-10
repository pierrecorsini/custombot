<!-- Context: ui/terminal/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Colors & Styles

**Core**: Multiple color systems, style via string or Style class, custom themes.

## Color Systems

| Value | Colors |
|-------|--------|
| `"auto"` | Auto-detect (default) |
| `None` | No colors |
| `"standard"` | 16 colors |
| `"256"` | 256 colors |
| `"truecolor"` | 16.7M colors |

```python
console = Console(color_system="truecolor")
```

## Color Formats

```python
style="red"                    # Named
style="color(5)"               # By number (0-255)
style="#ff0000"                # Hex
style="rgb(255,0,0)"           # RGB
style="red on white"           # Background (prefix "on")
```

## Style Class

```python
from rich.style import Style
danger = Style(color="red", bold=True, blink=True)
console.print("Error!", style=danger)
```

## Custom Theme

```python
from rich.theme import Theme
theme = Theme({"error": "bold red", "success": "green"})
console = Console(theme=theme)
console.print("[error]Failed[/]")
```

## Overflow

```python
console.print(long_text, overflow="fold")      # Wrap
console.print(long_text, overflow="crop")      # Truncate
console.print(long_text, overflow="ellipsis")  # With ...
```

**Ref**: https://rich.readthedocs.io/en/stable/console.html
