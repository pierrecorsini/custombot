<!-- Context: project-intelligence/technical | Priority: critical | Version: 1.0 | Updated: 2026-05-02 -->

# Technical Domain

**Purpose**: Tech stack, architecture, and development patterns for custombot — a lightweight WhatsApp AI assistant.
**Last Updated**: 2026-05-02

## Quick Reference
**Update Triggers**: Tech stack changes | New patterns | Architecture decisions
**Audience**: Developers, AI agents

---

## Primary Stack

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Language | Python | >=3.11 | Async/await, dataclasses, slots, type hints |
| Runtime | asyncio | stdlib | Event-driven message processing |
| LLM Client | openai | ~=2.29 | Supports OpenAI, Anthropic proxy, Ollama, OpenRouter, Groq |
| Channel | neonize | 0.3.17 | Native WhatsApp Web client (QR pairing, session persistence) |
| CLI | Click | ~=8.3 | Command groups, options, help text |
| Display | Rich | ~=14.3 | Terminal formatting, spinners, progress bars |
| Database | SQLite | stdlib | 3 databases: main (.data/), vector_memory, projects |
| Vector Search | sqlite-vec | 0.1.9 | Semantic memory with cosine similarity |
| Serialization | orjson + msgpack | latest | Fast JSON + binary encoding |
| Hashing | xxhash | ~=3.5 | Deduplication hash computation |
| Logging | stdlib + Rich + OTel | 1.30 | Structured logs, correlation IDs, OpenTelemetry spans |
| Config | JSON + dataclasses | — | Hot-reload via watchdog file watcher |
| Linting | Ruff | >=0.15 | Combined linter + formatter (replaces flake8, black, isort) |
| Typing | mypy | >=1.20 | Gradual strict mode (`disallow_untyped_defs = false`) |
| Testing | pytest + hypothesis | >=9.0 | Unit + property-based + benchmarks |

---

## Code Patterns

### Async LLM Client
```python
# OpenAI-compatible provider with circuit breaker + retry
from src.llm_provider import LLMProvider

provider = LLMProvider(config.llm)
response: str = await provider.chat(messages=[...], tools=[...])
# Supports streaming, tool calls, warmup probes, health checks
```

### Dataclass Containers
```python
from dataclasses import dataclass

@dataclass(frozen=True)      # Immutable containers (config, results)
class BotComponents:
    bot: Bot
    db: Database
    llm: LLMProvider

@dataclass(slots=True)       # Mutable state bags (builder context)
class BuilderContext:
    db: Optional[Database] = None
    memory: Optional[Memory] = None
```

### Module Structure (every file follows this)
```python
"""module.py — One-line purpose.

Longer description of what this module does and why.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config  # Type-only imports

log = logging.getLogger(__name__)

# ... implementation ...

__all__ = ["PublicClass", "public_function"]
```

### Exception Hierarchy
```python
from src.exceptions import LLMError, ConfigurationError

# Domain-specific with error codes, suggestions, docs links
raise LLMError("API timeout", provider="openai", model="gpt-4")
# .to_user_message() → formatted with emoji + ref code + docs link
```

---

## Naming Conventions

| Type | Convention | Example |
|------|-----------|---------|
| Files | snake_case | `message_queue.py`, `config_watcher.py` |
| Classes | PascalCase | `BotComponents`, `AppPhase`, `IncomingMessage` |
| Functions | snake_case | `build_bot()`, `perform_shutdown()` |
| Constants | UPPER_SNAKE_CASE | `WORKSPACE_DIR`, `MEMORY_FILENAME` |
| Private | Leading underscore | `_run_bot()`, `_setup_logging()` |
| Directories | snake_case | `vector_memory/`, `message_queue.py` |
| Tests | test_ prefix | `test_routing.py`, `test_config.py` |

---

## Code Standards

- `from __future__ import annotations` on line 1 of every file (after docstring)
- `log = logging.getLogger(__name__)` — module-level logger in every module
- `TYPE_CHECKING` guard for type-only imports (avoid circular deps)
- `__all__` exports in all public modules
- Frozen dataclasses for immutable data; `slots=True` for mutable state
- Protocol classes for dependency injection boundaries
- Step orchestrator pattern for multi-phase startup/build (declarative `ComponentSpec`)
- Double quotes for strings (ruff format), line length 100
- Docstrings: triple-double-quoted, Google-style Args/Returns

---

## Security Requirements

- Path validation: all file access sandboxed to workspace directory (`is_path_in_workspace`)
- URL sanitization: strip credentials from logged URLs (`sanitize_url_for_logging`)
- Prompt injection detection: classify and reject adversarial inputs
- Secret redaction: `Config.__repr__()` uses `_redact_secrets()` to mask API keys
- Config file permission check: warn if config.json readable by group/others (Unix)
- Input validation: `IncomingMessage` fields validated before use
- Defense-in-depth: 6-module security subsystem (`src/security/`)

---

## 📂 Codebase References

**Entry Point**: `main.py` — Click CLI with `start`, `options`, `diagnose` commands
**App Lifecycle**: `src/app.py` — `Application` class with `AppPhase` state machine
**Component Builder**: `src/builder.py` — `build_bot()` returns `BotComponents`
**Bot Core**: `src/bot/_bot.py` — `Bot.handle_message()` (ReAct loop, routing, delivery)
**Config**: `src/config/` — Schema defs, loader, validation, hot-reload watcher
**LLM**: `src/llm.py`, `src/llm_provider.py` — Async client with circuit breaker
**Memory**: `src/memory.py` — Per-chat `MEMORY.md` files
**Routing**: `src/routing.py` — YAML frontmatter rules in `.md` instruction files
**Skills**: `src/skills/` — `BaseSkill` (Python) + prompt-based skills (Markdown)
**Security**: `src/security/` — Path validator, prompt injection, URL sanitizer, audit, signing

**Config**: `pyproject.toml` (dependencies, ruff, mypy, pytest), `requirements.txt` (pip-compile)

## Related Files
- Project Context: `.opencode/context/project/navigation.md`
- Improvement Plan: `PLAN.md`
- Architecture Concepts: `.opencode/context/project/concepts/architecture-overview.md`
