<!-- Context: project-intelligence/guides/dev-environment | Priority: medium | Version: 3.1 | Updated: 2026-05-06 -->

# Development Environment

> How to set up, run, and develop the custombot project locally.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py start

# Run diagnostics
python main.py diagnose

# Edit configuration (TUI)
python main.py options

# Run tests
pytest tests/
```

## Requirements

- **Python**: 3.11+ (see `pyproject.toml` requires-python)
- **Platform**: Any (Linux, macOS, Windows)

## Onboarding Checklist

- [ ] Know the primary tech stack (Python + neonize/whatsmeow)
- [ ] Understand the native Python architecture (ctypes bindings, no bridge)
- [ ] Know the key project directories and their purpose
- [ ] Understand that ALL runtime files live in `.workspace/`
- [ ] Know where log files are located (`.workspace/logs/`)
- [ ] Be able to set up local development environment
- [ ] Know how to run tests and check logs for issues

## Deployment

```
Environment: Local development / Single server
Platform: Any (Linux, macOS, Windows)
CI/CD: GitHub Actions (ruff, mypy, pytest, pip-audit, coverage gate)
Monitoring: Log files + health endpoint + OpenTelemetry (optional)
```

## WhatsApp Shutdown

Correct shutdown: **Ctrl+C** in the terminal running `python main.py start`
- The bot handles graceful shutdown automatically
- Disconnects WhatsApp session cleanly
- Closes database connections

## Codebase References

- `main.py` — CLI entry point (`start`, `diagnose`, `options` commands)
- `pyproject.toml` — Project config, deps, ruff, mypy, pytest
- `requirements.txt` — Python dependencies
- `.github/workflows/ci.yml` — CI pipeline

## Related Files

- `lookup/tech-stack.md` — Full technology stack
- `lookup/project-structure.md` — Directory layout
- `guides/log-diagnostics.md` — How to diagnose issues via logs
