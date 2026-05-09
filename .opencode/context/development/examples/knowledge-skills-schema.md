<!-- Context: development/examples/knowledge-skills-schema | Priority: low | Version: 1.0 | Updated: 2026-03-31 -->

# Example: Knowledge Skills Definitions

**Purpose**: 10 LLM-callable skill tools for project & knowledge management

**Source**: `src/skills/builtin/project_skills.py`

---

## Skills Registry

| Skill | Purpose | Key Params |
|-------|---------|------------|
| `project_create` | Create new project | name, description, tags |
| `project_list` | List/filter projects | status, tag |
| `project_info` | Project details + recent knowledge | project_id, include_knowledge |
| `project_update` | Update name/description/status/tags | project_id, status, ... |
| `project_archive` | Archive a project | project_id |
| `knowledge_add` | Add entry to project | project_id, text, title, category, link_to |
| `knowledge_search` | Hybrid vector+graph search | project_id, query, category, limit |
| `knowledge_link` | Link two entries | from_id, to_id, relation |
| `knowledge_list` | List entries for project | project_id, category, limit |
| `project_recall` | Full project context for prompt | project_id |

---

## Registration Pattern

```python
# In src/skills/__init__.py load_builtins():
if project_store is not None:
    graph = ProjectGraph(project_store)
    recall = ProjectRecall(project_store, graph, vector_memory)
    self.register(ProjectCreateSkill(project_store))
    self.register(KnowledgeAddSkill(recall, project_store))
    # ... etc
```

---

## Builder Integration

```python
# In src/builder.py:
project_store = ProjectStore(db_path=str(workspace / ".data" / "projects.db"))
project_store.connect()
skills.load_builtins(db=db, vector_memory=vector_memory, project_store=project_store)
Bot(config=..., project_store=project_store)
```

---

## Related

- `concepts/project-store.md` — Store architecture
- `concepts/skills-architecture.md` — Skills system overview
