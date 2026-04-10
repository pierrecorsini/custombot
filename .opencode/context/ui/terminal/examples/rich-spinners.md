<!-- Context: ui/terminal/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Status & Spinners

**Core**: Use `console.status()` for indeterminate operations, `Progress` for known totals.

## Status (Simple Spinner)

```python
with console.status("[green]Processing...") as status:
    do_work()
    status.update("[blue]Next step...")
```

## Custom Spinner

```python
with console.status("Loading...", spinner="monkey"):
    do_work()
```

## Log During Status

```python
with console.status("Working...") as status:
    console.log("Step 1 done")
    console.log("[green]Step 2 done[/]")
```

## Status vs Progress

| Use Case | Tool |
|----------|------|
| Unknown duration | `console.status()` |
| Known step count | `Progress` |
| Multiple operations | `Progress` (multi-task) |
| Need ETA | `Progress` |

## Available Spinners

Run to see all: `python -m rich.spinner`

**Popular**: `dots`, `line`, `monkey`, `hearts`, `arrow`, `bouncingBar`, `dqpb`

## Status API

```python
console.status(
    "Message",
    spinner="dots",       # Spinner type
    spinner_style="blue", # Style
    speed=1.0             # Animation speed
)
```

## Status Methods

| Method | Description |
|--------|-------------|
| `status.update(text)` | Change message |
| `status.stop()` | Pause spinner |
| `status.start()` | Resume |

**Ref**: https://rich.readthedocs.io/en/stable/console.html#status
