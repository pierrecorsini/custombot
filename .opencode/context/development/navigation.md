<!-- Context: development/navigation | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Development Navigation

**Purpose**: Software development across all stacks

---

## Structure

```
development/
├── navigation.md
├── ui-navigation.md           # Specialized
├── backend-navigation.md      # Specialized
├── fullstack-navigation.md    # Specialized
│
├── principles/                # Universal (language-agnostic)
│   ├── navigation.md
│   ├── clean-code.md
│   └── api-design.md
│
├── frameworks/                # Full-stack frameworks
│   ├── navigation.md
│   └── tanstack-start/
│
├── ai/                        # AI & Agents
│   ├── navigation.md
│   └── mastra-ai/
│
├── frontend/                  # Client-side
│   ├── navigation.md
│   ├── when-to-delegate.md    # When to use frontend-specialist
│   └── react/
│       ├── navigation.md
│       └── react-patterns.md
│
├── backend/                   # Server-side (future)
│   ├── navigation.md
│   ├── api-patterns/
│   ├── nodejs/
│   ├── python/
│   └── authentication/
│
├── data/                      # Data layer (future)
│   ├── navigation.md
│   ├── sql-patterns/
│   ├── nosql-patterns/
│   └── orm-patterns/
│
├── integration/               # Connecting systems (future)
│   ├── navigation.md
│   ├── package-management/
│   ├── api-integration/
│   └── third-party-services/
│
└── infrastructure/            # DevOps (future)
    ├── navigation.md
    ├── docker/
    └── ci-cd/
```

---

## Quick Routes

| Task | Path |
|------|------|
| **UI/Frontend** | `ui-navigation.md` |
| **When to delegate frontend** | `frontend/when-to-delegate.md` |
| **Backend/API** | `backend-navigation.md` |
| **Full-stack** | `fullstack-navigation.md` |
| **Clean code** | `principles/clean-code.md` |
| **API design** | `principles/api-design.md` |

---

## By Concern

**Principles** → Universal development practices
**Frameworks** → Full-stack frameworks (Tanstack Start, Next.js)
**AI** → AI frameworks and agent runtimes (MAStra AI)
**Frontend** → React patterns and component design
**Backend** → APIs, Node.js, Python, auth (future)
**Data** → SQL, NoSQL, ORMs (future)
**Integration** → Packages, APIs, services (future)
**Infrastructure** → Docker, CI/CD (future)

---

## Lookup (Quick Reference)

| Topic | File |
|-------|------|
| **typing.Protocol** | `lookup/typing-protocol.md` |
| **Protocol Patterns** | `lookup/typing-protocol-patterns.md` |

---

## New Context (Harvested 2026-03-20)

### Concepts
- **Message Routing** → `concepts/message-routing.md` - Route messages to instruction files
- **Task History** → `concepts/task-history.md` - Persist scheduler execution history
- **Skills Architecture** → `concepts/skills-architecture.md` - Dual-directory skill system (builtin/user)

### Examples
- **Routing Rule Schema** → `examples/routing-rule-schema.md` - JSON schema for routing rules

### Guides
- **E2E Testing** → `guides/e2e-testing.md` - pytest patterns for CLI testing
- **Shell Security** → `guides/shell-security.md` - Command blocklist for shell skill
- **Optimization Patterns** → `guides/optimization-patterns.md` - O(n²)→O(1), HTTP pooling, timeouts

### Integrations
- **Baileys Bridge** → `integrations/baileys/concepts/bridge-pattern.md` - Node.js bridge pattern
- **Baileys REST API** → `integrations/baileys/examples/rest-api.md` - Bridge endpoints

---

## New Context (Harvested 2026-03-27)

### Concepts
- **Memory Safety Patterns** → `concepts/memory-safety-patterns.md` - Bounded caches, automatic pruning
- **Security Patterns** → `concepts/security-patterns.md` - Audit logging, env var protection, input validation
- **Performance Patterns** → `concepts/performance-patterns.md` - Early rejection, fail fast

### Examples
- **Rate Limiter (Bounded)** → `examples/rate-limiter-bounded.md` - Memory-safe rate limiting with deque
- **Shell Skill Security** → `examples/shell-skill-security.md` - Env var protection, command filtering
- **Path Sanitization** → `examples/path-sanitization.md` - Cross-platform safe filenames

---

## New Context (Harvested 2026-03-31)

### Concepts
- **Project Store** → `concepts/project-store.md` - Hybrid SQLite project tracking with graph + vector search
- **Knowledge Graph** → `concepts/knowledge-graph.md` - BFS traversal on knowledge_links

### Examples
- **Project Store Schema** → `examples/project-store-schema.md` - Full SQL schema (4 tables)
- **Knowledge Skills Schema** → `examples/knowledge-skills-schema.md` - 10 LLM-callable skill definitions

### Guides
- **Project Context Injection** → `guides/project-context-injection.md` - How project knowledge flows into system prompt

---

## Related Context

- **Core Standards** → `../core/standards/navigation.md`
- **UI Patterns** → `../ui/navigation.md`
