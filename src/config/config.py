"""
config.py — Backward-compatible re-export shim.

The implementation lives in focused modules:
    - config_schema_defs.py  — Dataclass definitions + JSON Schema validation
    - config_validation.py   — Validation and logging helpers
    - config_loader.py       — Load/save logic and dict → dataclass construction

This file re-exports everything so existing ``from src.config.config import X``
and ``from src.config import X`` continue to work unchanged.
"""

# ── Data model ──────────────────────────────────────────────────────────────
from src.config.config_schema_defs import (  # noqa: F401
    CONFIG_PATH,
    DEPRECATED_OPTIONS,
    RENAMED_OPTIONS,
    Config,
    LLMConfig,
    MiddlewareConfig,
    NeonizeConfig,
    ShellConfig,
    WhatsAppConfig,
)

# ── Validation helpers ──────────────────────────────────────────────────────
from src.config.config_validation import (  # noqa: F401
    _check_deprecated_options,
    _check_unknown_keys,
    _collect_known_field_names,
    _get_default_values,
    _get_suggestion_for_error,
    _log_default_values_used,
    _log_effective_config,
    _log_validation_errors,
    _redact_secrets,
)

# ── Load / save logic ───────────────────────────────────────────────────────
from src.config.config_loader import (  # noqa: F401
    _apply_env_overrides,
    _from_dict,
    _load_and_validate_file,
    _validate_config_type,
    load_config,
    save_config,
)
