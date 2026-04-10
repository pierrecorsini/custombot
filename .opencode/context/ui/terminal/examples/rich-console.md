<!-- Context: ui/terminal/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Console Integration

**Core**: Create Console with custom settings, use with Progress/Status. Thread-safe.

## Basic Setup

```python
from rich.console import Console
console = Console(width=120, style="bold blue")
```

## Console Options

| Option | Description |
|--------|-------------|
| `width/height` | Dimensions (None = auto) |
| `file` | Output file (default: stdout) |
| `stderr=True` | Print to stderr |
| `record=True` | Capture for export |
| `no_color=True` | Disable colors |

## Key Methods

```python
console.print("[bold red]Error[/]")  # Styled output
console.log("Message")               # With timestamp
console.rule("Section")              # Divider
console.status("Working...")         # Spinner
```

## With Progress

```python
from rich.progress import Progress
with Progress(console=console) as progress:
    task = progress.add_task("Processing", total=100)
```

## Thread Safety

Console is thread-safe. Safe to use from multiple threads.

## Async Status

```python
async def process():
    with console.status("[green]Processing...") as status:
        for item in items:
            status.update(f"Item {item}")
            await do_work(item)
```

**Ref**: https://rich.readthedocs.io/en/stable/console.html
