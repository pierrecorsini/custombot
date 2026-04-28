"""Routing engine constants — file watching, match cache."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Routing Engine — File Watching
# ─────────────────────────────────────────────────────────────────────────────

# Minimum interval (seconds) between stale-checks on instruction .md files.
# Prevents redundant stat() calls when match() is invoked at high frequency.
# Set to 5s so the per-message _is_stale() call triggers at most one filesystem
# scan (os.scandir + stat on every .md) every 5 seconds, reducing I/O on the
# hot path while still detecting instruction-file changes promptly.
ROUTING_WATCH_DEBOUNCE_SECONDS: float = 5.0

# TTL (seconds) for the routing match result cache. Identical message signatures
# within this window return the cached match result without re-evaluating rules.
ROUTING_MATCH_CACHE_TTL_SECONDS: float = 5.0

# Maximum number of cached routing match results. Bounded to prevent unbounded
# memory growth; evicts least-recently-used entries when full.
ROUTING_MATCH_CACHE_MAX_SIZE: int = 500
