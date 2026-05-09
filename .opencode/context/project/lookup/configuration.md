<!-- Context: project/lookup/configuration | Priority: medium | Version: 2.0 | Updated: 2026-05-02 -->

# Lookup: Configuration Schema

**Purpose**: Complete `config.json` schema and supported LLM providers.

**Source**: `config.example.json`, `src/config/config.py`, `src/config/config_schema_defs.py`

---

## Schema

```json
{
  "llm": {
    "model": "gpt-4o",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-...",
    "temperature": 0.7,
    "max_tokens": 4096,
    "system_prompt_prefix": "",
    "max_tool_iterations": 10,
    "stream_response": false,
    "embedding_model": "text-embedding-3-small",
    "embedding_dimensions": 1536,
    "embedding_base_url": null,
    "embedding_api_key": null
  },
  "whatsapp": {
    "provider": "neonize",
    "neonize": { "db_path": "workspace/whatsapp_session.db" },
    "allowed_numbers": [],
    "allow_all": false
  },
  "workspace": ".workspace",
  "memory_max_history": 50,
  "load_history": false,
  "skills_auto_load": true,
  "skills_user_directory": "workspace/skills",
  "log_incoming_messages": true,
  "log_routing_info": false,
  "shutdown_timeout": 30.0,
  "log_format": "text",
  "log_file": "workspace/logs/custombot.log",
  "log_max_bytes": 10485760,
  "log_backup_count": 5,
  "max_thread_pool_workers": null,
  "shell": { "denylist": [], "allowlist": [] },
  "middleware": {
    "middleware_order": [],
    "extra_middleware_paths": []
  },
  "max_chat_lock_cache_size": 1000,
  "max_chat_lock_eviction_policy": "lru"
}
```

---

## Environment Variable Overrides

| Variable | Overrides | Priority |
|----------|----------|----------|
| `OPENAI_API_KEY` | `llm.api_key` | Takes precedence over config.json |
| `OPENAI_BASE_URL` | `llm.base_url` | Takes precedence over config.json |

---

## Field Reference

### `llm` section

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | `"gpt-4o"` | Model identifier for the LLM API |
| `base_url` | string | OpenAI URL | API endpoint (any OpenAI-compatible) |
| `api_key` | string | required | API authentication key |
| `temperature` | float | `0.7` | Response randomness (0.0–2.0) |
| `max_tokens` | int | `4096` | Max response length |
| `system_prompt_prefix` | string | `""` | Prefix prepended to routing system prompt |
| `max_tool_iterations` | int | `10` | Max ReAct loop iterations |
| `stream_response` | bool | `false` | Use streaming LLM responses |
| `embedding_model` | string | `"text-embedding-3-small"` | Model for vector memory embeddings |
| `embedding_dimensions` | int | `1536` | Embedding vector dimensions |
| `embedding_base_url` | string? | `null` | Dedicated embedding API endpoint |
| `embedding_api_key` | string? | `null` | Dedicated embedding API key |

### `whatsapp` section

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string | `"neonize"` | Channel backend |
| `neonize.db_path` | string | `workspace/whatsapp_session.db` | Session DB path |
| `allowed_numbers` | array | `[]` | Whitelist of phone numbers |
| `allow_all` | bool | `false` | Allow all senders (default-deny) |

### General

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace` | string | `".workspace"` | Root workspace directory |
| `memory_max_history` | int | `50` | Max messages in chat history |
| `load_history` | bool | `false` | Load history on startup |
| `skills_auto_load` | bool | `true` | Auto-discover user skills |
| `skills_user_directory` | string | `"workspace/skills"` | User skill directory |
| `shutdown_timeout` | float | `30.0` | Graceful shutdown timeout (seconds) |
| `log_format` | string | `"text"` | Log format: text or json |
| `log_file` | string? | `"workspace/logs/..."` | Log file path |
| `log_verbosity` | string | `"normal"` | quiet / normal / verbose |
| `log_llm` | bool | `false` | Log LLM req/res to JSON files |
| `shell` | object | `{}` | Shell skill: denylist/allowlist |
| `middleware` | object | `{}` | Pipeline: order + custom paths |
| `max_chat_lock_cache_size` | int | `1000` | LRU lock cache size |
| `max_chat_lock_eviction_policy` | string | `"lru"` | Lock eviction: lru / safety |

---

## Supported LLM Providers

| Provider | `base_url` |
|----------|-----------|
| OpenAI | `https://api.openai.com/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |

---

## Editing Configuration

1. **TUI**: `python main.py options` — interactive editor
2. **Manual**: `cp config.example.json workspace/config.json` — edit directly
3. **Hot reload**: `ConfigWatcher` auto-applies changes when config.json mtime changes

---

## Codebase

- `src/config/config_schema_defs.py` — Pure dataclass definitions (LLMConfig, WhatsAppConfig, Config)
- `src/config/config_loader.py` — JSON load/save, dict→dataclass construction, env overrides
- `src/config/config_validation.py` — Validation helpers, deprecated/renamed option tracking
- `src/config/config_schema_defs.py` — Dataclass definitions + JSON Schema validation
- `src/config/config_watcher.py` — Hot-reload file watcher (mtime polling, debounced)
- `src/config/config.py` — Facade re-exporting from split modules
- `config.example.json` — Example configuration template

## Related

- `guides/cli-reference.md` — CLI commands
- `lookup/workspace-structure.md` — Workspace layout
