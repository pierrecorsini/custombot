<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Testing Subagents — Step-by-Step

**Core Idea**: Use `--subagent` flag (not `--agent`) to test subagents directly. Must register in 3 framework locations first.

---

## ⚠️ CRITICAL: Register Before Testing

Add subagent to **3 locations** in `evals/framework/src/`:

1. `run-sdk-tests.ts` ~line 336 — `subagentParentMap`: `'name': 'parent'`
2. `run-sdk-tests.ts` ~line 414 — `subagentPathMap`: `'name': 'PathName'`
3. `test-runner.ts` ~line 238 — `agentMap`: `'name': 'FileName.md'`

---

## Quick Start

```bash
cd evals/framework
npm run eval:sdk -- --subagent=contextscout --pattern="01-test.yaml"       # Standalone
npm run eval:sdk -- --subagent=contextscout --delegate --pattern="01.yaml"  # Integration
npm run eval:sdk -- --subagent=contextscout --pattern="01.yaml" --debug     # Debug
```

## Verification Steps

1. **Agent file**: Check frontmatter has `mode: subagent`
2. **Test config**: `evals/agents/ContextScout/config/config.yaml` — `agent: ContextScout`
3. **Run test**: Must use `--subagent` flag
4. **Check results**: Agent name should be subagent, not parent

---

## Test Organization

```
evals/agents/ContextScout/tests/
├── standalone/           # Unit tests (--subagent flag)
│   ├── 01-simple-discovery.yaml
│   └── 02-search-test.yaml
└── delegation/           # Integration tests (--agent flag)
    └── 01-openagent-delegates.yaml
```

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| OpenAgent runs instead | Used `--agent` flag | Use `--subagent` flag |
| Tool calls: 0 | Prompt doesn't emphasize tools | Add critical rules section |
| Permission denied | Tool restricted in frontmatter | Check `tools:` and `permissions:` |
| Not in framework maps | Missing registration | Add to 3 locations above |

## Writing Good Test Prompts

```yaml
# ✅ Explicit — works
prompts:
  - text: |
      Use the glob tool to find all markdown files.
      glob(pattern="*.md", path=".opencode/context/core")
```

## Related

- `concepts/subagent-testing-modes.md` — Standalone vs delegation
- `lookup/subagent-test-commands.md` — Command reference
- `errors/tool-permission-errors.md` — Permission issues
