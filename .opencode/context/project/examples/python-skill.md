<!-- Context: project/examples/python-skill | Priority: medium | Version: 1.0 | Updated: 2026-04-04 -->

# Example: Python Skill

**Purpose**: Minimal working example of a Python class skill for custombot.

**Source**: `README.md` — Adding a Python Skill section

---

## Complete Example

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

---

## Key Points

- **File location**: Must be in `skills/user/` directory
- **Class naming**: Any name, but must subclass `BaseSkill`
- **`name` attribute**: Becomes the tool name the LLM sees (must be unique)
- **`parameters`**: Standard OpenAI function-calling JSON schema format
- **`execute()` method**: Must be async, must return a string
- **`workspace_dir`**: Per-chat sandbox directory — use for file I/O
- **`**kwargs`**: Catch-all for additional parameters from the LLM

---

## Auto-Loading

Restart the bot — the skill is auto-discovered by SkillRegistry and exposed to the LLM as a callable tool. No manual registration needed.

---

## Codebase References

- `skills/base.py` — BaseSkill abstract class definition
- `skills/__init__.py` — SkillRegistry auto-discovery logic
- `skills/builtin/` — More complex real-world examples

## Related

- `examples/markdown-skill.md` — Markdown prompt skill alternative
- `guides/skill-development.md` — Full development guide
- `concepts/skills-system.md` — Architecture overview
