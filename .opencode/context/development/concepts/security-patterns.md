<!-- Context: development/concepts | Priority: critical | Version: 1.0 | Updated: 2026-03-27 -->

# Concept: Security Patterns for AI Agents

**Purpose**: Protect sensitive data in AI agent systems

---

## Core Idea

Defense in depth: audit logging, input validation, and controlled execution environments to prevent data leaks and unauthorized operations.

---

## Key Points

- Audit log all security-sensitive operations
- Block reading sensitive env vars (`API_KEY`, `SECRET`, `PASSWORD`, `TOKEN`)
- Sanitize user inputs before file/shell operations
- Enforce file size limits on writes (1MB max recommended)
- Use sanitized environment for command execution

---

## Quick Example

```python
import logging
import os

BLOCKED_PATTERNS = {'API_KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'CREDENTIAL'}

logger = logging.getLogger('security')

def sanitize_environment(env: dict) -> dict:
    """Remove sensitive variables from environment."""
    return {
        k: v for k, v in env.items()
        if not any(p in k.upper() for p in BLOCKED_PATTERNS)
    }

def audit_log(operation: str, details: dict):
    """Log security-sensitive operations."""
    logger.info(f"SECURITY: {operation} | {details}")

# Usage
safe_env = sanitize_environment(os.environ)
audit_log("shell_exec", {"cmd": cmd, "user": user_id})
```

---

## Security Checklist

- [ ] Sensitive env vars blocked
- [ ] File operations have size limits
- [ ] Shell commands use sanitized env
- [ ] All security events logged
- [ ] User inputs validated before use

---

## Related

- examples/shell-skill-security.md
- examples/path-sanitization.md

**Source**: Harvested from session 2026-03-26-code-optimization
