<!-- Context: openagents-repo/lookup | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Lookup: File Locations

**Purpose**: Quick reference for finding and adding files

---

## Directory Tree (Abbreviated)

```
.opencode/
├── agent/{category}/{name}.md          # Agents
├── agent/subagents/{cat}/{name}.md     # Subagents
├── command/{name}.md                  # Commands
├── context/{category}/{topic}.md      # Context
├── prompts/{category}/{agent}/        # Model variants
├── tool/{name}.md                     # Custom tools
└── plugin/                             # Plugins
evals/
├── framework/src/                   # Eval framework (TypeScript)
└── agents/{cat}/{agent}/tests/      # Test suites
scripts/{purpose}/{action}-{target}.sh  # Scripts
registry.json | VERSION | package.json   # Root files
```

---

## Where Is...?

| Component | Location |
|-----------|----------|
| Core agents | `.opencode/agent/core/` |
| Category agents | `.opencode/agent/{category}/` |
| Subagents | `.opencode/agent/subagents/` |
| Commands | `.opencode/command/` |
| Context | `.opencode/context/{category}/` |
| Agent tests | `evals/agents/{category}/{agent}/tests/` |
| Eval config | `evals/agents/{category}/{agent}/config/config.yaml` |
| Registry scripts | `scripts/registry/` |
| Validation scripts | `scripts/validation/` |

## Where Do I Add...?

| What | Where |
|------|-------|
| New agent | `.opencode/agent/{category}/{name}.md` |
| New subagent | `.opencode/agent/subagents/{category}/{name}.md` |
| New command | `.opencode/command/{name}.md` |
| New context | `.opencode/context/{category}/{name}.md` |
| New test | `evals/agents/{category}/{agent}/tests/{name}.yaml` |

## Key Agent Files

```
.opencode/agent/core/openagent.md
.opencode/agent/core/opencoder.md
.opencode/agent/content/copywriter.md
.opencode/agent/data/data-analyst.md
.opencode/agent/subagents/code/coder-agent.md
.opencode/agent/subagents/code/reviewer.md
```

## Naming Conventions

- Files: `{name}.md` or `{domain}-specialist.md`
- Categories: lowercase, singular (`development`, `content`)
- Scripts: `{action}-{target}.sh`

## Quick Find Commands

```bash
find .opencode/agent -name "{name}.md"    # Find agent
find evals/agents -name "*.yaml"          # Find tests
find .opencode/context -name "*.md"      # Find context
```

## Related

- `quick-start.md` — Getting started
- `lookup/commands.md` — Command reference
