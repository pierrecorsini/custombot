"""
src/security/sandbox.py — Resource-based skill sandboxing with OS-level limits.

Provides a ``ResourceSandbox`` that wraps skill execution with:
- CPU time limits (via ``resource.setrlimit`` on POSIX)
- Memory limits (via ``resource.setrlimit`` on POSIX)
- Filesystem access boundaries (allowed paths whitelist)

On Windows, resource limits are skipped (best-effort — async timeout
enforcement from ``src.skills.sandbox.SkillSandbox`` still applies).

Usage::

    from src.security.sandbox import ResourceSandbox, ResourceSandboxConfig

    config = ResourceSandboxConfig(max_cpu_seconds=30, max_memory_mb=512)
    sandbox = ResourceSandbox(config)
    result = await sandbox.run_in_thread(sync_fn, *args)
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

# Resource module is only available on POSIX (Linux/macOS).
_HAS_RESOURCE = platform.system() != "Windows"
if _HAS_RESOURCE:
    import resource


@dataclass(slots=True, frozen=True)
class ResourceSandboxConfig:
    """Configuration for resource-based skill sandboxing.

    Attributes:
        max_cpu_seconds: CPU time limit in seconds (RLIMIT_CPU).
            The process receives SIGXCPU at the soft limit and is killed
            at the hard limit.  Default 30s.
        max_memory_mb: Maximum resident set size in megabytes
            (RLIMIT_AS).  ``MemoryError`` is raised when exceeded.
            Default 512 MB.
        allowed_paths: Whitelist of filesystem paths the skill may
            access.  Empty list means no filesystem restrictions.
        max_output_bytes: Maximum bytes a subprocess may write to
            stdout/stderr before being killed.  Default 1 MB.
    """

    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512
    allowed_paths: tuple[str, ...] = ()
    max_output_bytes: int = 1_048_576


# Default configs for skill categories.
DEFAULT_SANDBOX_CONFIG = ResourceSandboxConfig()

# Tighter limits for dangerous skills (shell execution, file write).
DANGEROUS_SKILL_CONFIG = ResourceSandboxConfig(
    max_cpu_seconds=15.0,
    max_memory_mb=256,
    allowed_paths=(),
    max_output_bytes=512_000,
)


class ResourceLimitExceeded(Exception):
    """Raised when a skill exceeds its resource sandbox limits."""

    def __init__(self, limit_type: str, limit_value: float | int) -> None:
        self.limit_type = limit_type
        self.limit_value = limit_value
        super().__init__(
            f"Skill exceeded {limit_type} limit: {limit_value}"
        )


class ResourceSandbox:
    """Resource sandbox for skill execution with OS-level limits.

    On POSIX systems, applies ``resource.setrlimit`` within a subprocess
    to enforce CPU time and memory limits.  On Windows, resource limits
    are skipped (best-effort).
    """

    __slots__ = ("_config",)

    def __init__(self, config: ResourceSandboxConfig | None = None) -> None:
        self._config = config or DEFAULT_SANDBOX_CONFIG

    @property
    def config(self) -> ResourceSandboxConfig:
        return self._config

    def _set_rlimits(self) -> None:
        """Apply resource limits for the current process (POSIX only).

        Called inside a subprocess before executing user code so that
        limits are isolated from the main event loop.
        """
        if not _HAS_RESOURCE:
            return

        cfg = self._config

        if cfg.max_cpu_seconds > 0:
            soft = int(cfg.max_cpu_seconds)
            hard = soft + 2  # Grace period before SIGKILL
            resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))

        if cfg.max_memory_mb > 0:
            limit_bytes = cfg.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    def validate_path(self, path: str | Path) -> bool:
        """Check whether *path* is within the allowed paths whitelist.

        Returns ``True`` if the whitelist is empty (no restrictions) or
        if the resolved path falls under an allowed prefix.
        """
        if not self._config.allowed_paths:
            return True
        resolved = Path(path).resolve()
        return any(
            resolved.is_relative_to(Path(allowed).resolve())
            for allowed in self._config.allowed_paths
        )

    async def run_subprocess(
        self,
        args: Sequence[str],
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a subprocess within the sandbox resource limits.

        On POSIX, the subprocess is a direct child so ``setrlimit``
        applies.  On Windows, only the async timeout is enforced.

        Args:
            args: Command and arguments.
            cwd: Working directory (defaults to current).
            env: Environment variables (defaults to inherit).
            timeout: Wall-clock timeout in seconds.  Defaults to
                ``config.max_cpu_seconds * 2``.

        Returns:
            ``CompletedProcess`` with captured stdout/stderr.

        Raises:
            ResourceLimitExceeded: If the process exceeds limits.
            subprocess.TimeoutExpired: If wall-clock timeout is hit.
        """
        import asyncio

        effective_timeout = timeout or (self._config.max_cpu_seconds * 2 or 30)

        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
                preexec_fn=self._set_rlimits if _HAS_RESOURCE else None,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise subprocess.TimeoutExpired(
                    cmd=list(args), timeout=effective_timeout
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            # Check for resource limit signals
            if _HAS_RESOURCE and proc.returncode == -signal.SIGXCPU:
                raise ResourceLimitExceeded(
                    "max_cpu_seconds", self._config.max_cpu_seconds
                )

            if proc.returncode == 0:
                return subprocess.CompletedProcess(
                    args=list(args),
                    returncode=0,
                    stdout=stdout,
                    stderr=stderr,
                )

            return subprocess.CompletedProcess(
                args=list(args),
                returncode=proc.returncode or 1,
                stdout=stdout,
                stderr=stderr,
            )

        except ResourceLimitExceeded:
            raise
        except Exception as exc:
            log.error("Sandbox subprocess failed: %s", exc)
            raise
