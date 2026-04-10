<!-- Context: development/guides/shell-security | Priority: high | Version: 1.0 | Updated: 2026-03-21 -->

# Guide: Shell Command Security

**Purpose**: Secure shell skill with command blocklist to prevent dangerous operations

**Source**: Harvested from `.tmp/sessions/2025-03-21-code-optimization/context.md`

---

## Core Concept

Restrict shell skill execution by blocking dangerous commands that could cause data loss, system compromise, or privilege escalation. A blocklist pattern prevents accidental or malicious execution of destructive operations.

---

## Dangerous Commands to Block

| Category | Commands | Risk |
|----------|----------|------|
| **Destructive** | `rm -rf /`, `rm -rf *`, `mkfs`, `dd if=/dev/zero` | Data loss |
| **Privilege** | `sudo`, `su`, `chmod 777`, `chown root` | Privilege escalation |
| **Execution** | `eval`, `exec`, `source ~/.bashrc` | Arbitrary code execution |
| **Network** | `curl | bash`, `wget | sh` | Remote code execution |
| **System** | `shutdown`, `reboot`, `halt`, `init 0` | System disruption |

---

## Implementation Pattern

```python
# skills/builtin/shell.py
BLOCKED_PATTERNS = [
    r'\brm\s+-rf\s+/',      # rm -rf /
    r'\bsudo\b',            # sudo
    r'\beval\b',            # eval
    r'\bcurl.*\|\s*bash',   # curl | bash
    r'\bshutdown\b',        # shutdown
]

def validate_command(cmd: str) -> bool:
    """Return False if command matches blocked pattern."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return False
    return True
```

---

## Security Checklist

- [ ] Block destructive file operations (`rm -rf /`)
- [ ] Block privilege escalation (`sudo`, `su`)
- [ ] Block arbitrary code execution (`eval`, `exec`)
- [ ] Block remote script execution (`curl | bash`)
- [ ] Log all blocked attempts for audit
- [ ] Provide clear error messages to user

---

## Codebase Reference

- `skills/builtin/shell.py` - Shell skill implementation
- `src/bot.py` - Tool execution orchestrator

---

## Related

- `../core/standards/security-patterns.md` - General security patterns
- `../principles/clean-code.md` - Code quality standards
