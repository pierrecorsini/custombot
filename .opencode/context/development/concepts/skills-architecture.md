<!-- Context: development/concepts/skills-architecture | Priority: medium | Version: 1.0 | Updated: 2026-03-21 -->

# Concept: Skills Architecture

**Purpose**: Dual-directory skill system separating project-maintained from user-installed skills

**Source**: Harvested from `.tmp/sessions/2026-03-21-skills-refactor/living-notes.md`

---

## Core Concept

Skills are organized into two directories with distinct ownership: `skills/builtin/` contains project-maintained core skills that should never be modified by the skills_manager, while `skills/user/` contains dynamically installed skills from skills.sh ecosystem or custom user additions.

---

## Directory Structure

```
skills/
├── builtin/                    # Project-maintained (never auto-modified)
│   ├── __init__.py
│   ├── shell.py               # Shell command execution
│   ├── files.py               # File operations
│   ├── web_search.py          # DuckDuckGo search
│   ├── routing.py             # Message routing
│   └── skills_manager.py      # Install/remove skills
│
└── user/                       # Dynamically managed
    ├── *.md                   # SKILL.md from skills.sh
    ├── *.py                   # Custom Python skills
    └── */                     # Skill directories
```

---

## Ownership Rules

| Directory | Owner | Can Modify | Examples |
|-----------|-------|------------|----------|
| `builtin/` | Project | Manual only | shell, files, web_search |
| `user/` | skills_manager | Dynamic | installed from skills.sh |

**Critical**: `skills_manager` NEVER touches `builtin/` directory.

---

## Skills Manager Tools

| Tool | Purpose |
|------|---------|
| `skills_find` | Search skills.sh for available skills |
| `skills_add` | Install skill to `user/` directory |
| `skills_list` | List installed user skills |
| `skills_remove` | Remove skill from `user/` directory |

---

## Installation Flow

```
User: "How do I improve React performance?"
  │
  ├─► Bot calls skills_find("react")
  │   └─► Returns: vercel-labs/agent-skills@react-best-practices
  │
  ├─► Bot presents options to user
  │
  ├─► User approves: "Install it"
  │
  └─► Bot calls skills_add("vercel-labs/...")
      └─► Skill installed to skills/user/
          └─► Loaded on next message
```

---

## Codebase Reference

- `skills/__init__.py` - Skill loading and registration
- `skills/builtin/skills_manager.py` - Installation tools
- `skills/user/` - User-installed skills directory

---

## Related

- `../guides/shell-security.md` - Shell skill security
- `../../../project/project-context.md` - Project structure
