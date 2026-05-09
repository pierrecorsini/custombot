<!-- Context: project/examples/markdown-skill | Priority: medium | Version: 1.0 | Updated: 2026-04-04 -->

# Example: Markdown Prompt Skill

**Purpose**: Minimal working example of a Markdown prompt skill (picoclaw-style) for custombot.

**Source**: `README.md` — Adding a Markdown Prompt Skill section

---

## Complete Example

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

---

## Key Points

- **File location**: Must be `skills/user/<skill_name>/skill.md`
- **Skill name**: Derived from the directory name (`summarize/` → skill name `summarize`)
- **Content**: The markdown body becomes the prompt template sent to the LLM
- **Parameters section**: Declared under `## Parameters` heading — parsed automatically
- **No Python needed**: Pure markdown — the LLM processes the prompt and parameters

---

## Directory Structure

```
skills/user/
└── summarize/          ← skill name = directory name
    └── skill.md        ← prompt template + parameters
```

---

## Auto-Loading

Restart the bot — the SkillRegistry scans `skills/user/` for directories containing `skill.md` and registers them as `PromptSkill` instances.

---

## When to Use vs Python Skills

| Use Markdown Skill | Use Python Skill |
|-------------------|-----------------|
| Template-driven LLM tasks | Complex logic / async operations |
| Text transformation / formatting | API calls / database access |
| Quick prompt wrappers | File I/O / system operations |

---

## Codebase References

- `skills/prompt_skill.py` — Markdown skill loader and parser
- `skills/__init__.py` — SkillRegistry discovery for prompt skills

## Related

- `examples/python-skill.md` — Python class skill alternative
- `guides/skill-development.md` — Full development guide
- `concepts/skills-system.md` — Architecture overview
