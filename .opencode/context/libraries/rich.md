### Printing Above Progress Display

Source: https://github.com/textualize/rich/blob/master/docs/source/progress.md

Illustrates how to print messages above the progress bar using the internal console object of the Progress instance. This is useful for status updates or logging during task execution.

```python
with Progress() as progress:
    task = progress.add_task("twiddling thumbs", total=10)
    for job in range(10):
        progress.console.print(f"Working on job #{job}")
        run_job(job)
        progress.advance(task)
```

--------------------------------

### Print to Live Console

Source: https://github.com/textualize/rich/blob/master/docs/source/live.md

Use `live.console.print()` to display messages above the live content. This is useful for showing progress or status updates during a live display.

```python
import time

from rich.live import Live
from rich.table import Table

table = Table()
table.add_column("Row ID")
table.add_column("Description")
table.add_column("Level")

with Live(table, refresh_per_second=4) as live:
    for row in range(12):
        live.console.print(f"Working on row #{row}")
        time.sleep(0.4)
        table.add_row(f"{row}", f"description {row}", "[red]ERROR")
```

--------------------------------

### Create and Print a Rich Table

Source: https://github.com/textualize/rich/blob/master/docs/source/tables.md

Construct a Table object, add columns and rows, and print it to the console. This is the basic usage for displaying tabular data.

```python
from rich.console import Console
from rich.table import Table

table = Table(title="Star Wars Movies")

table.add_column("Released", justify="right", style="cyan", no_wrap=True)
table.add_column("Title", style="magenta")
table.add_column("Box Office", justify="right", style="green")

table.add_row("Dec 20, 2019", "Star Wars: The Rise of Skywalker", "$952,110,690")
table.add_row("May 25, 2018", "Solo: A Star Wars Story", "$393,151,347")
table.add_row("Dec 15, 2017", "Star Wars Ep. V111: The Last Jedi", "$1,332,539,889")
table.add_row("Dec 16, 2016", "Rogue One: A Star Wars Story", "$1,332,439,889")

console = Console()
console.print(table)
```

--------------------------------

### Create Tables with Rich

Source: https://github.com/textualize/rich/blob/master/README.md

Generate flexible tables with Unicode box characters and various formatting options for borders, styles, and alignment. Table columns can be configured to display different data types and support console markup.

```python
from rich.console import Console
from rich.table import Table

console = Console()

table = Table(show_header=True, header_style="bold magenta")
table.add_column("Date", style="dim", width=12)
table.add_column("Title")
table.add_column("Production Budget", justify="right")
table.add_column("Box Office", justify="right")
table.add_row(
    "Dec 20, 2019", "Star Wars: The Rise of Skywalker", "$275,000,000", "$375,126,118"
)
table.add_row(
    "May 25, 2018",
    "[red]Solo[/red]: A Star Wars Story",
    "$275,000,000",
    "$393,151,347",
)
table.add_row(
    "Dec 15, 2017",
    "Star Wars Ep. VIII: The Last Jedi",
    "$262,000,000",
    "[bold]$1,332,539,889[/bold]",
)

console.print(table)
```

--------------------------------

### Rich Console Output Interface

Source: https://context7.com/textualize/rich/llms.txt

The `Console` class is the primary interface for Rich output, offering methods for styled printing, logging with timestamps, capturing output, and rendering various elements.

```python
from rich.console import Console

console = Console()

# Basic printing with styles
console.print("Hello", "World!", style="bold red")
console.print("Where there is a [bold cyan]Will[/bold cyan] there [u]is[/u] a [i]way[/i].")

# Logging with timestamp and source location
console.log("Server started", log_locals=False)
console.log({"status": "running", "port": 8080}, log_locals=True)

# Print with different justification
console.print("Left aligned", justify="left")
console.print("Center aligned", justify="center")
console.print("Right aligned", justify="right")

# Capture output as string
with console.capture() as capture:
    console.print("This will be captured")
captured_output = capture.get()

# Export to HTML
console.print("[bold blue]Hello[/bold blue] World")
html_output = console.export_html()

# Print rule/separator
console.rule("[bold red]Section Title")

# Input with prompt
name = console.input("[bold]Enter your name: [/bold]")

# Control terminal features
console.clear()  # Clear screen
console.bell()   # Sound bell
console.set_window_title("My Application")
```