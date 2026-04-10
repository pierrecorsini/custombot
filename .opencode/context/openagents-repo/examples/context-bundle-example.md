<!-- Context: openagents-repo/examples | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Example: Context Bundle (Data Analyst Agent)

**Purpose**: Complete example of a filled context bundle for creating a data analyst agent

---

## The Bundle

```markdown
# Context Bundle: Create Data Analyst Agent
Session: 20250121-143022-a4f2 | For: TaskManager

## Task Overview
Create data analyst agent for data analysis, visualization, and statistics.

## Context Files
**Standards**: code-quality.md, test-coverage.md, documentation.md
**Repo**: quick-start.md, core-concepts/agents.md, core-concepts/evals.md, registry.md
**Guides**: adding-agent-basics.md, testing-agent.md, updating-registry.md

## Key Requirements
- File at `.opencode/agent/data/data-analyst.md`
- Frontmatter: id, name, description, category, type, tags, tools, permissions
- Eval structure at `evals/agents/data/data-analyst/`
- Registry entry in registry.json

## Files to Create
- `.opencode/agent/data/data-analyst.md` — Agent definition
- `evals/agents/data/data-analyst/config/eval-config.yaml` — Eval config
- `evals/agents/data/data-analyst/tests/smoke-test.yaml` — Smoke test
- `evals/agents/data/data-analyst/tests/data-analysis-test.yaml` — Capability test

## Files to Modify
- `registry.json` — Add data-analyst entry

## Success Criteria
- [ ] Agent file with proper frontmatter
- [ ] Smoke test passes
- [ ] Data analysis test passes
- [ ] Registry validates
```

---

## Key Patterns Demonstrated

- **Context references**: Full paths to load, not content duplication
- **Binary criteria**: Each item is pass/fail
- **Create vs Modify**: Clear separation
- **Validation commands**: Included in bundle

## Related

- `blueprints/context-bundle-template.md` — Blank template
- `templates/context-bundle-template.md` — Template variant
