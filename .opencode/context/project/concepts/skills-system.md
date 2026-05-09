<!-- Context: project/concepts/skills-system | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Concept: Skills System Architecture

**Core Idea**: Custombot uses a dual skill system — Python class skills (full logic, async execution) and Markdown prompt skills (picoclaw-style, template-driven). Both are auto-discovered by the SkillRegistry and exposed as LLM tool definitions for the ReAct loop.

**Source**: `README.md` — Skills section

---

## Key Points

- **Two skill types**: Python classes (`BaseSkill` subclass) for complex logic, Markdown files (`skill.md`) for prompt templates
- **Auto-discovery**: SkillRegistry scans `skills/builtin/` and `skills/user/` on startup
- **Tool definitions**: Each skill generates an OpenAI function-calling tool schema from its `parameters` dict
- **Rate limiting**: Expensive skills (web_search, shell, memory_save) have separate per-skill limits (10/60s)
- **Workspace sandboxed**: Skills execute within the per-chat workspace directory

---

## Architecture

```
SkillRegistry (auto-discovery on startup)
    │
    ├── skills/builtin/     ← shipped with bot
    │   ├── web_research.py
    │   ├── memory_vss.py
    │   ├── task_scheduler.py
    │   ├── project_skills.py
    │   ├── planner.py
    │   ├── shell.py
    │   └── files.py / routing.py / skills_manager.py
    │
    └── skills/user/        ← user-created
        ├── my_skill.py          ← Python class skill
        └── summarize/skill.md   ← Markdown prompt skill
```

---

## Skill Registration Flow

```
1. Scan skills/builtin/ and skills/user/
2. For each .py file: import → find BaseSkill subclass → register
3. For each */skill.md: parse frontmatter → register as PromptSkill
4. Generate OpenAI tool definitions from parameters schema
5. LLM sees tools in API call → can invoke any registered skill
```

---

## Execution Flow

```
LLM response contains tool_calls
       │
       ▼
tool_executor.py
  ├─ Resolve skill name → skill instance
  ├─ Check per-skill rate limit (expensive skills)
  ├─ Execute skill.execute(workspace_dir, **args)
  ├─ Track metrics (latency, success/failure)
  └─ Return result string → append to messages → ReAct loop continues
```

---

## Codebase

- `skills/base.py` — `BaseSkill` abstract class (name, description, parameters, execute)
- `skills/__init__.py` — `SkillRegistry` (auto-discovery, tool definition generation)
- `skills/prompt_skill.py` — Markdown prompt skill loader (picoclaw-style)
- `src/core/tool_executor.py` — Execution with rate limiting + metrics
- `skills/builtin/` — 28+ built-in skills

## Related

- `examples/python-skill.md` — How to create a Python skill
- `examples/markdown-skill.md` — How to create a Markdown skill
- `guides/skill-development.md` — Full skill development guide
- `lookup/built-in-skills.md` — Complete skill reference table
