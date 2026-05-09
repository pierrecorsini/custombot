"""src/skills/sandbox.py — Configurable skill sandboxing with resource limits.

Provides a ``SkillSandbox`` that wraps skill execution with:
- Maximum execution time (wall-clock timeout)
- Maximum output size (response truncation)
- Maximum tool iterations (prevents infinite loops)

Usage::

    from src.skills.sandbox import SkillSandbox, SandboxConfig

    config = SandboxConfig(max_time_seconds=30, max_output_chars=4000)
    sandbox = SkillSandbox(config)
    result = await sandbox.execute(skill_fn, *args, **kwargs)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class SandboxConfig:
    """Configuration for skill sandboxing.

    Attributes:
        max_time_seconds: Wall-clock timeout for skill execution.
            0 means no timeout (unlimited).
        max_output_chars: Maximum output length in characters.
            Output is truncated if exceeded.  0 means no limit.
        max_tool_iterations: Maximum number of tool calls allowed.
            0 means no limit (uses the bot's global cap).
    """

    max_time_seconds: float = 30.0
    max_output_chars: int = 4096
    max_tool_iterations: int = 0


class SandboxViolation(Exception):
    """Raised when a skill exceeds its sandbox resource limits."""

    def __init__(self, limit_type: str, limit_value: float | int, actual: float | int) -> None:
        self.limit_type = limit_type
        self.limit_value = limit_value
        self.actual = actual
        super().__init__(
            f"Skill exceeded {limit_type} limit: "
            f"{actual} > {limit_value}"
        )


class SkillSandbox:
    """Configurable sandbox for skill execution with resource limits.

    Wraps an async callable with timeout enforcement and output
    truncation.  Designed to be used by the tool executor to
    constrain individual skill invocations.
    """

    __slots__ = ("_config",)

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    async def execute(
        self,
        fn: asyncio.coroutine,
        *args: object,
        **kwargs: object,
    ) -> str:
        """Execute *fn* within the sandbox constraints.

        Applies:
        1. Wall-clock timeout (``max_time_seconds``)
        2. Output truncation (``max_output_chars``)

        Returns:
            The function's string result, possibly truncated.

        Raises:
            SandboxViolation: When timeout is exceeded.
        """
        config = self._config

        # Apply timeout constraint
        if config.max_time_seconds > 0:
            try:
                result = await asyncio.wait_for(
                    fn(*args, **kwargs),  # type: ignore[call-arg]
                    timeout=config.max_time_seconds,
                )
            except asyncio.TimeoutError:
                raise SandboxViolation(
                    "max_time_seconds",
                    config.max_time_seconds,
                    config.max_time_seconds,  # exactly at limit
                )
        else:
            result = await fn(*args, **kwargs)  # type: ignore[call-arg]

        # Apply output truncation
        output = str(result) if result is not None else ""
        if config.max_output_chars > 0 and len(output) > config.max_output_chars:
            output = output[: config.max_output_chars] + "\n[truncated by sandbox]"
            log.debug(
                "Skill output truncated: %d → %d chars",
                len(str(result)),
                config.max_output_chars,
            )

        return output

    def check_tool_iterations(self, count: int) -> None:
        """Check if tool iteration count exceeds the sandbox limit.

        Raises:
            SandboxViolation: When iteration count exceeds the limit.
        """
        if self._config.max_tool_iterations > 0 and count > self._config.max_tool_iterations:
            raise SandboxViolation(
                "max_tool_iterations",
                self._config.max_tool_iterations,
                count,
            )
