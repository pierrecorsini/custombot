"""
async_executor.py — Unified subprocess handling with timeout and logging.

Consolidates subprocess execution patterns across the codebase into a single,
well-tested utility. Supports both shell and exec modes with proper timeout
handling, error capture, and execution logging.

Usage:
    executor = AsyncExecutor(timeout=30.0, log_execution=True)

    # Exec mode (safer, preferred)
    result = await executor.run(["git", "status"])

    # Shell mode (for complex shell commands)
    result = await executor.run("echo hello | grep h", shell=True)

    if result.success:
        print(result.stdout)
    else:
        print(f"Error: {result.stderr}")
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Mapping, Optional

from src.utils.logging_utils import log_execution

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutorResult:
    """Result of a subprocess execution."""

    stdout: str
    stderr: str
    return_code: int
    success: bool
    timed_out: bool


class AsyncExecutor:
    """
    Unified async subprocess executor with timeout and logging.

    Handles both shell and exec modes with proper error handling and
    output capture. Uses @log_execution decorator for consistent logging.
    """

    def __init__(self, timeout: float = 30.0, log_execution_flag: bool = True):
        """
        Initialize the executor.

        Args:
            timeout: Default timeout in seconds for command execution.
            log_execution_flag: Whether to log execution details.
        """
        self._default_timeout = timeout
        self._log_execution = log_execution_flag

    @log_execution(level="debug", log_args=True)
    async def run(
        self,
        command: str | list[str],
        shell: bool = False,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> ExecutorResult:
        """
        Execute a command asynchronously.

        Args:
            command: Command to execute (string for shell mode, list for exec mode).
            shell: If True, run command through shell (less safe, but allows pipes/etc).
            timeout: Timeout in seconds (uses default if not specified).
            cwd: Working directory for the command.
            env: Environment variables for the subprocess (merged with current env).

        Returns:
            ExecutorResult with stdout, stderr, return_code, success, and timed_out.
        """
        timeout = timeout if timeout is not None else self._default_timeout

        # Merge provided env with current environment
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        try:
            if shell:
                proc = await asyncio.create_subprocess_shell(
                    command if isinstance(command, str) else " ".join(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=process_env,
                )
            else:
                cmd_list = command if isinstance(command, list) else [command]
                proc = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=process_env,
                )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            return ExecutorResult(
                stdout=stdout,
                stderr=stderr,
                return_code=proc.returncode if proc.returncode is not None else -1,
                success=proc.returncode == 0,
                timed_out=False,
            )

        except asyncio.TimeoutError:
            if "proc" in locals():
                proc.kill()
                await proc.wait()

            return ExecutorResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                return_code=-1,
                success=False,
                timed_out=True,
            )

        except FileNotFoundError as e:
            return ExecutorResult(
                stdout="",
                stderr=f"Command not found: {e}",
                return_code=-1,
                success=False,
                timed_out=False,
            )

        except Exception as e:
            log.error("AsyncExecutor error: %s: %s", type(e).__name__, e)
            return ExecutorResult(
                stdout="",
                stderr=f"Execution error: {type(e).__name__}: {e}",
                return_code=-1,
                success=False,
                timed_out=False,
            )


__all__ = ["AsyncExecutor", "ExecutorResult"]
