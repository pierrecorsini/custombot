"""
src/config — Configuration management package.

Provides:
  - Config: Main configuration dataclass
  - LLMConfig, WhatsAppConfig, NeonizeConfig: Sub-configs
  - load_config, save_config: Configuration file operations
  - CONFIG_PATH: Default configuration file path
"""

from src.config.config_schema_defs import (
    CONFIG_PATH,
    DEPRECATED_OPTIONS,
    RENAMED_OPTIONS,
    Config,
    LLMConfig,
    NeonizeConfig,
    WhatsAppConfig,
)
from src.config.config_loader import load_config, save_config
from src.config.config_schema import (
    ConfigValidationError,
    add_schema_version,
    format_validation_errors,
    validate_config,
    validate_config_dict,
)

__all__ = [
    "Config",
    "LLMConfig",
    "WhatsAppConfig",
    "NeonizeConfig",
    "load_config",
    "save_config",
    "CONFIG_PATH",
    "DEPRECATED_OPTIONS",
    "RENAMED_OPTIONS",
    "validate_config",
    "validate_config_dict",
    "ConfigValidationError",
    "add_schema_version",
    "format_validation_errors",
]
