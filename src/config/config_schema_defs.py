"""
config_schema_defs.py — Configuration dataclass definitions.

Pure data model layer with no I/O or validation logic. Each dataclass
declares its fields and defaults; construction from dicts, file I/O,
and schema validation live in companion modules.

Public classes: LLMConfig, NeonizeConfig, ShellConfig, MiddlewareConfig,
WhatsAppConfig, Config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.constants import (
    DEFAULT_CHAT_LOCK_CACHE_SIZE,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_LOCK_EVICTION_POLICY,
    DEFAULT_MAX_CONCURRENT_MESSAGES,
    DEFAULT_MEMORY_MAX_HISTORY,
    DEFAULT_SHUTDOWN_TIMEOUT,
    MAX_TOOL_ITERATIONS,
    WORKSPACE_DIR,
)

# Default config file path — re-exported by config.py and __init__.py.
CONFIG_PATH = __import__("pathlib").Path(f"{WORKSPACE_DIR}/config.json")

# ─────────────────────────────────────────────────────────────────────────────
# Deprecated / renamed option tracking
# ─────────────────────────────────────────────────────────────────────────────

# Options that are deprecated and will be removed in future versions.
# Format: option_path -> (removal_version, suggestion)
DEPRECATED_OPTIONS: Dict[str, Tuple[str, str]] = {
    # Example (not currently deprecated, shown for future reference):
    # "llm.legacy_mode": ("2.0", "Remove this option; legacy mode is no longer supported"),
}

# Options that have been renamed.
# Format: old_path -> new_path
RENAMED_OPTIONS: Dict[str, str] = {
    # Example: "whatsapp.bridge_url": "whatsapp.neonize.db_path",
}


# ─────────────────────────────────────────────────────────────────────────────
# Sub-configs
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LLMConfig:
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: Optional[int] = None  # Optional: only sent to API if set
    timeout: float = DEFAULT_LLM_TIMEOUT  # Default timeout in seconds for LLM calls
    system_prompt_prefix: str = ""
    max_tool_iterations: int = MAX_TOOL_ITERATIONS
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    # When True, LLM responses are streamed token-by-token to reduce perceived
    # latency.  Falls back to non-streaming for tool-call turns.  Not all
    # providers support streaming — disable if the provider returns errors.
    stream_response: bool = False

    def __repr__(self) -> str:
        if self.api_key:
            key_masked = f"***({len(self.api_key)} chars)"
        else:
            key_masked = "NOT_SET"
        return f"LLMConfig(model={self.model!r}, base_url={self.base_url!r}, api_key={key_masked!r}, temp={self.temperature})"


@dataclass
class NeonizeConfig:
    """Neonize — native Python WhatsApp client via whatsmeow (Go)."""

    db_path: str = f"{WORKSPACE_DIR}/whatsapp_session.db"

    def __repr__(self) -> str:
        return f"NeonizeConfig(db_path={self.db_path!r})"


@dataclass
class ShellConfig:
    """Shell skill security configuration — command allowlist/denylist."""

    # Additional command patterns to block beyond the built-in denylist.
    # Each entry is a regex pattern matched against the full command string.
    command_denylist: List[str] = field(default_factory=list)
    # Command patterns that bypass the denylist (allowlist takes precedence).
    # If a command matches any allowlist pattern, it is allowed even if it
    # would otherwise be blocked by the denylist.
    command_allowlist: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"ShellConfig(denylist={len(self.command_denylist)} patterns, "
            f"allowlist={len(self.command_allowlist)} patterns)"
        )


@dataclass
class MiddlewareConfig:
    """Middleware pipeline configuration.

    Allows customizing the message-processing middleware chain without
    editing source code.  Built-in middleware names are referenced by
    string; custom middleware can be added via dotted import paths.

    Built-in names:
        operation_tracker, metrics, inbound_logging, preflight,
        typing, error_handler, handle_message
    """

    # Ordered list of built-in middleware names to include.
    # When empty (default), the full built-in order is used.
    middleware_order: List[str] = field(default_factory=list)
    # Dotted import paths for custom middleware factories
    # (e.g. ``"my_package.middleware:my_factory"``).
    extra_middleware_paths: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"MiddlewareConfig(order={self.middleware_order or 'default'}, "
            f"extra={len(self.extra_middleware_paths)})"
        )


@dataclass
class WhatsAppConfig:
    provider: str = "neonize"
    neonize: NeonizeConfig = field(default_factory=NeonizeConfig)
    # If non-empty, only these numbers (e164, no +) will be answered
    allowed_numbers: List[str] = field(default_factory=list)
    # Must be explicitly set to False to reject messages when allowed_numbers is empty.
    # Defaults to True for backward compatibility (original behavior: accept all when list is empty).
    allow_all: bool = True

    def __repr__(self) -> str:
        nums = f"{len(self.allowed_numbers)} numbers" if self.allowed_numbers else "all"
        return f"WhatsAppConfig(provider={self.provider!r}, allowed={nums}, allow_all={self.allow_all}, neonize={self.neonize!r})"


# ─────────────────────────────────────────────────────────────────────────────
# Root config
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    middleware: MiddlewareConfig = field(default_factory=MiddlewareConfig)
    # Whether to process historical/offline messages that arrived before the bot connected
    load_history: bool = False
    # How many past messages to include in LLM context
    memory_max_history: int = DEFAULT_MEMORY_MAX_HISTORY
    # Whether to auto-load skills from skills_user_directory on startup
    skills_auto_load: bool = True
    # Directory for user-authored skill files (Python or skill.md)
    skills_user_directory: str = field(default_factory=lambda: f"{WORKSPACE_DIR}/skills")
    # Logging options
    log_incoming_messages: bool = True  # Log incoming messages to console
    log_routing_info: bool = False  # Log routing rule matching details
    # Graceful shutdown timeout (seconds) - force quit after this
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT
    # Logging format: "text" (human-readable) or "json" (structured for aggregation)
    log_format: str = "text"
    # Log rotation configuration - defaults to workspace/logs/custombot.log
    log_file: str = field(default_factory=lambda: f"{WORKSPACE_DIR}/logs/custombot.log")
    log_max_bytes: int = 10 * 1024 * 1024  # 10 MB max file size before rotation
    log_backup_count: int = 5  # Number of backup log files to keep
    # Logging verbosity: "quiet" (errors only), "normal" (balanced), "verbose" (debug)
    log_verbosity: str = "normal"
    # LLM request/response logging: one JSON file per request and per response
    log_llm: bool = False
    # Maximum worker threads for the asyncio ThreadPoolExecutor.
    # Controls concurrency for asyncio.to_thread() calls (DB, file I/O, vector
    # memory).  None means use DEFAULT_THREAD_POOL_WORKERS from constants.
    max_thread_pool_workers: Optional[int] = None
    # Maximum number of per-chat locks retained in the LRU cache.
    # Controls how many concurrent chats can have cached locks before eviction.
    # Raise for deployments with >1000 concurrent active chats.
    max_chat_lock_cache_size: int = DEFAULT_CHAT_LOCK_CACHE_SIZE
    # Eviction policy when the per-chat lock cache is full and all entries are in-use.
    # "grow" allows unbounded growth with a warning (default, safe for correctness).
    # "reject_on_full" raises RuntimeError to prevent memory bloat.
    max_chat_lock_eviction_policy: str = DEFAULT_LOCK_EVICTION_POLICY.value
    # Maximum number of messages processed concurrently by _on_message().
    # Caps memory usage and LLM rate-limit pressure under load without blocking
    # the event loop — excess messages await a free slot via asyncio.Semaphore.
    max_concurrent_messages: int = DEFAULT_MAX_CONCURRENT_MESSAGES

    def __repr__(self) -> str:
        return (
            f"Config(llm={self.llm!r}, whatsapp={self.whatsapp!r}, "
            f"shell={self.shell!r}, middleware={self.middleware!r}, "
            f"memory_max_history={self.memory_max_history})"
        )
