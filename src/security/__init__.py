"""
src/security/__init__.py — Security utilities for workspace confinement.

Provides path validation, command sanitization, prompt injection defense,
and payload signing to ensure all file operations remain within the
designated workspace directory and user input is safely handled.
"""

from src.security.audit import audit_log
from src.security.path_validator import (
    PathSecurityError,
    is_path_in_workspace,
    validate_command_paths,
    validate_path,
)
from src.security.prompt_injection import (
    ContentFilterResult,
    InjectionDetectionResult,
    check_system_prompt_length,
    detect_injection,
    filter_response_content,
    sanitize_user_input,
)
from src.security.signing import (
    IntegrityError,
    get_scheduler_secret,
    sign_payload,
    verify_payload,
)

__all__ = [
    "audit_log",
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
    "IntegrityError",
    "get_scheduler_secret",
    "sign_payload",
    "verify_payload",
]
