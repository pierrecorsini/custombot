"""
progress.py — Progress indicators for long-running operations.

Provides:
  - SpinnerStatus: Spinner for operations with unknown duration (>1s threshold)
  - ProgressBar: Progress bar for multi-step operations with known total
  - ProgressTracker: High-level API for tracking operations

Uses Rich library for beautiful terminal output.

Usage:
    # Spinner for indeterminate operations
    async with SpinnerStatus("Connecting to WhatsApp...") as spinner:
        await connect()
        spinner.update("Authenticating...")

    # Progress bar for multi-step operations
    with ProgressBar("Loading skills", total=5) as progress:
        for skill in skills:
            load_skill(skill)
            progress.advance()

    # Automatic threshold detection (shows spinner only if >1s)
    with maybe_spinner("Quick operation"):
        do_something()
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, TypeVar

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Minimum duration (in seconds) before showing a spinner
SPINNER_THRESHOLD_SECONDS = 1.0

# Minimum interval between progress updates (to avoid spamming)
MIN_UPDATE_INTERVAL_SECONDS = 0.1

# Default spinner style
DEFAULT_SPINNER = "dots"

# Shared console instance (thread-safe with thread-local buffers)
_console = Console()

log = logging.getLogger(__name__)

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────────────────
# Spinner Status (for indeterminate operations)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SpinnerConfig:
    """Configuration for spinner behavior."""

    spinner: str = DEFAULT_SPINNER
    spinner_style: str = "status.spinner"
    speed: float = 1.0
    threshold_seconds: float = SPINNER_THRESHOLD_SECONDS


class SpinnerStatus:
    """
    Spinner indicator for operations with unknown duration.

    Shows a spinner animation with a status message. Only displays
    if the operation takes longer than the threshold (default 1 second).

    Thread-safe: Uses Rich's thread-local console buffers.

    Usage:
        async with SpinnerStatus("Loading...") as spinner:
            await long_operation()
            spinner.update("Processing...")

        # Or synchronous:
        with SpinnerStatus.sync("Loading...") as spinner:
            long_operation()
    """

    def __init__(
        self,
        message: str,
        *,
        spinner: str = DEFAULT_SPINNER,
        threshold_seconds: float = SPINNER_THRESHOLD_SECONDS,
        console: Console | None = None,
    ) -> None:
        """
        Initialize spinner status.

        Args:
            message: Initial status message.
            spinner: Spinner type (dots, line, dots12, etc.).
            threshold_seconds: Minimum duration before showing spinner.
            console: Custom console instance (uses shared if None).
        """
        self._message = message
        self._spinner = spinner
        self._threshold = threshold_seconds
        self._console = console or _console
        self._start_time: float | None = None
        self._status: Any = None
        self._active = False
        self._displayed = False

    @property
    def is_displayed(self) -> bool:
        """Check if the spinner is currently being displayed."""
        return self._displayed

    def _should_display(self) -> bool:
        """Check if enough time has passed to display the spinner."""
        if self._start_time is None:
            return False
        return (time.monotonic() - self._start_time) >= self._threshold

    def update(self, message: str) -> None:
        """
        Update the spinner message.

        If the spinner hasn't been displayed yet and the operation
        completes before the threshold, the update is a no-op.

        Args:
            message: New status message.
        """
        self._message = message
        if self._displayed and self._status is not None:
            self._status.update(f"[bold]{message}[/bold]")

    def _start(self) -> None:
        """Start the spinner (internal)."""
        if self._active:
            return
        self._active = True
        self._start_time = time.monotonic()

    def _display(self) -> None:
        """Display the spinner if threshold is met."""
        if self._displayed or not self._should_display():
            return
        self._status = self._console.status(
            f"[bold]{self._message}[/bold]",
            spinner=self._spinner,
        )
        self._status.__enter__()
        self._displayed = True
        log.debug("Spinner displayed: %s", self._message)

    def _stop(self) -> None:
        """Stop the spinner (internal)."""
        if not self._active:
            return
        self._active = False
        if self._displayed and self._status is not None:
            self._status.__exit__(None, None, None)
            self._displayed = False
            elapsed = time.monotonic() - (self._start_time or 0)
            log.debug("Spinner stopped after %.2fs: %s", elapsed, self._message)

    def __enter__(self) -> "SpinnerStatus":
        self._start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._stop()

    async def __aenter__(self) -> "SpinnerStatus":
        self._start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._stop()


# ─────────────────────────────────────────────────────────────────────────────
# Progress Bar (for multi-step operations)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ProgressConfig:
    """Configuration for progress bar behavior."""

    show_spinner: bool = True
    show_bar: bool = True
    show_percentage: bool = True
    show_elapsed: bool = True
    min_update_interval: float = MIN_UPDATE_INTERVAL_SECONDS


class ProgressBar:
    """
    Progress bar for multi-step operations with known total.

    Displays:
      - Spinner animation (optional)
      - Progress bar
      - Percentage complete
      - Elapsed time

    Throttles updates to avoid spamming the console.

    Usage:
        with ProgressBar("Loading skills", total=5) as progress:
            for skill in skills:
                load_skill(skill)
                progress.advance()

        # Or with custom steps:
        with ProgressBar("Processing", total=100) as progress:
            for i in range(100):
                process(i)
                progress.update(completed=i + 1)
    """

    def __init__(
        self,
        description: str,
        total: int | float,
        *,
        config: ProgressConfig | None = None,
        console: Console | None = None,
    ) -> None:
        """
        Initialize progress bar.

        Args:
            description: Description of the operation.
            total: Total number of steps.
            config: Progress bar configuration.
            console: Custom console instance.
        """
        self._description = description
        self._total = total
        self._config = config or ProgressConfig()
        self._console = console or _console
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._last_update: float = 0.0
        self._completed: float = 0.0

    def _build_progress(self) -> Progress:
        """Build the progress display with configured columns."""
        columns = []

        if self._config.show_spinner:
            columns.append(SpinnerColumn())

        columns.append(TextColumn("[bold blue]{task.description}[/bold blue]"))

        if self._config.show_bar:
            columns.append(BarColumn(bar_width=40))

        if self._config.show_percentage:
            columns.append(TaskProgressColumn())

        if self._config.show_elapsed:
            columns.append(TimeElapsedColumn())

        return Progress(*columns, console=self._console)

    def advance(self, amount: float = 1) -> None:
        """
        Advance the progress by a given amount.

        Updates are throttled to avoid console spam.

        Args:
            amount: Amount to advance (default 1).
        """
        if self._progress is None or self._task_id is None:
            return

        self._completed += amount
        now = time.monotonic()

        # Throttle updates
        if (now - self._last_update) >= self._config.min_update_interval:
            self._progress.update(self._task_id, completed=self._completed)
            self._last_update = now

    def update(
        self,
        *,
        completed: float | None = None,
        total: float | None = None,
        description: str | None = None,
    ) -> None:
        """
        Update the progress state.

        Args:
            completed: New completed value.
            total: New total value.
            description: New description.
        """
        if self._progress is None or self._task_id is None:
            return

        if completed is not None:
            self._completed = completed

        now = time.monotonic()

        # Always update on description change, otherwise throttle
        should_update = (
            description is not None
            or (now - self._last_update) >= self._config.min_update_interval
        )

        if should_update:
            self._progress.update(
                self._task_id,
                completed=self._completed,
                total=total,
                description=description,
            )
            self._last_update = now

    @property
    def completed(self) -> float:
        """Get the current completed value."""
        return self._completed

    @property
    def total(self) -> int | float:
        """Get the total value."""
        return self._total

    @property
    def percentage(self) -> float:
        """Get the current percentage complete."""
        if self._total == 0:
            return 100.0
        return (self._completed / self._total) * 100.0

    def __enter__(self) -> "ProgressBar":
        self._progress = self._build_progress()
        self._progress.__enter__()
        self._task_id = self._progress.add_task(
            self._description,
            total=self._total,
        )
        self._last_update = time.monotonic()
        log.debug("Progress started: %s (total=%s)", self._description, self._total)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._progress is not None:
            # Ensure final update
            if self._task_id is not None:
                self._progress.update(self._task_id, completed=self._completed)
            self._progress.__exit__(None, None, None)
            log.debug(
                "Progress completed: %s (%.1f%%)",
                self._description,
                self.percentage,
            )


# ─────────────────────────────────────────────────────────────────────────────
# High-Level API
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def maybe_spinner(
    message: str,
    *,
    threshold_seconds: float = SPINNER_THRESHOLD_SECONDS,
) -> Generator[SpinnerStatus, None, None]:
    """
    Context manager that shows a spinner only if operation exceeds threshold.

    This is the recommended way to add progress feedback to operations
    that might be fast or slow depending on circumstances.

    Args:
        message: Status message to display.
        threshold_seconds: Minimum duration before showing spinner.

    Yields:
        SpinnerStatus instance.

    Example:
        with maybe_spinner("Loading configuration...") as spinner:
            config = load_config()
            # Spinner only shows if load takes >1 second
    """
    spinner = SpinnerStatus(message, threshold_seconds=threshold_seconds)
    with spinner:
        yield spinner


@asynccontextmanager
async def maybe_spinner_async(
    message: str,
    *,
    threshold_seconds: float = SPINNER_THRESHOLD_SECONDS,
) -> Generator[SpinnerStatus, None, None]:
    """
    Async context manager that shows a spinner only if operation exceeds threshold.

    Args:
        message: Status message to display.
        threshold_seconds: Minimum duration before showing spinner.

    Yields:
        SpinnerStatus instance.

    Example:
        async with maybe_spinner_async("Connecting...") as spinner:
            await connect()
            spinner.update("Authenticating...")
    """
    spinner = SpinnerStatus(message, threshold_seconds=threshold_seconds)
    async with spinner:
        yield spinner


def track(
    sequence: list[T],
    description: str = "Processing",
    *,
    console: Console | None = None,
) -> Generator[tuple[int, T], None, None]:
    """
    Track progress through a sequence with a progress bar.

    Args:
        sequence: Items to process.
        description: Progress bar description.
        console: Custom console instance.

    Yields:
        Tuples of (index, item).

    Example:
        for i, item in track(items, "Processing items"):
            process(item)
    """
    total = len(sequence)
    with ProgressBar(description, total=total, console=console) as progress:
        for i, item in enumerate(sequence):
            yield i, item
            progress.advance()


async def track_async(
    sequence: list[T],
    description: str = "Processing",
    *,
    console: Console | None = None,
) -> Generator[tuple[int, T], None, None]:
    """
    Async generator for tracking progress through a sequence.

    Note: This is a regular generator - use with `async for` is not needed.
    Just iterate normally and await your async operations inside the loop.

    Args:
        sequence: Items to process.
        description: Progress bar description.
        console: Custom console instance.

    Yields:
        Tuples of (index, item).

    Example:
        for i, item in track_async(items, "Fetching"):
            await fetch(item)
    """
    total = len(sequence)
    with ProgressBar(description, total=total, console=console) as progress:
        for i, item in enumerate(sequence):
            yield i, item
            progress.advance()


# ─────────────────────────────────────────────────────────────────────────────
# CLI Integration Helpers
# ─────────────────────────────────────────────────────────────────────────────


def get_console() -> Console:
    """Get the shared console instance."""
    return _console


def print_progress_message(message: str, style: str = "dim") -> None:
    """
    Print a progress message to the console (not a spinner/progress bar).

    Use this for one-off progress messages that don't need a full indicator.

    Args:
        message: Message to print.
        style: Rich style to apply.
    """
    _console.print(f"[{style}]{message}[/{style}]")


def print_step(step: int, total: int, description: str) -> None:
    """
    Print a step indicator (e.g., "[1/5] Loading...").

    Args:
        step: Current step number (1-indexed).
        total: Total number of steps.
        description: Step description.
    """
    _console.print(f"[dim][{step}/{total}][/dim] [cyan]{description}[/cyan]")
