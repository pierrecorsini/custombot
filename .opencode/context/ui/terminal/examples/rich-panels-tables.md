<!-- Context: ui/terminal/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-25 -->
# Rich Panels & Tables

**Core**: Panel for bordered content, Table for structured data, Grid for layout.

## Panel

```python
from rich.panel import Panel
print(Panel("Content", title="Title", border_style="red"))
print(Panel.fit("Fits content"))  # vs full width
```

### Panel Options

| Option | Description |
|--------|-------------|
| `title/subtitle` | Header/footer text |
| `box=box.DOUBLE` | Border style |
| `border_style="red"` | Border color |
| `expand=False` | Fit to content |
| `padding=(1,2)` | (vertical, horizontal) |

## Table

```python
from rich.table import Table
table = Table(title="Movies")
table.add_column("Title", style="cyan")
table.add_column("Year", justify="right")
table.add_row("Star Wars", "1977")
console.print(table)
```

### Column Options

```python
table.add_column("Name",
    justify="left",      # left/center/right
    style="magenta",
    width=20,            # Fixed width
    no_wrap=True
)
```

### Table Options

| Option | Description |
|--------|-------------|
| `title/caption` | Header/footer |
| `box=box.MINIMAL` | Border style |
| `show_header=False` | Hide headers |
| `row_styles=["dim",""]` | Zebra striping |
| `expand=True` | Full width |

## Grid (No Borders)

```python
grid = Table.grid(expand=True)
grid.add_column()
grid.add_column(justify="right")
grid.add_row("Left", "Right")
```

## Box Styles

`ASCII`, `SQUARE`, `ROUNDED` (default), `DOUBLE`, `HEAVY`, `MINIMAL`

**Ref**: https://rich.readthedocs.io/en/stable/panel.html
