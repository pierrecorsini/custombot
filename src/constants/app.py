"""Application main-loop constants — per-category retry policies."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Main-Loop Retry Policies
# ─────────────────────────────────────────────────────────────────────────────

# LLM transient errors (timeout, rate-limit, connection, circuit-breaker-open):
# exponential backoff with jitter.  3 retries at 2s → 4s → 8s (worst ~16s).
MAIN_LOOP_LLM_TRANSIENT_MAX_RETRIES: int = 3
MAIN_LOOP_LLM_TRANSIENT_INITIAL_DELAY: float = 2.0

# Channel disconnections (BridgeError, ConnectionError):
# fixed-interval retry with jitter.  5 retries at 5s intervals (worst ~30s).
MAIN_LOOP_CHANNEL_DISCONNECT_MAX_RETRIES: int = 5
MAIN_LOOP_CHANNEL_DISCONNECT_RETRY_DELAY: float = 5.0

# Backoff multiplier for exponential delay calculation.
MAIN_LOOP_BACKOFF_MULTIPLIER: int = 2
