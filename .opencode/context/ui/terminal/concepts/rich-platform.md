<!-- Context: ui/terminal/concepts | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Cross-Platform

**Core**: Rich auto-detects terminal capabilities. Works on macOS, Linux, Windows.

## Platform Support

- **macOS/Linux**: Full support
- **Windows**: Full in Windows Terminal, limited in cmd.exe
- **Python**: 3.8+

## Auto-Detection

Rich automatically strips colors when piped or redirected:
```bash
python script.py | grep foo  # Plain text output
```

## Environment Variables

| Variable | Effect |
|----------|--------|
| `NO_COLOR=1` | Disable all colors |
| `FORCE_COLOR=1` | Force colors on |
| `TERM=dumb` | Disable styling |
| `TTY_COMPATIBLE=1` | CI/GitHub Actions |

## Manual Control

```python
console = Console(force_terminal=True)     # Force colors
console = Console(force_interactive=True)  # Force animations
console = Console(color_system=None)       # Disable colors

if console.is_terminal:
    print("In terminal")
```

## File Output

```python
with open("out.txt", "w") as f:
    console = Console(file=f)  # Plain text
    console = Console(file=f, force_terminal=True)  # With ANSI
```

## Testing

```python
with console.capture() as capture:
    console.print("[bold]test[/]")
output = capture.get()
```

**Ref**: https://rich.readthedocs.io/en/stable/console.html
