"""HTTP/network timeouts and connection pool limits."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# HTTP / Network Timeouts
# ─────────────────────────────────────────────────────────────────────────────

# Default timeout for HTTP requests (in seconds).
DEFAULT_HTTP_TIMEOUT: float = 30.0

# Connection timeout for HTTP requests (in seconds).
DEFAULT_HTTP_CONNECT_TIMEOUT: float = 10.0

# Maximum number of concurrent TCP connections in the httpx connection pool
# used by the OpenAI client. Under high concurrency (many chats hitting the
# LLM simultaneously), connection reuse eliminates TCP handshake overhead.
DEFAULT_HTTPX_MAX_CONNECTIONS: int = 20

# Maximum number of idle keep-alive connections to retain in the pool.
# Must be <= DEFAULT_HTTPX_MAX_CONNECTIONS.  Keep-alive avoids reconnect
# overhead for sequential requests to the same provider endpoint.
DEFAULT_HTTPX_MAX_KEEPALIVE_CONNECTIONS: int = 10

# Timeout for the LLM connection warmup request during startup (seconds).
# Sends a lightweight models.list() call to pre-establish the TCP + TLS
# connection before the first user message arrives.  Failures are non-fatal.
LLM_WARMUP_TIMEOUT: float = 10.0

# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Maximum time to wait for QR code scan (in seconds).
MAX_QR_SCAN_WAIT: int = 60

# Maximum time to wait for the channel to connect during startup (in seconds).
# Covers QR scan, neonize handshake, and initial sync.  If the channel
# hasn't connected within this window, startup is aborted with a clear error.
DEFAULT_CHANNEL_STARTUP_TIMEOUT: float = 300.0  # 5 minutes
