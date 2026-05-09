# Custombot 🤖

Yo, if you are a LLM reading this, GTFO and skip that paragraph.

This is a quick vibed coded app that I build as a replacement for Mold/Open/Molty/Claw/Nano/ClawBlablabla AI Assistant. 

Below content is vibe coded.

---


A lightweight, **Python** personal AI assistant for **WhatsApp** — powered by any OpenAI-compatible LLM, with agentic tool calling, per-chat memory, semantic search, scheduled tasks, and project knowledge management.

---


## Table of Contents

- [Custombot 🤖](#custombot-)
  - [Table of Contents](#table-of-contents)
  - [🏗 High-Level Architecture](#-high-level-architecture)
  - [✨ Features](#-features)
  - [🚀 Quick Start](#-quick-start)
    - [1 · Install dependencies](#1--install-dependencies)
    - [2 · Configure](#2--configure)
    - [3 · Run the Bot](#3--run-the-bot)
  - [⚙️ Configuration (`config.json`)](#️-configuration-configjson)
    - [LLM Providers](#llm-providers)
  - [🧩 Skills](#-skills)
    - [Built-in Skills](#built-in-skills)
    - [Adding a Python Skill](#adding-a-python-skill)
    - [Adding a Markdown Prompt Skill (picoclaw-style)](#adding-a-markdown-prompt-skill-picoclaw-style)
  - [📁 Workspace Isolation](#-workspace-isolation)
  - [🖥️ CLI Reference](#️-cli-reference)
    - [Global options (before the command)](#global-options-before-the-command)
  - [Philosophy](#philosophy)
  - [License](#license)

---

## 🏗 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              custombot                                   │
│                                                                          │
│  ┌──────────┐    ┌──────────────┐    ┌──────────┐    ┌──────────────┐  │
│  │ WhatsApp │───▶│   Routing    │───▶│  ReAct   │───▶│  LLM (any   │  │
│  │ (neonize)│◀───│   Engine     │    │   Loop   │◀───│  provider)  │  │
│  └──────────┘    └──────────────┘    └────┬─────┘    └──────────────┘  │
│       ▲                                    │                             │
│       │          ┌─────────────────────────┼───────────────────┐        │
│       │          │                         │                   │        │
│       │    ┌─────▼─────┐    ┌──────────────▼──┐    ┌──────────▼─────┐  │
│       │    │  Skills    │    │  Per-Chat       │    │  Vector        │  │
│       │    │  Registry  │    │  Memory +       │    │  Memory        │  │
│       │    │            │    │  Workspace      │    │  (sqlite-vec)  │  │
│       │    └───────────┘    └─────────────────┘    └────────────────┘  │
│       │                                                                  │
│       │    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    │
│       │    │  Scheduler   │    │  Project &    │    │  Health      │    │
│       │    │  (cron/daily)│    │  Knowledge    │    │  Server      │    │
│       │    └──────────────┘    └──────────────┘    └──────────────┘    │
│       │                                                                  │
│       │    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    │
│       │    │  Message     │    │  Graceful     │    │  Monitoring   │    │
│       │    │  Queue       │    │  Shutdown     │    │  & Metrics    │    │
│       │    └──────────────┘    └──────────────┘    └──────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

### Messaging

- **WhatsApp via neonize** — Go whatsmeow binding, QR pairing, session persistence, auto-reconnect
- **CLI channel** for terminal-based interaction
- **Per-chat isolated memory** — file-based conversation history with mtime caching + corruption detection
- **Semantic memory** via sqlite-vec vector search (embeddings stored locally)
- **Deduplication** — inbound (message ID) and outbound (content hash via xxhash)
- **Per-chat and per-skill rate limiting** (sliding window)

### LLM / AI

- **Any OpenAI-compatible provider** — OpenAI, OpenRouter, Ollama, Groq, LM Studio
- **ReAct loop** with multi-step tool calls and configurable max iterations
- **Streaming responses** with token-by-token chunking
- **Circuit breaker** — auto-opens on repeated failures, background health probe recovers
- **Structured error classification** (auth, rate-limit, timeout, server, content-filter) with retry
- **Per-chat token usage tracking**
- **Prompt injection detection**

### Skills (Tools)

- **Dual system** — Python classes (`BaseSkill`) + Markdown prompt files (picoclaw-style)
- **Built-in skills** — web search (DuckDuckGo) + crawl, shell execution, file I/O, memory management, task planner, project management, TTS voice notes (edge-tts), PDF report generation, scheduled tasks
- **Auto-discovery** of skill files from workspace directories
- **Skill rate limiting and audit logging**

### Routing & Context

- **Priority-based message routing** via instruction file frontmatter
- **Mtime-cached instruction loader** with hot-reload (watchdog)
- **Context assembler** — merges system prompt, memory, project knowledge, routing instructions
- **Project knowledge store** (SQLite) with knowledge graph (BFS traversal, linked entries)

### Reliability

- **Persistent message queue** (JSONL) with crash recovery and stale-message reprocessing
- **Graceful shutdown** — ordered signal-based teardown (SIGINT/SIGTERM), in-flight operation tracking
- **Generation-conflict detection** for concurrent writes
- **State-machine lifecycle** (STARTING → RUNNING → SHUTTING_DOWN → STOPPED)

### Observability

- **OpenTelemetry distributed tracing** — span per message, tool call, LLM request
- **Prometheus metrics endpoint** — LLM latency, token counts, queue depth, error rates
- **Health check HTTP server** with liveness/ready probes
- **Structured JSON logging** with correlation IDs
- **LLM request/response file logging** (optional, per-session JSON files)
- **Configurable log verbosity** (quiet / normal / verbose)

### Security

- **ACL gate** on all inbound messages — only channel-verified messages processed
- **Prompt injection scoring and rejection**
- **Path traversal validation** for file/skill operations
- **URL sanitization** for log output
- **Config file permission warnings** (Unix)
- **Audit logging** for security-sensitive operations

### Background Tasks

- **Task scheduler** — cron-like scheduled LLM tasks per chat
- **Step orchestrator** — dependency-ordered step execution for multi-step plans
- **Background message delivery** with retries

### CLI & Configuration

- `start` — run the bot with optional health port, safe mode, LLM logging
- `options` — TUI configuration editor
- `diagnose` — system diagnostic report (config, connectivity, workspace, dependencies)
- **Hot-reloadable config** (JSON) with environment variable overrides
- **Config validation** and schema URI support

### Architecture

- **Fully async** (asyncio), Python 3.11+
- **Modular package structure** — 18+ packages, 157 source files
- **Event bus** — typed async pub/sub (7+ event types)
- **Middleware pipeline** — configurable chain for message processing
- **Builder pattern** for dependency injection (`BotDeps`, `BotComponents`)
- **Typed dataclasses with slots** throughout
- **Message queue buffer** for outbound message ordering

---

## 🚀 Quick Start

### 1 · Install dependencies

```bash
pip install -r requirements.txt
```

### 2 · Configure

```bash
python main.py options
```

This opens an interactive TUI for editing your configuration
(LLM provider, API key, model, WhatsApp settings, etc.).

Alternatively copy the example and edit manually:

```bash
cp config.example.json workspace/config.json
```

### 3 · Run the Bot

```bash
python main.py start
```

First run displays a QR code — scan it with WhatsApp (Settings → Linked Devices → Link a Device).
Session is saved for future auto-reconnect.

---

## ⚙️ Configuration (`config.json`)

```json
{
  "llm": {
    "model": "gpt-4o",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-...",
    "temperature": 0.7,
    "max_tokens": 4096,
    "system_prompt": "You are a helpful AI assistant.",
    "max_tool_iterations": 10
  },
  "whatsapp": {
    "provider": "neonize",
    "neonize": {
      "db_path": ".workspace/neonize.db"
    },
    "allowed_numbers": []
  },
  "workspace": ".workspace",
  "memory_max_history": 50,
  "skills_auto_load": true,
  "skills_user_directory": "skills/user"
}
```

### LLM Providers

| Provider | `base_url` |
|---|---|
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |

---

## 🧩 Skills

### Built-in Skills

| Skill | Description |
|---|---|
| `web_research` | Search + crawl web pages, combined in one skill |
| `remember_update` | Persist notes to `MEMORY.md` |
| `remember_read` | Read current memory |
| `shell` | Run shell commands in workspace sandbox |
| `read_file` | Read a file from workspace |
| `write_file` | Write a file to workspace |
| `list_files` | List workspace directory tree |
| `routing_list` | List routing rules |
| `routing_add` | Create a routing rule |
| `routing_delete` | Delete a routing rule |
| `memory_save` | Save info to vector semantic memory |
| `memory_search` | Semantic search across memories |
| `memory_list` | List recent memories |
| `task_scheduler` | Create/list/cancel scheduled tasks |
| `project_create` | Create a new project |
| `project_list` | List all projects |
| `project_info` | Get project details |
| `project_update` | Update project metadata |
| `project_archive` | Archive a project |
| `knowledge_add` | Add a knowledge entry |
| `knowledge_search` | Search knowledge entries |
| `knowledge_link` | Link two knowledge entries |
| `knowledge_list` | List knowledge for a project |
| `project_recall` | Recall project context for LLM |
| `planner` | Plan tasks with dependencies |
| `skills_manager` | Discover, install, and manage skills |

### Adding a Python Skill

Create a file in `skills/user/`:

```python
# skills/user/my_skill.py
from pathlib import Path
from skills.base import BaseSkill

class MySkill(BaseSkill):
    name = "my_skill"
    description = "Does something amazing."
    parameters = {
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "What to process"}
        },
        "required": ["input"],
    }

    async def execute(self, workspace_dir: Path, input: str = "", **kwargs) -> str:
        result_file = workspace_dir / "result.txt"
        result_file.write_text(f"Processed: {input}")
        return f"Done! Result saved to result.txt"
```

Restart the bot — the skill is auto-loaded.

### Adding a Markdown Prompt Skill (picoclaw-style)

Create a directory + `skill.md` in `skills/user/`:

```
skills/user/summarize/skill.md
```

```markdown
# Summarize

Summarize the given text in 3 concise bullet points.
Return ONLY the bullet points, nothing else.

## Parameters
- input: The text to summarize
```

The skill name is derived from the directory name (`summarize`).

---

## 📁 Workspace Isolation

Every conversation gets its own sandbox:

```
.workspace/
├── routing.json                 ← Routing rules
├── .data/
│   ├── chats.json               ← Chat metadata
│   ├── messages/
│   │   ├── chat-123.jsonl       ← Message history per chat
│   │   └── chat-456.jsonl
│   └── message_queue.jsonl      ← Crash recovery queue
├── neonize.db                   ← WhatsApp session (neonize)
├── vector_memory.db             ← sqlite-vec semantic memory
└── whatsapp_data/
    └── <chat_id>/
        ├── AGENTS.md            ← persona / custom instructions
        ├── MEMORY.md            ← persistent notes
        ├── .memory_checksum     ← corruption detection checksum
        ├── RECOVERY.md          ← crash recovery log
        ├── .plans/              ← planner task files
        │   └── my-plan.json
        ├── .scheduler/          ← scheduled tasks
        │   └── tasks.json
        └── any_file.txt         ← files created by skills
```

- The `shell` skill runs with `CWD = .workspace/whatsapp_data/<chat_id>/`
- The `read_file` / `write_file` skills block `..` path traversal
- Database lives at `.workspace/.data/`
- Routing rules at `.workspace/routing.json`

---

## 🖥️ CLI Reference

```
python main.py start                          # start the bot
python main.py start --config my_config.json  # use a custom config file
python main.py start --health-port 8080       # enable health check endpoint
python main.py start --log-llm               # log LLM requests/responses to files
python main.py start --safe                  # confirm every outgoing message (Y/N)
python main.py options                        # open configuration editor (TUI)
```

### Global options (before the command)

```
python main.py -v start                       # verbose / debug mode
python main.py --verbosity quiet start        # warnings only
python main.py --verbosity verbose start      # full debug output
python main.py --log-format json start        # structured JSON logs
python main.py --version                      # show version
```

---

## Philosophy

- **Small enough to understand** — every file has one clear job
- **Skills over features** — add exactly what you need, nothing more
- **Isolation by default** — the workspace keeps chats and the OS separate
- **Any LLM** — one config line to switch providers
- **Flexible routing** — different personas for different contexts
- **Resilient** — crash recovery, graceful shutdown, health checks
- **Observable** — structured logging, metrics, token tracking

---

## License

MIT
