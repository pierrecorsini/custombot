"""
src/db — Database package with file-based persistence layer.

Provides:
  - Database: Async file-based database (thin facade)
  - MessageStore: JSONL message persistence, indexing, and retrieval
  - CompressionService: Conversation-history compression
  - FileHandlePool: Bounded LRU pool of append-mode file handles
  - ValidationResult, RecoveryResult, CorruptionResult: Result types
  - get_database: Singleton accessor
"""

from src.db.db import (
    Database,
    ValidationResult,
    _sanitize_chat_id_for_path,
    _validate_chat_id,
    get_database,
)
from src.db.db_index import (
    RecoveryResult,
    load_index,
    rebuild_index,
    recover_index,
    save_index,
)
from src.db.db_integrity import (
    CorruptionResult,
    MessageLine,
    backup_file_sync,
    calculate_checksum,
    detect_corruption_sync,
    repair_file_sync,
    validate_all_sync,
    validate_checksum,
)
from src.db.file_pool import FileHandlePool
from src.db.message_store import MessageStore
from src.db.compression import CompressionService

__all__ = [
    # Main database class
    "Database",
    "get_database",
    # Decomposed sub-services
    "MessageStore",
    "CompressionService",
    "FileHandlePool",
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
