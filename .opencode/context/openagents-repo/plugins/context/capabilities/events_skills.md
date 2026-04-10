<!-- Context: openagents-repo/events_skills | Priority: low | Version: 1.0 | Updated: 2026-02-15 -->

# OpenCode Events: Skills Plugin Implementation

**Core Idea**: The Skills Plugin uses `tool.execute.before` (inject skill content into conversation) and `tool.execute.after` (enhance output with emoji title) event hooks. Skill lookup uses O(1) Map for performance.

---

## Event Hooks Used

| Hook | When | Purpose |
|------|------|---------|
| `tool.execute.before` | Before tool runs | Inject skill content as silent prompt (`noReply: true`) |
| `tool.execute.after` | After tool completes | Add emoji title to output for visual feedback |

### Before Hook (Content Injection)

```typescript
const beforeHook = async (input: any, output: any) => {
  if (input.tool.startsWith("skills_")) {
    const skill = skillMap.get(input.tool)  // O(1) lookup
    if (skill) {
      await ctx.client.session.prompt({
        path: { id: input.sessionID },
        body: {
          agent: input.agent,
          noReply: true,  // Don't trigger AI response
          parts: [{ type: "text", text: `📚 Skill: ${skill.name}\n\n${skill.content}` }]
        }
      })
    }
  }
}
```

### After Hook (Output Enhancement)

```typescript
const afterHook = async (input: any, output: any) => {
  if (input.tool.startsWith("skills_")) {
    const skill = skillMap.get(input.tool)
    if (skill && output.output) {
      output.title = `📚 ${skill.name}`
    }
  }
}
```

---

## Event Lifecycle

```
Agent calls skill tool (e.g., skills_brand_guidelines)
  → tool.execute.before fires → inject skill content (noReply: true)
  → tool.execute() runs → returns "Skill activated: {name}"
  → tool.execute.after fires → adds 📚 emoji title
  → Agent receives result + skill content in history
```

---

## Key Design Decisions

- **Map over Array**: O(1) skill lookup vs O(n) — critical at scale (100+ skills = 100x faster)
- **Hooks over embedded logic**: Separates tool execution from delivery (SOLID, testable)
- **noReply: true**: Skill content persists in history without triggering extra AI response

## Plugin Return Object

```typescript
return {
  tool: tools,
  "tool.execute.before": beforeHook,
  "tool.execute.after": afterHook,
}
```

## References

- `context/capabilities/events.md` — Event system overview
- `context/reference/best-practices.md` — Plugin patterns
