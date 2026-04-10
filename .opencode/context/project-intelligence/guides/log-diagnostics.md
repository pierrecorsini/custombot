<!-- Context: project-intelligence/guides/log-diagnostics | Priority: medium | Version: 3.0 | Updated: 2026-04-06 -->

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

## Codebase References

- `src/logging_config.py` — Structured logging with rotation and JSON format option
- `src/monitoring.py` — Performance metrics, memory monitoring
- `src/health.py` — Health check endpoint

## Related Files

- `lookup/tech-stack.md` — Stack overview
- `errors/bug-fixes.md` — Past bugs and fixes applied
- `errors/known-issues.md` — Current known issues
