"""
src/config — Configuration management package.

Provides:
  - Config: Main configuration dataclass
  - LLMConfig, WhatsAppConfig, NeonizeConfig: Sub-configs
  - load_config, save_config: Configuration file operations
  - CONFIG_PATH: Default configuration file path
"""

from src.config.config import (
    Config,
    LLMConfig,
    WhatsAppConfig,
    NeonizeConfig,
    MemoryConfig,
    load_config,
    save_config,
    CONFIG_PATH,
    DEPRECATED_OPTIONS,
    RENAMED_OPTIONS,
)
from src.config.config_schema import (
    validate_config,
    validate_config_dict,
    ConfigValidationError,
    add_schema_version,
    format_validation_errors,
)

__all__ = [
    "Config",
    "LLMConfig",
    "WhatsAppConfig",
    "NeonizeConfig",
    "MemoryConfig",
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
