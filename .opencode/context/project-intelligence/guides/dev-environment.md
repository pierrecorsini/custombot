<!-- Context: project-intelligence/guides/dev-environment | Priority: medium | Version: 3.0 | Updated: 2026-04-06 -->

# Development Environment

> How to set up, run, and develop the custombot project locally.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py start

# Run tests
pytest tests/

# Run CLI channel (no WhatsApp needed)
python main.py cli
```

## Requirements

- **Python**: 3.13+
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
CI/CD: Not configured
Monitoring: Log files + health endpoint (optional)
```

## WhatsApp Shutdown

Correct shutdown: **Ctrl+C** in the terminal running `python main.py start`
- The bot handles graceful shutdown automatically
- Disconnects WhatsApp session cleanly
- Closes database connections

## Codebase References

- `main.py` — CLI entry point (`start`, `cli` commands)
- `requirements.txt` — Python dependencies
- `src/setup_wizard.py` — Quick-start guide

## Related Files

- `lookup/tech-stack.md` — Full technology stack
- `lookup/project-structure.md` — Directory layout
- `guides/log-diagnostics.md` — How to diagnose issues via logs
