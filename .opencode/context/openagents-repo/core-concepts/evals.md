<!-- Context: openagents-repo/evals | Priority: high | Version: 1.0 | Updated: 2026-02-15 -->
# Core Concept: Eval Framework

The eval framework is a TypeScript-based testing system that validates agent behavior through YAML test definitions, session collection, rule-based evaluators, and pass/fail reports. Located at `evals/framework/`.

---

## Key Points

- **Test definitions**: YAML files defining conversation + expectations per agent
- **Session collection**: Records of agent interactions stored in `.tmp/sessions/`
- **Evaluators**: Rules checking approval gates, context loading, tool usage, error handling, execution balance
- **Reports**: Pass/fail with specific violation details

---

## Architecture

```
Test Definition (YAML) → SDK Test Runner → Agent Execution → Session Collection → Event Timeline → Evaluators → Report
```

---

## Evaluators

| Evaluator | What It Validates |
|-----------|-------------------|
| **Approval Gate** | Agent requests approval before write/edit/bash |
| **Context Loading** | Agent loads required context before implementation |
| **Tool Usage** | Uses `read` not `bash cat`, `grep` not `bash grep`, etc. |
| **Stop on Failure** | Reports errors, proposes fix, requests approval (no auto-fix) |
| **Execution Balance** | Reasonable read vs execute ratio |

---

## Test Structure

```
evals/agents/{category}/{agent-name}/
├── config/config.yaml      # Agent + model + timeout + suites
└── tests/
    ├── smoke-test.yaml     # Basic functionality
    ├── approval-gate.yaml  # Approval workflow
    └── context-loading.yaml # Context usage
```

### Minimal Test Example

```yaml
name: Smoke Test
agent: core/openagent
model: anthropic/claude-sonnet-4-5
conversation:
  - role: user
    content: "Hello, can you help me?"
expectations:
  - type: no_violations
```

### Expectation Types

| Type | Validates |
|------|-----------|
| `no_violations` | No evaluator violations occurred |
| `specific_evaluator` | Named evaluator passed/failed as expected |
| `tool_usage` | Specific tools used with min_count |
| `context_loaded` | Specific context files were loaded |

---

## Running Tests

```bash
cd evals/framework
npm run eval:sdk -- --agent={category}/{agent}           # Run all tests for agent
npm run eval:sdk -- --agent={category}/{agent} --debug   # Debug mode
npm run eval:sdk                                        # Run all tests
```

### Debugging Failures

```bash
# Run with debug output
npm run eval:sdk -- --agent={agent} --pattern="{test}" --debug

# Check session data
ls .tmp/sessions/  # Find session, then inspect session.json and events.json
```

---

## Related Files

- **Testing guide**: `guides/testing-agent.md`
- **Agent concepts**: `core-concepts/agents.md`

---

**Last Updated**: 2025-12-10 | **Version**: 0.5.0
