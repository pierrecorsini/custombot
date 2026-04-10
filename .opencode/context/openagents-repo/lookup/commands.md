<!-- Context: openagents-repo/lookup | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Lookup: Command Reference

**Purpose**: Quick reference for common commands

---

## Registry

```bash
./scripts/registry/validate-registry.sh            # Validate
./scripts/registry/auto-detect-components.sh --dry-run   # Preview
./scripts/registry/auto-detect-components.sh --auto-add  # Apply
```

## Testing

```bash
cd evals/framework
npm run eval:sdk -- --agent={cat}/{agent} --pattern="{test}.yaml"  # Single test
npm run eval:sdk -- --agent={cat}/{agent}                          # All agent tests
npm run eval:sdk                                                   # All tests
npm run eval:sdk -- --agent={agent} --debug                        # Debug
./scripts/validation/validate-test-suites.sh                       # Validate suites
```

## Installation

```bash
./install.sh --list                          # List available
./install.sh {profile}                       # Install (essential|developer|business|full)
./install.sh --component agent:{name}        # Specific component
./install.sh developer --skip-existing       # Skip collisions
./install.sh developer --force               # Overwrite
REGISTRY_URL="file://$(pwd)/registry.json" ./install.sh --list  # Local test
```

## Version

```bash
cat VERSION && cat package.json | jq '.version'                    # Check
echo "0.X.Y" > VERSION                                              # Update
jq '.version = "0.X.Y"' package.json > tmp && mv tmp package.json
```

## Context Dependencies

```bash
/check-context-deps              # Analyze all
/check-context-deps {agent}      # Specific agent
/check-context-deps --fix        # Auto-fix
```

## Release

```bash
git add VERSION package.json CHANGELOG.md
git commit -m "chore: bump version to 0.X.Y"
git tag -a v0.X.Y -m "Release v0.X.Y"
git push origin main && git push origin v0.X.Y
gh release create v0.X.Y --title "v0.X.Y" --notes "See CHANGELOG.md"
```

## Debugging

```bash
ls -lt .tmp/sessions/ | head -5                      # Recent sessions
cat .tmp/sessions/{id}/session.json | jq              # View session
cat .tmp/sessions/{id}/events.json | jq               # View events
```

## Full Validation

```bash
./scripts/registry/validate-registry.sh && \
./scripts/validation/validate-test-suites.sh && \
cd evals/framework && npm run eval:sdk
```

## Related

- `quick-start.md` — Getting started
- `lookup/file-locations.md` — File paths
