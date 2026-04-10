"""
src/security/__init__.py — Security utilities for workspace confinement.

Provides path validation, command sanitization, and prompt injection defense
to ensure all file operations remain within the designated workspace directory
and user input is safely handled.
"""

from src.security.path_validator import (
    validate_path,
    validate_command_paths,
    is_path_in_workspace,
    PathSecurityError,
)
from src.security.prompt_injection import (
    detect_injection,
    sanitize_user_input,
    check_system_prompt_length,
    filter_response_content,
    InjectionDetectionResult,
    ContentFilterResult,
)

__all__ = [
    "validate_path",
    "validate_command_paths",
    "is_path_in_workspace",
    "PathSecurityError",
    "detect_injection",
    "sanitize_user_input",
    "check_system_prompt_length",
    "filter_response_content",
    "InjectionDetectionResult",
    "ContentFilterResult",
]
