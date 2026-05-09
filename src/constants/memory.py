"""Memory/history constants — context budgets, token estimation, monitoring thresholds."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Memory / History Limits
# ─────────────────────────────────────────────────────────────────────────────

# Maximum number of recent messages to include in LLM context.
# Balances context quality against token costs and latency.
DEFAULT_MEMORY_MAX_HISTORY: int = 50

# Heuristic: approximate characters per token for English text.
# Used for fast token estimation without external dependencies (tiktoken).
# English averages ~4 chars/token; mixed/multilingual is lower (~3).
CHARS_PER_TOKEN: int = 4

# Heuristic: approximate characters per token for CJK text
# (Chinese, Japanese, Korean).  CJK characters each represent a word or
# morpheme and tokenize to roughly 1-2 tokens per character, so we use 1.5
# as the ratio — significantly lower than the English 4 chars/token.
CJK_CHARS_PER_TOKEN: float = 1.5

# Total token budget for system prompt + history sent to the LLM.
# Set conservatively to fit within typical model context windows (128k).
# Leaves headroom for the model's response tokens.
DEFAULT_CONTEXT_TOKEN_BUDGET: int = 100_000

# ─────────────────────────────────────────────────────────────────────────────
# Memory Monitoring Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default warning threshold for memory usage (percentage).
# When system memory usage exceeds this, a warning is logged.
MEMORY_WARNING_THRESHOLD_PERCENT: float = 80.0

# Default critical threshold for memory usage (percentage).
# When system memory usage exceeds this, an error is logged.
MEMORY_CRITICAL_THRESHOLD_PERCENT: float = 90.0

# Default interval for periodic memory checks (seconds).
# How often the memory monitor logs usage stats.
MEMORY_CHECK_INTERVAL_SECONDS: float = 60.0
