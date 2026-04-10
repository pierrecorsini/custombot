<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: GitHub Issues Workflow

**Core Idea**: Manage issues via `gh` CLI with consistent labels, project board tracking, and PR auto-close conventions.

---

## Quick Commands

```bash
gh issue list --repo darrenhinde/OpenAgentsControl
gh issue create --repo darrenhinde/OpenAgentsControl --title "Title" --label "feature"
gh project item-add 2 --owner darrenhinde --url <issue-url>
gh issue close NUMBER --repo darrenhinde/OpenAgentsControl
```

## Label System

**Type**: `feature`, `bug`, `enhancement`, `question`
**Priority**: `priority-high`, `priority-medium`, `priority-low`
**Category**: `agents`, `framework`, `evals`, `idea`

---

## Workflow States

Backlog → Todo → In Progress → In Review → Done

## Issue Creation

```bash
gh issue create --repo darrenhinde/OpenAgentsControl \
  --title "Add feature X" \
  --label "feature,priority-medium" \
  --body "$(cat <<'EOF'
## Goals
- Goal 1
## Success Criteria
- [ ] Criterion 1
EOF
)"
```

---

## Working on Issues

1. Assign: `gh issue edit NUMBER --add-assignee @me`
2. Branch: `git checkout -b feature/issue-NUMBER-desc`
3. Reference: `git commit -m "feat: implement X (#NUMBER)"`
4. PR: `gh pr create --title "Fix #NUMBER: Description"`
5. Auto-close: Use `Closes #NUMBER` or `Fixes #NUMBER` in PR

## Epic Breakdown

Create parent issue, then subtasks with `Part of #PARENT_NUMBER` in body.

---

## Issue Templates

**Feature**: Overview → Goals → Key Features → Success Criteria
**Bug**: Description → Steps to Reproduce → Expected vs Actual → Environment
**Improvement**: Current State → Proposed → Impact → Approach

## Related

- `guides/updating-registry.md` — Registry changes
- `guides/creating-release.md` — Release process
