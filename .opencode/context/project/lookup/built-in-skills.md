<!-- Context: project/lookup/built-in-skills | Priority: medium | Version: 1.0 | Updated: 2026-04-04 -->

# Lookup: Built-in Skills Reference

**Purpose**: Complete reference of all built-in skills available to the LLM.

**Source**: `README.md` — Built-in Skills table

---

## Skills by Category

### Memory & Notes

| Skill | Description |
|-------|-------------|
| `remember_update` | Persist notes to `MEMORY.md` |
| `remember_read` | Read current memory contents |
| `memory_save` | Save info to vector semantic memory (sqlite-vec) |
| `memory_search` | Semantic search across vector memories |
| `memory_list` | List recent memories (no embedding needed) |

### Routing

| Skill | Description |
|-------|-------------|
| `routing_list` | List all routing rules |
| `routing_add` | Create a new routing rule |
| `routing_delete` | Delete a routing rule |

### Files & Shell

| Skill | Description |
|-------|-------------|
| `shell` | Run shell commands in workspace sandbox |
| `read_file` | Read a file from workspace |
| `write_file` | Write a file to workspace |
| `list_files` | List workspace directory tree |

### Projects & Knowledge

| Skill | Description |
|-------|-------------|
| `project_create` | Create a new project |
| `project_list` | List all projects |
| `project_info` | Get project details |
| `project_update` | Update project metadata |
| `project_archive` | Archive a project |
| `knowledge_add` | Add a knowledge entry |
| `knowledge_search` | Search knowledge entries |
| `knowledge_link` | Link two knowledge entries |
| `knowledge_list` | List knowledge for a project |
| `project_recall` | Recall project context for LLM injection |

### Task Management

| Skill | Description |
|-------|-------------|
| `task_scheduler` | Create/list/cancel scheduled tasks (daily/interval/cron) |
| `planner` | Plan tasks with dependency tracking and execution ordering |

### Web & Research

| Skill | Description |
|-------|-------------|
| `web_research` | Search + crawl web pages, combined in one skill |

### System

| Skill | Description |
|-------|-------------|
| `skills_manager` | Discover, install, and manage skills |

---

## Rate-Limited Skills

These skills have separate per-skill limits (10 calls / 60s):
- `web_search`, `web_research`, `shell`, `memory_save`

---

## Codebase

- `skills/builtin/` — All built-in skill implementations
- `skills/builtin/web_research.py` — Web search + crawl
- `skills/builtin/memory_vss.py` — Vector semantic memory skills
- `skills/builtin/task_scheduler.py` — Scheduled task CRUD
- `skills/builtin/project_skills.py` — Project & knowledge (10 tools)
- `skills/builtin/planner.py` — Task planning with dependencies
- `skills/builtin/shell.py` — Shell command execution
- `skills/builtin/files.py` — File read/write/list
- `skills/builtin/routing.py` — Routing CRUD

## Related

- `concepts/skills-system.md` — Architecture overview
- `guides/skill-development.md` — How to create new skills
