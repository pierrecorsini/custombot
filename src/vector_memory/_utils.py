"""Internal utilities shared across vector_memory submodules."""

from __future__ import annotations

import logging
import struct

from src.core.errors import NonCriticalCategory, log_noncritical

_log = logging.getLogger(__name__)


def _serialize_f32(vector: list[float]) -> bytes:
    """Pack a float32 list into binary BLOB for sqlite-vec."""
    return struct.pack("%sf" % len(vector), *vector)


def _track_embed_cache_event(hit: bool) -> None:
    """Report an embedding cache hit or miss to the performance metrics collector."""
    try:
        from src.monitoring.performance import get_metrics_collector

        if hit:
            get_metrics_collector().track_embed_cache_hit()
        else:
            get_metrics_collector().track_embed_cache_miss()
    except Exception:
        log_noncritical(
            NonCriticalCategory.CACHE_TRACKING,
            "Failed to track embedding cache %s event",
            "hit" if hit else "miss",
            logger=_log,
        )
