"""
src/skills/builtin/shell.py — Run shell commands inside the per-chat workspace.

All commands execute with CWD = workspace_dir (the isolated sandbox for
the current chat), so the agent cannot accidentally touch other chats'
data or system files outside workspace/.

Security measures:
- Command pattern blocking (rm -rf, sudo, etc.) — configurable via ShellConfig
- Configurable allowlist/denylist in config.json
- Path validation to prevent absolute path escapes
- System directory access blocking
- Environment variable protection (blocks reading sensitive vars)
- Audit logging for security events

A 30-second timeout is enforced. stdout + stderr are returned.
"""

from __future__ import annotations

import logging
import os
import platform
import re
from functools import cached_property
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.security.audit import audit_log
from src.security.path_validator import (
    PathSecurityError,
    validate_command_paths,
)
from src.skills.base import BaseSkill, validate_input
from src.utils.async_executor import AsyncExecutor

if TYPE_CHECKING:
    from pathlib import Path
    from src.config.config_schema_defs import ShellConfig

log = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds

# Security: Commands that are blocked for safety
_BLOCKED_PATTERNS = [
    r"\brm\s+(-[rf]+\s+|.*\s+-[rf]+\s*)",  # rm -rf variants
    r"\bsudo\b",
    r"\bchmod\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\binit\s+[06]",
    r"\bfdisk\b",
    r"\bformat\b",
    r"\b:\(\)\{.*;\};",  # Fork bomb
    r"\b>\s*/dev/sd",  # Direct disk write
    r"\b>\s*/dev/hd",  # Direct disk write
    r"\beval\b",  # eval command
    r"\bexec\b",  # exec command
    r"\bexport\b",  # export command
    r"\benv\s+",  # env with arguments
    r"\|\s*(ba)?sh\b",  # pipe to shell
    r"\bnohup\b",  # nohup command
    r"\bdisown\b",  # disown command
    r"\bkill\s+-9\b",  # force kill
    r"\bchmod\s+[0-7]*777\b",  # world-writable permissions
    r"\bchown\b",  # change ownership
    r"\bnc\s+.*-e\b",  # netcat reverse shell
    r"\bpython\b.*-c\b.*\b(import|exec|eval|os|subprocess|sys)\b",  # python one-liners
    # --- Additional blocked patterns for bypass prevention ---
    r"`[^`]*`",  # backtick command substitution
    r"\$\(",  # $() subshell / command substitution
    r"\bsource\b",  # source command (execute arbitrary files)
    r"\bcurl\b.*\|\s*\w+",  # curl pipe to any command
    r"\bwget\b.*\|\s*\w+",  # wget pipe to any command
    r";\s*(rm|sudo|chmod|chown|dd|mkfs|shutdown|reboot|format)\b",  # semicolon chaining of dangerous commands
    r"&&\s*(rm|sudo|chmod|chown|dd|mkfs|shutdown|reboot|format)\b",  # && chaining of dangerous commands
    r"\|\|\s*(rm|sudo|chmod|chown|dd|mkfs|shutdown|reboot|format)\b",  # || chaining of dangerous commands
]

# Security: Sensitive environment variable patterns to block reading
_SENSITIVE_ENV_PATTERNS = [
    r"API[_-]?KEY",
    r"SECRET[_-]?KEY",
    r"ACCESS[_-]?TOKEN",
    r"AUTH[_-]?TOKEN",
    r"BEARER[_-]?TOKEN",
    r"PRIVATE[_-]?KEY",
    r"PASSWORD",
    r"PASSWD",
    r"CREDENTIALS?",
    r"AWS[_-]?ACCESS",
    r"AWS[_-]?SECRET",
    r"OPENAI[_-]?API",
    r"ANTHROPIC[_-]?API",
    r"DATABASE[_-]?URL",
    r"DB[_-]?PASSWORD",
    r"REDIS[_-]?PASSWORD",
    r"SMTP[_-]?PASSWORD",
    r"SLACK[_-]?TOKEN",
    r"GITHUB[_-]?TOKEN",
    r"GITLAB[_-]?TOKEN",
]

# Compile patterns for efficiency
_SENSITIVE_ENV_REGEX = re.compile(r"|".join(_SENSITIVE_ENV_PATTERNS), re.IGNORECASE)


def _audit_log(event: str, details: Dict[str, Any]) -> None:
    """Log security-relevant events for audit purposes."""
    audit_log(event, details, level=logging.WARNING, prefix="SECURITY_AUDIT")


def _is_command_blocked(command: str, extra_denylist: List[str] | None = None) -> Optional[str]:
    """Check if command matches blocked patterns. Returns reason if blocked."""
    cmd_lower = command.lower()
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return f"Command blocked for security (matches pattern: {pattern})"
    # Additional user-configured deny patterns
    if extra_denylist:
        for pattern in extra_denylist:
            try:
                if re.search(pattern, cmd_lower, re.IGNORECASE):
                    return f"Command blocked by custom denylist (matches pattern: {pattern})"
            except re.error as exc:
                log.warning("Invalid denylist regex %r: %s", pattern, exc)
    return None


def _is_command_allowed(command: str, allowlist: List[str] | None = None) -> bool:
    """Check if command matches any allowlist pattern (bypasses denylist)."""
    if not allowlist:
        return False
    for pattern in allowlist:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        except re.error as exc:
            log.warning("Invalid allowlist regex %r: %s", pattern, exc)
    return False


def _is_env_access_blocked(command: str) -> Optional[str]:
    """
    Check if command attempts to read sensitive environment variables.

    Returns reason if blocked, None otherwise.
    """
    cmd_lower = command.lower()

    # Check for common env reading patterns
    env_read_patterns = [
        r"\bprintenv\b",
        r"\benv\b(?!\s*$)",  # env with arguments (but not just `env`)
        r"\becho\s+\$[A-Za-z_]",  # echo $VAR
        r"\bprintf\s+.*\$[A-Za-z_]",  # printf with $VAR
        r"\bset\b(?=.*\$)",  # set | grep $VAR
        r"\$\{[A-Za-z_][A-Za-z0-9_]*\}",  # ${VAR}
        r"\$[A-Za-z_][A-Za-z0-9_]*\b",  # $VAR (not in quotes)
    ]

    for pattern in env_read_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            # Check if any referenced var matches sensitive patterns
            # Extract variable names from command
            var_matches = re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", command)
            for var in var_matches:
                if _SENSITIVE_ENV_REGEX.search(var):
                    _audit_log(
                        "env_access_blocked",
                        {
                            "variable": var,
                            "command_snippet": command[:100],
                        },
                    )
                    return f"Access to sensitive environment variable blocked: {var}"

    return None


def _get_sanitized_env() -> Dict[str, str]:
    """
    Get a sanitized copy of environment variables for command execution.

    Removes sensitive variables that could be leaked.
    Result is cached for the lifetime of the process since os.environ
    rarely changes between command executions.
    """
    if _get_sanitized_env._cache is not None:
        return _get_sanitized_env._cache

    sanitized = dict(os.environ)

    # Remove sensitive variables
    for key in list(sanitized.keys()):
        if _SENSITIVE_ENV_REGEX.search(key):
            del sanitized[key]

    _get_sanitized_env._cache = sanitized
    return sanitized


_get_sanitized_env._cache = None  # type: ignore[attr-defined]


def _get_shell_env_info() -> str:
    """Return OS name for command compatibility."""
    return f"Current OS: {platform.system()}. Use compatible commands."


class ShellSkill(BaseSkill):
    name = "shell"
    dangerous = True
    description = (
        "Execute a shell command inside the isolated workspace directory for "
        "this conversation. Use for file manipulation, running scripts, "
        "installing packages into the workspace, etc. "
        "The current working directory is always the conversation's sandbox. "
        "Note: Absolute paths outside the workspace are not allowed."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (max {_TIMEOUT}, default {_TIMEOUT}).",
                "default": _TIMEOUT,
            },
        },
        "required": ["command"],
    }

    def __init__(self, config: ShellConfig | None = None) -> None:
        self._config = config

    @cached_property
    def tool_definition(self) -> Dict[str, Any]:
        """Return tool definition with dynamic environment info in description."""
        env_info = _get_shell_env_info()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.description} {env_info}",
                "parameters": self.parameters,
            },
        }

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        command: str = "",
        timeout: int = _TIMEOUT,
        **kwargs: Any,
    ) -> str:
        cfg = self._config
        extra_denylist = cfg.command_denylist if cfg else []
        allowlist = cfg.command_allowlist if cfg else []

        # Security check 0: Allowlist bypass (takes precedence over denylist)
        if _is_command_allowed(command, allowlist):
            _audit_log(
                "command_allowed_by_allowlist",
                {"command_snippet": command[:100]},
            )
        else:
            # Security check 1: Block dangerous command patterns
            blocked_reason = _is_command_blocked(command, extra_denylist)
            if blocked_reason:
                _audit_log(
                    "command_blocked",
                    {
                        "command_snippet": command[:100],
                        "reason": blocked_reason,
                    },
                )
                return f"❌ Security: {blocked_reason}"

        # Security check 2: Block access to sensitive environment variables
        env_blocked = _is_env_access_blocked(command)
        if env_blocked:
            _audit_log(
                "env_access_blocked",
                {
                    "command_snippet": command[:100],
                    "reason": env_blocked,
                },
            )
            return f"❌ Security: {env_blocked}"

        # Security check 3: Validate paths in command (prevent absolute path escapes)
        try:
            validate_command_paths(workspace_dir, command)
        except PathSecurityError as exc:
            _audit_log(
                "path_escape_blocked",
                {
                    "path": str(exc.path),
                    "reason": exc.reason,
                    "command_snippet": command[:100],
                },
            )
            return f"❌ Security: {exc}"

        timeout = min(int(timeout), _TIMEOUT)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Use sanitized environment for command execution
        sanitized_env = _get_sanitized_env()

        executor = AsyncExecutor(timeout=float(timeout))
        result = await executor.run(
            command,
            shell=True,
            cwd=str(workspace_dir),
            env=sanitized_env,
        )

        if result.timed_out:
            return f"Error: command timed out after {timeout}s."

        parts = []
        if result.stdout:
            parts.append(result.stdout.strip())
        if result.stderr:
            parts.append("STDERR:\n" + result.stderr.strip())
        if result.return_code != 0:
            parts.append(f"(exit code {result.return_code})")

        return "\n".join(parts) if parts else "(no output)"
