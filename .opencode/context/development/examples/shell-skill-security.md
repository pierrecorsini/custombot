<!-- Context: development/examples | Priority: critical | Version: 1.0 | Updated: 2026-03-27 -->

# Example: Shell Skill Security

**Purpose**: Protect sensitive environment variables from shell command exposure

---

## Problem

AI agents with shell access could read sensitive environment variables:
```bash
echo $API_KEY      # Exposes API key
env | grep SECRET  # Lists all secrets
printenv           # Dumps entire environment
```

---

## Solution

```python
import os
import logging
from typing import Optional

logger = logging.getLogger('security')

# Patterns to block from environment access
BLOCKED_ENV_PATTERNS = {
    'API_KEY', 'SECRET', 'PASSWORD', 'TOKEN', 
    'CREDENTIAL', 'PRIVATE', 'AUTH'
}

# Commands that might leak environment
SUSPICIOUS_COMMANDS = {'env', 'printenv', 'set', 'export'}


def sanitize_environment(env: dict) -> dict:
    """Remove sensitive variables from environment dict."""
    sanitized = {}
    blocked = []
    
    for key, value in env.items():
        key_upper = key.upper()
        if any(pattern in key_upper for pattern in BLOCKED_ENV_PATTERNS):
            blocked.append(key)
        else:
            sanitized[key] = value
    
    if blocked:
        logger.info(f"Blocked {len(blocked)} sensitive env vars: {blocked}")
    
    return sanitized


def validate_command(cmd: str) -> tuple[bool, Optional[str]]:
    """Check command for suspicious patterns."""
    cmd_lower = cmd.lower()
    
    for suspicious in SUSPICIOUS_COMMANDS:
        if suspicious in cmd_lower:
            return False, f"Blocked command: {suspicious}"
    
    # Check for env var access patterns
    if '$' in cmd and any(p in cmd.upper() for p in BLOCKED_ENV_PATTERNS):
        return False, "Blocked: attempting to read sensitive env var"
    
    return True, None


async def execute_shell_safe(cmd: str, user_id: str) -> str:
    """Execute shell command with security controls."""
    
    # Validate command
    allowed, reason = validate_command(cmd)
    if not allowed:
        logger.warning(f"Shell blocked for {user_id}: {reason}")
        return f"Error: {reason}"
    
    # Audit log
    logger.info(f"Shell exec by {user_id}: {cmd[:100]}")
    
    # Execute with sanitized environment
    safe_env = sanitize_environment(os.environ)
    result = await run_command(cmd, env=safe_env)
    
    return result
```

---

## Security Layers

| Layer | Protection |
|-------|------------|
| Pattern blocking | Can't read `*SECRET*`, `*API_KEY*`, etc. |
| Command filtering | Block `env`, `printenv`, `set` |
| Audit logging | All attempts logged |
| Sanitized env | Run with clean environment |

---

## Testing

```python
def test_env_protection():
    env = {'API_KEY': 'secret123', 'PATH': '/usr/bin'}
    safe = sanitize_environment(env)
    assert 'API_KEY' not in safe
    assert 'PATH' in safe
```

---

**Source**: `src/skills/builtin/shell.py`  
**Reference**: Harvested from session 2026-03-26-code-optimization
