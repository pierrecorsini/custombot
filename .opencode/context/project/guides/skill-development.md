<!-- Context: project/guides/skill-development | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Guide: Skill Development

**Purpose**: How to create, test, and register new skills for the custombot system.

**Source**: `README.md` — Adding a Python Skill / Adding a Markdown Prompt Skill

---

## Two Skill Types

| Type | Best For | Location |
|------|----------|----------|
| **Python class** | Complex logic, API calls, async operations | `skills/user/my_skill.py` |
| **Markdown prompt** | Template-driven, LLM-powered tasks | `skills/user/my_skill/skill.md` |

---

## Creating a Python Skill

### Steps

1. Create a `.py` file in `skills/user/`
2. Subclass `BaseSkill` with required attributes
3. Implement `async def execute(self, workspace_dir, **kwargs) -> str`
4. Restart bot — skill is auto-loaded

### Required Attributes

```python
class BaseSkill:
    name: str           # Unique skill identifier
    description: str    # What the skill does (shown to LLM)
    parameters: dict    # OpenAI function-calling JSON schema
```

### Minimal Template

```python
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
        return f"Processed: {input}"
```

---

## Creating a Markdown Prompt Skill

### Steps

1. Create a directory in `skills/user/<skill_name>/`
2. Add `skill.md` with instructions + parameters section
3. Restart bot — skill name = directory name

### Template

```markdown
# Skill Title

Instructions for the LLM to follow.
Return ONLY the result, nothing else.

## Parameters
- input: The text to process
```

---

## Testing

- **Manual**: Send a message in WhatsApp/CLI that triggers the skill
- **CLI mode**: Use `python main.py cli` for quick testing without WhatsApp

---

## Gotchas

- Skill name must be unique across both `builtin/` and `user/`
- `workspace_dir` is the per-chat sandbox — use it for file I/O, not absolute paths
- Skills run async — don't block with `time.sleep()`, use `asyncio.sleep()`
- Return a string from `execute()` — it becomes the tool result for the LLM

---

## Codebase

- `skills/base.py` — BaseSkill abstract class
- `skills/__init__.py` — SkillRegistry auto-discovery
- `skills/prompt_skill.py` — Markdown skill loader
- `skills/builtin/` — Reference implementations

## Related

- `examples/python-skill.md` — Full Python skill example
- `examples/markdown-skill.md` — Full Markdown skill example
- `lookup/built-in-skills.md` — Existing skills for reference
- `concepts/skills-system.md` — Architecture overview
