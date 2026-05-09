"""Load testing harness that simulates N concurrent chats.

Usage::

    from tests.load.load_runner import LoadTestRunner

    runner = LoadTestRunner(num_chats=100, messages_per_chat=3)
    await runner.run(bot, workspace)

    print(runner.report())

Or via Makefile::

    make load-test

Metrics collected:
    - Message processing latency (p50, p95, p99)
    - Error rate (% of failed messages)
    - Throughput (messages/second)
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from src.channels.base import IncomingMessage


@dataclass(slots=True)
class LoadTestResult:
    """Aggregated results from a load test run."""

    total_messages: int = 0
    successful_messages: int = 0
    failed_messages: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def error_rate(self) -> float:
        if self.total_messages == 0:
            return 0.0
        return self.failed_messages / self.total_messages

    @property
    def throughput(self) -> float:
        elapsed = self.end_time - self.start_time
        if elapsed <= 0:
            return 0.0
        return self.successful_messages / elapsed

    def percentile(self, p: float) -> float:
        """Return the p-th percentile latency in seconds."""
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * p / 100.0)
        idx = min(idx, len(sorted_lat) - 1)
        return sorted_lat[idx]

    def report(self) -> str:
        """Format a human-readable summary."""
        lines = [
            "=" * 50,
            "  LOAD TEST RESULTS",
            "=" * 50,
            f"  Total messages:   {self.total_messages}",
            f"  Successful:       {self.successful_messages}",
            f"  Failed:           {self.failed_messages}",
            f"  Error rate:       {self.error_rate:.1%}",
            f"  Throughput:       {self.throughput:.1f} msg/s",
            "",
            "  Latency:",
            f"    p50:  {self.percentile(50) * 1000:.1f} ms",
            f"    p95:  {self.percentile(95) * 1000:.1f} ms",
            f"    p99:  {self.percentile(99) * 1000:.1f} ms",
            f"    min:  {min(self.latencies) * 1000:.1f} ms" if self.latencies else "    min:  N/A",
            f"    max:  {max(self.latencies) * 1000:.1f} ms" if self.latencies else "    max:  N/A",
            "",
            f"  Wall time:        {self.end_time - self.start_time:.2f}s",
            "=" * 50,
        ]
        return "\n".join(lines)


@dataclass(slots=True)
class _ChatSession:
    """Simulates a single chat sending messages at random intervals."""

    chat_id: str
    messages: list[str]
    interval_range: tuple[float, float] = (0.0, 5.0)


class LoadTestRunner:
    """Orchestrates concurrent chat load tests.

    Parameters
    ----------
    num_chats:
        Number of concurrent chat sessions (default 100).
    messages_per_chat:
        Messages each chat sends (default 3).
    interval_range:
        Min/max seconds between messages in each chat (default 0-5).
    """

    def __init__(
        self,
        num_chats: int = 100,
        messages_per_chat: int = 3,
        interval_range: tuple[float, float] = (0.0, 5.0),
    ) -> None:
        self._num_chats = num_chats
        self._messages_per_chat = messages_per_chat
        self._interval_range = interval_range
        self._result = LoadTestResult()

    @property
    def result(self) -> LoadTestResult:
        return self._result

    async def run(
        self,
        handle_message_fn: Any,
        workspace: Path,
    ) -> LoadTestResult:
        """Execute the load test.

        Parameters
        ----------
        handle_message_fn:
            Async callable that accepts an ``IncomingMessage`` and returns
            a response string (e.g. ``bot.handle_message``).
        workspace:
            Path used for generating chat IDs and message IDs.
        """
        self._result = LoadTestResult()
        rng = random.Random(42)

        sessions = self._build_sessions(rng)

        self._result.start_time = time.monotonic()

        tasks = [
            self._run_session(handle_message_fn, session, rng)
            for session in sessions
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

        self._result.end_time = time.monotonic()
        return self._result

    def report(self) -> str:
        """Format results summary."""
        return self._result.report()

    def _build_sessions(self, rng: random.Random) -> list[_ChatSession]:
        """Create chat sessions with random message content."""
        sessions = []
        for i in range(self._num_chats):
            chat_id = f"chat-load-{i:04d}"
            messages = [
                f"Load test message {j} from chat {i}"
                for j in range(self._messages_per_chat)
            ]
            sessions.append(_ChatSession(
                chat_id=chat_id,
                messages=messages,
                interval_range=self._interval_range,
            ))
        return sessions

    async def _run_session(
        self,
        handle_message_fn: Any,
        session: _ChatSession,
        rng: random.Random,
    ) -> None:
        """Execute one chat session: send messages at random intervals."""
        for j, text in enumerate(session.messages):
            # Random delay between messages
            delay = rng.uniform(*session.interval_range)
            if delay > 0:
                await asyncio.sleep(delay)

            msg = IncomingMessage(
                message_id=f"msg-load-{session.chat_id}-{j:04d}",
                chat_id=session.chat_id,
                sender_id=f"user-{session.chat_id}",
                sender_name=f"User-{session.chat_id}",
                text=text,
                timestamp=time.time(),
                acl_passed=True,
            )

            self._result.total_messages += 1
            start = time.monotonic()

            try:
                await handle_message_fn(msg)
                latency = time.monotonic() - start
                self._result.latencies.append(latency)
                self._result.successful_messages += 1
            except Exception as exc:
                latency = time.monotonic() - start
                self._result.latencies.append(latency)
                self._result.failed_messages += 1
                self._result.errors.append(f"{session.chat_id}/{j}: {exc}")
