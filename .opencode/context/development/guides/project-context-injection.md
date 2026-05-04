<!-- Context: development/guides/project-context-injection | Priority: medium | Version: 1.0 | Updated: 2026-03-31 -->

# Guide: Project Context Injection

**Purpose**: How project knowledge flows into LLM system prompt

**Source**: `src/bot.py`, `src/core/context_builder.py`

---

## Flow

```
Chat message arrives (chat_id)
  ↓
Bot._get_project_context(chat_id)
  ↓ looks up project_chats for this chat_id
  ↓ for each bound project: recall.recall(project_id)
  ↓
build_context(..., project_context=result)
  ↓
System prompt gets "## 📂 Project Context" section
```

---

## How to Bind a Chat to a Project

Via WhatsApp conversation:
```
User: Create a project called "website-redesign"
Bot: [project_create] → Created project 'website-redesign'

User: Add knowledge: we're using Next.js
Bot: [knowledge_add] → Saved to website-redesign

User: Link this chat to the project
Bot: [project_store.bind_chat internally via knowledge_add with source_chat_id]
```

Or programmatically: `store.bind_chat(project_id, chat_id)`

---

## Key Implementation Points

1. **Bot constructor** accepts optional `project_store` parameter
2. **`_get_project_context(chat_id)`** — looks up bound projects, generates recall for each
3. **Both** `build_context` calls (scheduled + regular) inject project context
4. **Graceful degradation** — if no project_store or no bindings, returns None (no injection)

---

## Codebase Reference

- `src/bot.py:_get_project_context()` — Lookup + recall
- `src/core/context_builder.py:build_context()` — project_context parameter
- `src/builder.py` — Wires project_store to Bot and Skills

---

## Related

- `concepts/project-store.md` — Store architecture
- `concepts/skills-architecture.md` — Skills system
