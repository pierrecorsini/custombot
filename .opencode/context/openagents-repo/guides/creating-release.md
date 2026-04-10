<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Creating a Release

**Core Idea**: Semantic versioning (MAJOR.MINOR.PATCH), update VERSION + package.json + CHANGELOG, commit, tag, push, create GitHub release.

---

## Quick Steps

```bash
# 1. Update version
echo "0.X.Y" > VERSION
jq '.version = "0.X.Y"' package.json > tmp && mv tmp package.json

# 2. Update CHANGELOG (manual edit)

# 3. Commit and tag
git add VERSION package.json CHANGELOG.md
git commit -m "chore: bump version to 0.X.Y"
git tag -a v0.X.Y -m "Release v0.X.Y"

# 4. Push
git push origin main && git push origin v0.X.Y

# 5. GitHub release
gh release create v0.X.Y --title "v0.X.Y" --notes "See CHANGELOG.md"
```

---

## Versioning Rules

| Change | Example | When |
|--------|---------|------|
| PATCH | 0.5.0 → 0.5.1 | Bug fixes |
| MINOR | 0.5.0 → 0.6.0 | New features (backward compatible) |
| MAJOR | 0.5.0 → 1.0.0 | Breaking changes |

## CHANGELOG Format

```markdown
## [0.X.Y] - YYYY-MM-DD
### Added / Changed / Fixed / Removed
- Item 1
- Item 2
```

## Checklist

- [ ] All tests pass
- [ ] Registry validates
- [ ] VERSION + package.json match
- [ ] CHANGELOG updated
- [ ] Committed, tagged, pushed
- [ ] GitHub release created

## Common Issues

| Issue | Fix |
|-------|-----|
| Version mismatch | Update both VERSION and package.json |
| Tag exists | `git tag -d v0.X.Y && git push origin :refs/tags/v0.X.Y` |
| Push rejected | `git pull origin main` first |

## Related

- `scripts/versioning/bump-version.sh` — Version script
- `guides/updating-registry.md` — Registry changes
