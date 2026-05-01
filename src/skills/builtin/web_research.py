"""
src/skills/builtin/web_research.py — Combined web search and crawl skill.

Searches the web, then optionally crawls result URLs for full content.
Can also directly crawl any URL.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.skills.base import BaseSkill, validate_input

log = logging.getLogger(__name__)


class WebResearchSkill(BaseSkill):
    name = "web_research"
    description = (
        "Search the web and/or crawl URLs for content. "
        "Use 'search' to find URLs, 'crawl' to extract content from URLs, "
        "or 'search_and_crawl' to do both."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "crawl", "search_and_crawl"],
                "description": "Action to perform: search, crawl, or search_and_crawl",
            },
            "query": {
                "type": "string",
                "description": "Search query (required for search actions)",
            },
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs to crawl (required for crawl actions)",
            },
            "max_results": {
                "type": "integer",
                "description": "Max search results (default 5)",
                "default": 5,
            },
            "selector": {
                "type": "string",
                "description": "CSS selector to target specific content",
            },
        },
        "required": ["action"],
    }

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        action: str = "search",
        query: str = "",
        urls: list[str] | None = None,
        max_results: int = 5,
        selector: str = "",
        **kwargs: Any,
    ) -> str:
        if action == "search":
            return await self._search(query, max_results)
        elif action == "crawl":
            return await self._crawl(urls or [], selector)
        elif action == "search_and_crawl":
            return await self._search_and_crawl(query, max_results, selector)
        else:
            return f"Unknown action: {action}"

    async def _search(self, query: str, max_results: int) -> str:
        """Search the web using DuckDuckGo."""
        if not query:
            return "Error: query is required for search."

        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
            except ImportError:
                return "Error: Install ddgs: pip install ddgs"

        try:
            loop = asyncio.get_running_loop()
            with DDGS() as ddgs:
                results = await loop.run_in_executor(
                    None, lambda: list(ddgs.text(query, max_results=max_results))
                )
        except Exception as exc:
            return f"Search failed: {exc}"

        if not results:
            return f"No results for: {query}"

        lines = [f"## Search: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            url = r.get("href", "")
            body = r.get("body", "").replace("\n", " ").strip()
            lines.append(f"**{i}. {title}**\n   {url}\n   {body}\n")

        return "\n".join(lines)

    async def _crawl(self, urls: list[str], selector: str) -> str:
        """Crawl URLs and extract content."""
        if not urls:
            return "Error: urls required for crawl."

        try:
            import crawl4ai
        except ImportError:
            return "Error: Install crawl4ai: pip install crawl4ai && crawl4ai-setup"

        results = []
        for url in urls:
            result = await self._crawl_single(url, selector)
            results.append(result)

        return "\n\n---\n\n".join(results)

    async def _crawl_single(self, url: str, selector: str) -> str:
        """Crawl a single URL using crawl4ai directly (no shell injection risk)."""
        from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

        try:
            config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)
            if selector:
                config.css_selector = selector

            async with AsyncWebCrawler() as crawler:
                result = await asyncio.wait_for(crawler.arun(url=url, config=config), timeout=60.0)

            if result.success:
                content = result.markdown.raw_markdown or ""
                return f"## {result.url}\n\n{content[:5000]}"
            return f"Failed: {result.error_message or 'Unknown error'}"
        except asyncio.TimeoutError:
            return f"Timeout crawling {url}"
        except Exception as exc:
            return f"Error crawling {url}: {exc}"

    async def _search_and_crawl(self, query: str, max_results: int, selector: str) -> str:
        """Search and then crawl the top results."""
        search_result = await self._search(query, max_results)

        # Extract URLs from search results
        import re

        urls = re.findall(r"https?://[^\s\)]+", search_result)
        urls = urls[:max_results]

        if not urls:
            return search_result + "\n\nNo URLs to crawl."

        crawl_results = await self._crawl(urls, selector)
        return f"{search_result}\n\n--- Crawled Content ---\n\n{crawl_results}"
