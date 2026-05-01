"""
src/db/db_validation.py — Database validation logic.

Extracted from db.py for better code organization.
Provides file integrity validation, JSON parsing checks,
and message file corruption detection.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set

from src.db.db_integrity import validate_checksum
from src.utils import JsonParseMode, safe_json_parse

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationResult:
    """Result of database connection validation."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def validate_directory_access(
    dir_path: Path,
    dir_name: str,
    errors: List[str],
    warnings: List[str],
    details: Dict[str, Any],
    allow_missing: bool = False,
) -> bool:
    """
    Validate that a directory exists and is writable.

    Args:
        dir_path: Path to the directory.
        dir_name: Human-readable name for logging.
        errors: List to append error messages.
        warnings: List to append warning messages.
        details: Dict to store validation details.
        allow_missing: If True, missing directory is a warning not an error.

    Returns:
        True if directory is valid, False otherwise.
    """
    key_exists = f"{dir_name}_exists"
    key_writable = f"{dir_name}_writable"

    if not dir_path.exists():
        msg = f"{dir_name.capitalize()} does not exist"
        if allow_missing:
            msg += f" (will be created): {dir_path}"
            warnings.append(msg)
        else:
            msg += f": {dir_path}"
            errors.append(msg)
        details[key_exists] = False
        return allow_missing

    details[key_exists] = True

    if not os.access(dir_path, os.W_OK):
        errors.append(f"{dir_name.capitalize()} is not writable: {dir_path}")
        details[key_writable] = False
        return False

    details[key_writable] = True
    return True


def validate_json_file(
    file_path: Path,
    expected_type: type,
    file_name: str,
    errors: List[str],
    warnings: List[str],
    details: Dict[str, Any],
    allow_missing: bool = True,
) -> bool:
    """
    Validate a JSON file exists and has correct structure.

    Args:
        file_path: Path to the JSON file.
        expected_type: Expected type (dict or list).
        file_name: Human-readable name for logging.
        errors: List to append error messages.
        warnings: List to append warning messages.
        details: Dict to store validation details.
        allow_missing: If True, missing file is not an error.

    Returns:
        True if file is valid or allowed to be missing.
    """
    key_valid = f"{file_name}_valid"
    key_count = f"{file_name}_count"

    if not file_path.exists():
        if allow_missing:
            details[key_valid] = True
            details[key_count] = 0
            return True
        errors.append(f"{file_name} does not exist: {file_path}")
        details[key_valid] = False
        return False

    details["files_checked"] = details.get("files_checked", [])
    details["files_checked"].append(file_path.name)

    try:
        content = file_path.read_text(encoding="utf-8")
        result = safe_json_parse(content, expected_type=expected_type, mode=JsonParseMode.STRICT)

        if not result.success:
            if result.error_type == "type":
                type_name = "object" if expected_type == dict else "array"
                errors.append(f"{file_path.name} is not a valid JSON {type_name}")
            else:
                errors.append(f"{file_path.name} is corrupted: {result.error}")
            details[key_valid] = False
            return False

        details[key_valid] = True
        details[key_count] = len(result.data)
        return True

    except OSError as e:
        errors.append(f"Failed to read {file_path.name}: {e}")
        details[key_valid] = False
        return False


def validate_message_files(
    messages_dir: Path,
    max_files: int = 10,
) -> Dict[str, Any]:
    """
    Validate message files in a directory.

    Checks a sample of message files for valid JSON and checksum integrity.

    Args:
        messages_dir: Path to the messages directory.
        max_files: Maximum number of files to validate (default: 10).

    Returns:
        Dict with keys:
        - corrupted_files: List of files with JSON errors
        - checksum_errors: List of files with checksum errors
        - files_count: Total number of message files
    """
    result: Dict[str, Any] = {
        "corrupted_files": [],
        "checksum_errors": [],
        "files_count": 0,
    }

    if not messages_dir.exists():
        return result

    msg_files = list(messages_dir.glob("*.jsonl"))
    result["files_count"] = len(msg_files)

    for msg_file in msg_files[:max_files]:
        try:
            content = msg_file.read_text(encoding="utf-8")
            for line_num, line in enumerate(content.splitlines(), 1):
                if not line.strip():
                    continue

                msg = safe_json_parse(line, default=None, log_errors=False, mode=JsonParseMode.LINE)
                if msg is None:
                    result["corrupted_files"].append(f"{msg_file.name}:{line_num}")
                    continue

                # Validate checksum if present
                is_valid, error = validate_checksum(msg)
                if not is_valid:
                    result["checksum_errors"].append(f"{msg_file.name}:{line_num}")

        except OSError as e:
            result["corrupted_files"].append(f"{msg_file.name}: {e}")

    return result


def validate_database_integrity(
    data_dir: Path,
    messages_dir: Path,
    chats_file: Path,
    routing_file: Path,
    index_file: Path,
) -> ValidationResult:
    """
    Perform comprehensive database validation.

    Checks directory access, JSON file integrity, and message file corruption.

    Args:
        data_dir: Main data directory.
        messages_dir: Messages subdirectory.
        chats_file: Path to chats.json.
        routing_file: Path to routing.json.
        index_file: Path to message_index.json.

    Returns:
        ValidationResult with status and details.
    """
    errors: List[str] = []
    warnings: List[str] = []
    details: Dict[str, Any] = {
        "data_dir": str(data_dir),
        "messages_dir": str(messages_dir),
        "files_checked": [],
    }

    # Check 1: Data directory
    validate_directory_access(data_dir, "data_dir", errors, warnings, details)

    # Check 2: Messages directory
    validate_directory_access(
        messages_dir, "messages_dir", errors, warnings, details, allow_missing=True
    )

    # Check 3: chats.json
    validate_json_file(chats_file, dict, "chats", errors, warnings, details)

    # Check 4: routing.json
    validate_json_file(routing_file, list, "routing", errors, warnings, details)

    # Check 5: message_index.json
    validate_json_file(
        index_file, list, "message_index", errors, warnings, details, allow_missing=True
    )
    if details.get("message_index_valid") is False:
        # Downgrade error to warning for index (can be rebuilt)
        if any("message_index" in e for e in errors):
            errors = [e for e in errors if "message_index" not in e]
            warnings.append(
                "message_index.json is corrupted (will be rebuilt): "
                + details.get("message_index", {}).get("error", "unknown error")
            )

    # Check 6: Message files
    msg_result = validate_message_files(messages_dir)
    details["message_files_count"] = msg_result["files_count"]

    if msg_result["corrupted_files"]:
        warnings.append(
            f"Some message files have invalid JSON: {msg_result['corrupted_files'][:3]}"
        )
        details["corrupted_message_files"] = msg_result["corrupted_files"]

    if msg_result["checksum_errors"]:
        warnings.append(
            f"Some message files have checksum errors: {msg_result['checksum_errors'][:3]}"
        )
        details["checksum_errors"] = msg_result["checksum_errors"]

    # Log corruption if detected
    if msg_result["corrupted_files"] or msg_result["checksum_errors"]:
        log.warning(
            "Corruption detection: %d JSON errors, %d checksum errors in message files",
            len(msg_result["corrupted_files"]),
            len(msg_result["checksum_errors"]),
        )

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        details=details,
    )


__all__ = [
    "ValidationResult",
    "validate_directory_access",
    "validate_json_file",
    "validate_message_files",
    "validate_database_integrity",
]
