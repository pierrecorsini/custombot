<!-- Context: ui/terminal/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Progress Bars

**Core**: `track()` for simple loops, `Progress` class for multi-task/control.

## Quick: track()

```python
from rich.progress import track
for item in track(range(100), description="Processing"):
    do_work(item)
```

## Progress Class

```python
from rich.progress import Progress
with Progress() as progress:
    task = progress.add_task("Downloading", total=1000)
    while not progress.finished:
        progress.update(task, advance=1)
```

## Multiple Tasks

```python
with Progress() as progress:
    t1 = progress.add_task("[red]Download", total=100)
    t2 = progress.add_task("[green]Process", total=100)
    progress.update(t1, advance=5)
    progress.update(t2, advance=3)
```

## Custom Columns

```python
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

with Progress(
    SpinnerColumn(),
    TextColumn("[blue]{task.description}"),
    BarColumn(),
    TimeRemainingColumn(),
) as progress:
    ...
```

## Key Methods

| Method | Description |
|--------|-------------|
| `add_task(desc, total)` | Returns task_id |
| `update(id, advance=N)` | Add N to progress |
| `update(id, completed=N)` | Set to N |
| `start_task(id)` | Start delayed task |
| `remove_task(id)` | Remove task |

## Options

```python
Progress(
    transient=True,        # Remove when done
    refresh_per_second=10,
    expand=True
)
```

## Indeterminate (Unknown Total)

```python
task = progress.add_task("Waiting", total=None)  # Pulsing
```

**Ref**: https://rich.readthedocs.io/en/stable/progress.html
