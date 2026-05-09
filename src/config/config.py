"""
config.py — Backward-compatible re-export shim (DEPRECATED).

The implementation lives in focused modules:
    - config_schema_defs.py  — Dataclass definitions + JSON Schema validation
    - config_validation.py   — Validation and logging helpers
    - config_loader.py       — Load/save logic and dict → dataclass construction

This file re-exports everything so existing ``from src.config.config import X``
continues to work.  New code should import from the canonical modules directly:

    from src.config import Config, load_config          # via __init__.py
    from src.config.config_schema_defs import ShellConfig  # specific submodule
    from src.config.config_loader import _from_dict        # internal helpers

This shim will be removed in a future release.
"""

import warnings

warnings.warn(
    "Importing from 'src.config.config' is deprecated. "
    "Use 'src.config' (package __init__) or the specific submodule "
    "(config_schema_defs, config_validation, config_loader) instead.",
    DeprecationWarning,
    stacklevel=2,
)

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
