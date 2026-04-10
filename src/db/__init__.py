"""
src/db — Database package with file-based persistence layer.

Provides:
  - Database: Async file-based database
  - ValidationResult, RecoveryResult, CorruptionResult: Result types
  - get_database: Singleton accessor
"""

from src.db.db import (
    Database,
    ValidationResult,
    get_database,
    _validate_chat_id,
    _sanitize_chat_id_for_path,
)
from src.db.db_integrity import (
    CorruptionResult,
    MessageLine,
    calculate_checksum,
    validate_checksum,
    detect_corruption_sync,
    backup_file_sync,
    repair_file_sync,
    validate_all_sync,
)
from src.db.db_index import (
    RecoveryResult,
    load_index,
    save_index,
    rebuild_index,
    recover_index,
)

__all__ = [
    # Main database class
    "Database",
    "get_database",
    # Result types
    "ValidationResult",
    "RecoveryResult",
    "CorruptionResult",
    "MessageLine",
    # Integrity functions
    "calculate_checksum",
    "validate_checksum",
    "detect_corruption_sync",
    "backup_file_sync",
    "repair_file_sync",
    "validate_all_sync",
    # Index functions
    "load_index",
    "save_index",
    "rebuild_index",
    "recover_index",
    # Utility functions
    "_validate_chat_id",
    "_sanitize_chat_id_for_path",
]
