"""
consolidation.py — Background memory consolidation job.

Periodically deduplicates, merges, and prunes episodic memories.
Extends BaseBackgroundService for managed lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from src.memory.decay import MemoryDecayManager
from src.utils.background_service import BaseBackgroundService

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.85
DEFAULT_INTERVAL_SECONDS = 3600


@dataclass(slots=True, frozen=True)
class ConsolidationResult:
    """Summary of a single consolidation run."""

    merged_count: int = 0
    pruned_count: int = 0
    summary_count: int = 0
    elapsed_seconds: float = 0.0


class MemoryConsolidationJob(BaseBackgroundService):
    """Background service that consolidates episodic memories."""

    _service_name = "Memory consolidation"

    def __init__(
        self,
        episodic_memory: Any,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        super().__init__()
        self._episodic = episodic_memory
        self._interval = interval_seconds
        self._similarity = similarity_threshold
        self._decay = MemoryDecayManager()

    async def _run_loop(self) -> None:
        """Periodically run consolidation cycles."""
        while self._running:
            try:
                result = await self._consolidate()
                log.info(
                    "Consolidation complete: merged=%d pruned=%d summary=%d (%.1fs)",
                    result.merged_count,
                    result.pruned_count,
                    result.summary_count,
                    result.elapsed_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Consolidation cycle failed", exc_info=True)
            await asyncio.sleep(self._interval)

    async def _consolidate(self) -> ConsolidationResult:
        """Execute a single consolidation pass across all episode files."""
        start = time.monotonic()

        episodic_dir = self._episodic._episodic_dir
        if not episodic_dir.exists():
            return ConsolidationResult()

        files = await asyncio.to_thread(lambda: list(episodic_dir.glob("*.jsonl")))
        total_merged = 0
        total_pruned = 0
        total_summaries = 0

        for path in files:
            merged, pruned = await self._consolidate_file(path)
            total_merged += merged
            total_pruned += pruned

        elapsed = time.monotonic() - start
        return ConsolidationResult(
            merged_count=total_merged,
            pruned_count=total_pruned,
            summary_count=total_summaries,
            elapsed_seconds=elapsed,
        )

    async def _consolidate_file(self, path: Any) -> tuple[int, int]:
        """Deduplicate and prune a single episode file."""
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        episodes: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                episodes.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue

        if not episodes:
            return 0, 0

        merged = self._merge_duplicates(episodes)
        now_hours = time.time() / 3600
        for ep in episodes:
            ep["age_hours"] = now_hours - ep.get("timestamp", 0.0) / 3600
        pruned_count = len(episodes)
        episodes = self._decay.prune_decayed(episodes)
        pruned_count = pruned_count - len(episodes)

        if merged > 0 or pruned_count > 0:
            lines = "".join(json.dumps(ep) + "\n" for ep in episodes)
            await asyncio.to_thread(path.write_text, lines, encoding="utf-8")

        return merged, pruned_count

    def _merge_duplicates(self, episodes: list[dict[str, Any]]) -> int:
        """Merge near-duplicate episodes in-place. Returns merge count."""
        merged = 0
        seen: list[int] = []
        for i, ep in enumerate(episodes):
            if i in seen:
                continue
            for j in range(i + 1, len(episodes)):
                if j in seen:
                    continue
                if self._is_similar(ep, episodes[j]):
                    # Keep the newer one with combined tags
                    if episodes[j].get("timestamp", 0) >= ep.get("timestamp", 0):
                        episodes[j].setdefault("tags", [])
                        for t in ep.get("tags", []):
                            if t not in episodes[j]["tags"]:
                                episodes[j]["tags"].append(t)
                        seen.append(i)
                    else:
                        ep.setdefault("tags", [])
                        for t in episodes[j].get("tags", []):
                            if t not in ep["tags"]:
                                ep["tags"].append(t)
                        seen.append(j)
                    merged += 1
                    break
        for idx in reversed(seen):
            episodes.pop(idx)
        return merged

    def _is_similar(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Check if two episodes have similar content."""
        if a.get("type") != b.get("type"):
            return False
        ratio = SequenceMatcher(None, a.get("content", ""), b.get("content", "")).ratio()
        return ratio >= self._similarity
