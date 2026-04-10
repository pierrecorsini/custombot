<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Profile Validation

**Core Idea**: When adding agents, add to appropriate profiles in registry.json. Profiles are pre-configured bundles users install.

---

## Profiles

| Profile | Contents |
|---------|----------|
| **essential** | Core agents only (openagent + core subagents) |
| **developer** | All dev agents + code subagents + dev commands/context |
| **business** | Content agents + data agents + image tools |
| **full** | Developer + business (everything except meta) |
| **advanced** | Full + meta agents (system-builder, repo-manager) |

---

## Profile Assignment Matrix

| Category | Essential | Developer | Business | Full | Advanced |
|----------|-----------|-----------|----------|------|----------|
| core | ✅ | ✅ | ✅ | ✅ | ✅ |
| development | ❌ | ✅ | ❌ | ✅ | ✅ |
| content/data | ❌ | ❌ | ✅ | ✅ | ✅ |
| meta | ❌ | ❌ | ❌ | ❌ | ✅ |

---

## Validation Steps After Adding Agent

```bash
# 1. Add to components
./scripts/registry/auto-detect-components.sh --auto-add

# 2. Manually add to appropriate profiles in registry.json
#    e.g., "agent:your-agent" → developer, full, advanced

# 3. Validate
./scripts/registry/validate-registry.sh

# 4. Test
REGISTRY_URL="file://$(pwd)/registry.json" ./install.sh --list | grep "your-agent"
```

## Common Mistakes

- ❌ Adding to components but forgetting profiles
- ❌ Wrong profile assignment (dev agent in business profile)
- ❌ Adding to `full` but not `advanced`

## Related

- `core-concepts/registry.md` — Registry concepts
- `guides/updating-registry.md` — Registry update guide
