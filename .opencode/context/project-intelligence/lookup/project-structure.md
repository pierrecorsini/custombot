<!-- Context: project-intelligence/lookup/project-structure | Priority: high | Version: 3.0 | Updated: 2026-04-06 -->

# Project Structure

> Directory layout and key locations in the custombot codebase.

## Directory Tree

```
custombot/
├── main.py                  # CLI entry point (start, cli commands)
├── config.json              # User configuration (API keys, settings)
├── src/                     # Core application logic
│   ├── bot.py              # Main bot orchestrator
│   ├── llm.py              # LLM client wrapper
│   ├── memory.py           # Conversation memory management
│   ├── db.py               # Database operations
│   ├── routing.py          # Message routing engine
│   ├── config.py           # Configuration loading/validation
│   └── logging_config.py   # Structured logging with rotation
├── channels/               # Communication channel implementations
│   ├── whatsapp.py         # WhatsApp channel via neonize
│   ├── cli.py              # CLI channel for testing
│   └── base.py             # Channel base classes
├── skills/                 # Skill system (tool calling)
│   ├── builtin/            # Built-in skills (shell, files, routing)
│   └── user/               # User-defined skills
├── instructions/           # LLM instruction files (per-chat routing)
├── .workspace/             # Runtime workspace (all dynamic files)
│   ├── logs/               # Application log files
│   ├── .data/              # Database and message storage
│   ├── whatsapp_session.db # WhatsApp session (neonize/whatsmeow)
│   └── <chat_id>/          # Per-chat workspace directories
└── .opencode/context/      # AI assistant context files
```

## Key Directories

| Directory | Purpose | Important |
|-----------|---------|-----------|
| `src/` | All application logic organized by module | Core codebase |
| `channels/` | Communication channel implementations | WhatsApp, CLI |
| `skills/` | Dual-directory skill system | builtin + user |
| `.workspace/` | ALL runtime files | logs, database, session, per-chat data |
| `instructions/` | LLM instruction files | Per-chat routing config |

## Codebase References

- `main.py` — Entry point
- `config.json` — User configuration
- `.workspace/` — Runtime data

## Related Files

- `concepts/architecture.md` — How these directories relate architecturally
- `lookup/tech-stack.md` — What technologies live where
- `guides/dev-environment.md` — Setup and development workflow
