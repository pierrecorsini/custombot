<!-- Context: openagents-repo/navigation | Priority: critical | Version: 1.1 | Updated: 2026-04-05 -->

# OpenAgents Control Repository Context

**Purpose**: Context files specific to the OpenAgents Control repository

**Last Updated**: 2026-04-05

---

## Quick Navigation

| Function | Files | Purpose |
|----------|-------|---------|
| **Concepts** | 1 file | Core ideas and principles |
| **Core Concepts** | 5 files | Foundational knowledge |
| **Examples** | 2 files | Working code samples |
| **Guides** | 18 files | Step-by-step workflows |
| **Lookup** | 4 files | Quick reference tables |
| **Errors** | 1 file | Common issues + solutions |
| **Plugins** | Plugin system | Plugin architecture and capabilities |
| **Templates** | 1 file | Reusable templates |
| **Blueprints** | 1 file | Architecture blueprints |
| **Quality** | 1 file | Quality standards |

---

## Concepts (Core Ideas)

| File | Topic | Priority |
|------|-------|----------|
| `concepts/subagent-testing-modes.md` | Standalone vs delegation testing | ⭐⭐⭐⭐⭐ |

**When to read**: Before testing any subagent

---

## Core Concepts (Foundational)

| File | Topic | Priority |
|------|-------|----------|
| `core-concepts/agents.md` | How agents work | ⭐⭐⭐⭐⭐ |
| `core-concepts/agent-metadata.md` | Agent metadata and configuration | ⭐⭐⭐⭐⭐ |
| `core-concepts/evals.md` | How testing works | ⭐⭐⭐⭐⭐ |
| `core-concepts/registry.md` | How registry works | ⭐⭐⭐⭐ |
| `core-concepts/categories.md` | How organization works | ⭐⭐⭐ |

**When to read**: First time working in this repo

---

## Examples (Working Code)

| File | Topic | Priority |
|------|-------|----------|
| `examples/subagent-prompt-structure.md` | Optimized subagent prompt template | ⭐⭐⭐⭐⭐ |
| `examples/context-bundle-example.md` | Context bundle usage example | ⭐⭐⭐⭐ |

**When to read**: When creating subagent prompts or context bundles

---

## Guides (Step-by-Step)

| File | Topic | Priority |
|------|-------|----------|
| `guides/adding-agent-basics.md` | How to add new agents (basics) | ⭐⭐⭐⭐ |
| `guides/adding-agent-testing.md` | How to add agent tests | ⭐⭐⭐⭐ |
| `guides/adding-skill-basics.md` | How to add OpenCode skills | ⭐⭐⭐⭐ |
| `guides/adding-skill-example.md` | Skill creation example walkthrough | ⭐⭐⭐⭐ |
| `guides/adding-skill-implementation.md` | Skill implementation details | ⭐⭐⭐⭐ |
| `guides/testing-subagents.md` | How to test subagents standalone | ⭐⭐⭐⭐⭐ |
| `guides/testing-subagents-approval.md` | Subagent testing approval process | ⭐⭐⭐⭐ |
| `guides/testing-agent.md` | How to test agents | ⭐⭐⭐⭐ |
| `guides/subagent-invocation.md` | How to invoke subagents | ⭐⭐⭐⭐ |
| `guides/external-libraries-workflow.md` | How to handle external library dependencies | ⭐⭐⭐⭐ |
| `guides/github-issues-workflow.md` | How to work with GitHub issues and project board | ⭐⭐⭐⭐ |
| `guides/npm-publishing.md` | How to publish package to npm | ⭐⭐⭐ |
| `guides/updating-registry.md` | How to update registry | ⭐⭐⭐ |
| `guides/debugging.md` | How to debug issues | ⭐⭐⭐ |
| `guides/resolving-installer-wildcard-failures.md` | Fix wildcard context install failures | ⭐⭐⭐ |
| `guides/creating-release.md` | How to create releases | ⭐⭐ |
| `guides/building-cli-compact.md` | Building compact CLI | ⭐⭐⭐ |
| `guides/profile-validation.md` | Profile validation process | ⭐⭐⭐ |

**When to read**: When performing specific tasks

---

## Lookup (Quick Reference)

| File | Topic | Priority |
|------|-------|----------|
| `lookup/subagent-test-commands.md` | Subagent testing commands | ⭐⭐⭐⭐⭐ |
| `lookup/subagent-framework-maps.md` | Subagent framework mappings | ⭐⭐⭐⭐⭐ |
| `lookup/file-locations.md` | Where files are located | ⭐⭐⭐⭐ |
| `lookup/commands.md` | Available slash commands | ⭐⭐⭐ |

**When to read**: Quick command lookups and file locations

---

## Errors (Troubleshooting)

| File | Topic | Priority |
|------|-------|----------|
| `errors/tool-permission-errors.md` | Tool permission issues | ⭐⭐⭐⭐⭐ |

**When to read**: When tests fail with permission errors

---

## Sub-Systems

| Category | Description | Navigation |
|----------|-------------|------------|
| **Plugins** | Plugin architecture and capabilities | `plugins/navigation.md` |
| **Templates** | Reusable context bundle templates | `templates/navigation.md` |
| **Blueprints** | Architecture blueprints | `blueprints/navigation.md` |
| **Quality** | Quality standards and dependencies | `quality/navigation.md` |

---

## Loading Strategy

### For Subagent Testing:
1. Load `concepts/subagent-testing-modes.md` (understand modes)
2. Load `guides/testing-subagents.md` (step-by-step)
3. Load `guides/testing-subagents-approval.md` (approval process)
4. Reference `lookup/subagent-test-commands.md` (commands)
5. If errors: Load `errors/tool-permission-errors.md`

### For Agent Creation:
1. Load `core-concepts/agents.md` (understand system)
2. Load `core-concepts/agent-metadata.md` (metadata format)
3. Load `guides/adding-agent-basics.md` (step-by-step)
4. **If using external libraries**: Load `guides/external-libraries-workflow.md` (fetch docs)
5. Load `examples/subagent-prompt-structure.md` (if subagent)
6. Load `guides/testing-agent.md` (validate)

### For Issue Management:
1. Load `guides/github-issues-workflow.md` (understand workflow)
2. Create issues with proper labels and templates
3. Add to project board for tracking
4. Process requests systematically

### For Debugging:
1. Load `guides/debugging.md` (general approach)
2. Load specific error file from `errors/`
3. Reference `lookup/file-locations.md` (find files)

---

## File Size Compliance

All files follow MVI principle (<200 lines):

- ✅ Concepts: <100 lines
- ✅ Core Concepts: <100 lines
- ✅ Examples: <100 lines
- ✅ Guides: <150 lines
- ✅ Lookup: <100 lines
- ✅ Errors: <150 lines

---

## Related Context

- `../core/` - Core system context (standards, patterns)
- `../core/context-system/` - Context management system
- `quick-start.md` - 2-minute repo orientation
- `plugins/navigation.md` - Plugin system context
- `templates/navigation.md` - Template system
- `blueprints/navigation.md` - Blueprint system
- `quality/navigation.md` - Quality standards

---

## Contributing

When adding new context files:

1. Follow MVI principle (<200 lines)
2. Use function-based organization (concepts/, examples/, guides/, lookup/, errors/)
3. Update this navigation.md
4. Add cross-references to related files
5. Validate with `/context validate`
