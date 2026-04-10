<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Debugging Common Issues

**Core Idea**: Systematic troubleshooting for registry, test, install, version, and CI/CD issues.

---

## Quick Diagnostics

```bash
./scripts/registry/validate-registry.sh -v     # Registry health
./scripts/validation/validate-test-suites.sh    # Test suites
cat VERSION && cat package.json | jq '.version' # Version check
cd evals/framework && npm run eval:sdk          # Run evals
```

---

## Registry Issues

| Symptom | Fix |
|---------|-----|
| Path doesn't exist | Remove entry or create file |
| Component not found | Check frontmatter, run `auto-detect-components.sh --auto-add` |
| Invalid YAML | Fix frontmatter syntax |

## Test Failures

| Violation | Fix |
|-----------|-----|
| Approval Gate | Add approval request in agent prompt |
| Context Loading | Add context loading step before implementation |
| Tool Usage | Use `read` not `bash cat`, `grep` not `bash grep` |

## Install Issues

```bash
# Missing dependencies
brew install curl jq  # macOS
sudo apt-get install curl jq  # Linux

# Test locally
REGISTRY_URL="file://$(pwd)/registry.json" ./install.sh --list

# Collision handling
./install.sh developer --skip-existing  # or --force / --backup
```

## Version Issues

```bash
# Check consistency
cat VERSION && cat package.json | jq '.version' && cat registry.json | jq '.version'

# Fix mismatch
echo "0.X.Y" > VERSION
jq '.version = "0.X.Y"' package.json > tmp && mv tmp package.json
```

## Debug Sessions

```bash
ls -lt .tmp/sessions/ | head -5           # Find recent session
cat .tmp/sessions/{id}/session.json | jq   # View session
cat .tmp/sessions/{id}/events.json | jq    # View events
```

## Related

- `guides/testing-agent.md` — Testing guide
- `guides/updating-registry.md` — Registry guide
