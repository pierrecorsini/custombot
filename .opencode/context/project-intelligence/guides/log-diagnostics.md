<!-- Context: project-intelligence/guides/log-diagnostics | Priority: medium | Version: 3.1 | Updated: 2026-05-06 -->

# Log Diagnostics

> How to check logs and diagnose issues in the custombot application.

## Log File Location

**Path**: `.workspace/logs/custombot.log`

| Property | Value |
|----------|-------|
| Max Size | 10 MB per file |
| Rotation | Keeps 5 backup files |
| Format | Text (human-readable) or JSON (configurable) |

## Checking Logs

```bash
# View recent logs
tail -100 .workspace/logs/custombot.log

# Search for errors
grep -i "error\|exception\|failed" .workspace/logs/custombot.log

# Watch live logs
tail -f .workspace/logs/custombot.log
```

## Log Entry Contents

Each log entry includes:
- Timestamp and log level
- Module name
- Correlation IDs for request tracing
- Lifecycle events (startup, shutdown, component initialization)
- Connection status changes
- Message processing details
- Error traces with context

## Common Diagnostic Patterns

| Symptom | What to Search | Likely Cause |
|---------|---------------|--------------|
| Bot not responding | `error\|exception` | LLM API or WhatsApp disconnect |
| Messages not sending | `WhatsApp\|neonize` | Session expired, reconnect needed |
| High memory | `memory\|monitoring` | Unbounded cache, check `src/monitoring.py` |
| Slow responses | `latency\|timeout` | LLM API timeout, check `src/rate_limiter.py` |
| Shutdown errors | `cannot schedule\|cannot join` | Executor shutdown order — see `src/lifecycle.py` |
| Startup corruption warnings | `corrupt\|jsonl` | JSONL last line corruption — auto-repair in `workspace_integrity.py` |
| Dependency check failures | `dependency\|not found` | Package name normalization — hyphens vs underscores |
| Embedding errors | `embedding\|encoding_format` | Missing encoding_format param for non-OpenAI providers |
| Config validation errors | `config\|schema\|validation` | Schema fields not synced with runtime config |

## Codebase References

- `src/logging/` — Structured logging with rotation and JSON format option
- `src/monitoring/` — Performance metrics, memory monitoring
- `src/health/` — Health check endpoint
- `src/workspace_integrity.py` — JSONL auto-repair on startup
- `src/lifecycle.py` — Shutdown sequence with executor handling
- `src/diagnose.py` — Diagnostic checks including embedding probe

## Related Files

- `lookup/tech-stack.md` — Stack overview
- `errors/bug-fixes.md` — Past bugs and fixes applied
- `errors/known-issues.md` — Current known issues
