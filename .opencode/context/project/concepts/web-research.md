<!-- Context: project/concepts/web-research | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Web Research Skill

**Core Idea**: Combined web search (DuckDuckGo) and page crawling in a single skill. Three actions — `search` for finding results, `crawl` for extracting page content, and `search_and_crawl` for the combined workflow (search → pick top URLs → crawl each).

**Source**: `FEATURES.md` — Web Research Skill section (archived 2026-04-16)

---

## Key Points

- **Single skill, three actions**: `search`, `crawl`, `search_and_crawl`
- **DuckDuckGo search**: No API key required, configurable `max_results` (default 5)
- **Page crawling**: HTTP GET with content extraction, optional CSS `selector` for targeted extraction
- **Combined workflow**: `search_and_crawl` runs search then automatically crawls top results
- **Rate-limited**: Counts as expensive skill (10 calls / 60s)

---

## Actions

```
┌──────────────┐   ┌──────────────┐   ┌─────────────────┐
│   search     │   │    crawl     │   │ search_and_crawl │
│              │   │              │   │                  │
│ query ──▶    │   │ URLs ──▶     │   │ search ──▶       │
│ DuckDuckGo   │   │ HTTP GET ──▶ │   │ top URLs ──▶     │
│ results      │   │ extract      │   │ crawl each       │
└──────────────┘   └──────────────┘   └─────────────────┘
```

---

## Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | required | Search query (search, search_and_crawl) |
| `urls` | array | required | URLs to crawl (crawl) |
| `max_results` | int | 5 | Limit search results |
| `selector` | string | null | CSS selector for targeted extraction |

---

## Quick Example

```
User: "Search for Python async best practices"
  → web_research(action="search", query="Python async best practices", max_results=5)

User: "Crawl this page and extract the main content"
  → web_research(action="crawl", urls=["https://example.com/guide"], selector="article")

User: "Find and summarize the top 3 results about FastAPI"
  → web_research(action="search_and_crawl", query="FastAPI tutorial", max_results=3)
```

---

## Codebase

- `skills/builtin/web_research.py` — WebResearchSkill (search + crawl + combined)

## Related

- `concepts/skills-system.md` — How skills are registered and executed
- `lookup/built-in-skills.md` — Full skill reference
