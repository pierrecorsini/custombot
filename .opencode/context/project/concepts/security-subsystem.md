<!-- Context: project/concepts/security-subsystem | Priority: high | Version: 1.0 | Updated: 2026-05-02 -->

# Concept: Security Subsystem

**Core Idea**: A defense-in-depth security layer spanning input validation, prompt injection detection, path traversal prevention, audit logging, cryptographic signing, and URL sanitization. Each module is independent but collectively protects the bot from injection, file system attacks, and credential leaks.

**Source**: `src/security/`, `src/channels/base.py`, `src/channels/validation.py`

---

## Key Points

- **Prompt injection filter** (`prompt_injection.py`): Multi-language detection, content filtering for LLM inputs
- **Path validation** (`path_validator.py`): TOCTOU-safe with symlink detection, prevents `..` traversal in file skills
- **Cryptographic signing** (`signing.py`): HMAC-SHA256 for scheduled task prompt integrity
- **Audit log** (`audit.py`): HMAC-SHA256 chained audit log for skill executions
- **URL sanitization** (`url_sanitizer.py`): Redacts credentials/keys from URLs in log output
- **Shell security** (`skills/builtin/shell.py`): Command blocklist (backticks, subshells, chaining)
- **Input validation at boundary** (`channels/base.py`): Regex validation of `chat_id`, `message_id`, `sender_id` at `IncomingMessage.__post_init__()` — rejects path separators, control chars, dots-only traversal

---

## Validation Patterns

| Field | Regex | Real-World Values |
|-------|-------|-------------------|
| `chat_id` | `^[a-zA-Z0-9_\-.@]+$` | `1234567890@s.whatsapp.net`, `120363abc@g.us` |
| `message_id` | `^[a-zA-Z0-9_\-.@]+$` | `3EB0XXXXXX`, UUID format |
| `sender_id` | `^[a-zA-Z0-9_\-.@]+$` | Phone numbers, `cli-abc123` |

---

## Attack Surface

| Threat | Mitigation | Module |
|--------|-----------|--------|
| Path traversal (`..`) | Blocked by path_validator + input regex | `security/path_validator.py` |
| Symlink escape | Detected and rejected | `security/path_validator.py` |
| Prompt injection | Multi-language pattern matching | `security/prompt_injection.py` |
| Scheduled task tampering | HMAC-SHA256 prompt verification | `security/signing.py` |
| Credential leak in logs | URL sanitization + API key redaction | `security/url_sanitizer.py` |
| Command injection | Blocklist (backticks, `&&`, `;`, `$()`) | `skills/builtin/shell.py` |
| Malformed chat_id injection | Regex validation at message boundary | `channels/base.py` |

---

## Codebase

- `src/security/path_validator.py` — TOCTOU-safe path validation
- `src/security/prompt_injection.py` — Injection detection + filtering
- `src/security/signing.py` — HMAC-SHA256 for scheduler prompts
- `src/security/audit.py` — Chained HMAC audit log
- `src/security/url_sanitizer.py` — URL redaction for logging
- `src/channels/base.py` — `IncomingMessage.__post_init__()` boundary validation
- `src/channels/validation.py` — Channel-specific input validation helpers

## Related

- `concepts/architecture-overview.md` — Where security fits in the pipeline
- `lookup/implemented-modules.md` — Module inventory
