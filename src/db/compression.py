"""
src/db/compression.py — Conversation-history compression service.

Archives old JSONL messages when a chat exceeds a line threshold, replacing
them with a summary stored in a companion ``.compressed_summary.json`` file.
The most recent messages are kept intact in the JSONL.

Extracted from ``db.py`` to isolate the compression responsibility and make
it independently testable.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from typing import Any

from src.constants import (
    COMPRESSION_KEEP_RECENT,
    COMPRESSION_LINE_THRESHOLD,
)
from src.core.errors import NonCriticalCategory, log_noncritical
from src.db.db_index import save_index
from src.db.db_utils import (
    _atomic_write,
    _db_log_extra,
    _track_db_latency,
)
from src.db.file_pool import FileHandlePool
from src.utils import json_dumps, json_loads

log = logging.getLogger(__name__)


class CompressionService:
    """Manages conversation-history compression for long-lived chats.

    When a chat's JSONL file exceeds ``COMPRESSION_LINE_THRESHOLD`` lines,
    the oldest messages are archived into a summary file and removed from
    the JSONL, keeping only the most recent messages. This reduces disk I/O
    and reverse-seek latency for conversations with thousands of messages.
    """

    def __init__(
        self,
        file_pool: FileHandlePool,
        messages_dir: Path,
        message_file_fn,  # Callable[[str], Path]
        get_message_lock_fn,  # Callable[[str], Awaitable[asyncio.Lock]]
        get_index_lock_fn,  # Callable[[], asyncio.Lock]
        message_id_index: dict[str, None],
        mark_index_dirty_fn,  # Callable[[], None]
        atomic_write_fn,  # Callable[[Path, str], None]
        vector_memory: Any | None = None,
    ) -> None:
        self._file_pool = file_pool
        self._messages_dir = messages_dir
        self._message_file = message_file_fn
        self._get_message_lock = get_message_lock_fn
        self._get_index_lock = get_index_lock_fn
        self._message_id_index = message_id_index
        self._mark_index_dirty = mark_index_dirty_fn
        self._atomic_write = atomic_write_fn
        self._vector_memory = vector_memory

    def set_vector_memory(self, vector_memory: Any) -> None:
        """Set the optional vector memory store for embedding compression summaries."""
        self._vector_memory = vector_memory

    def compressed_summary_file(self, chat_id: str) -> Path:
        """Get the compressed summary file path for a chat."""
        return self._message_file(chat_id).with_suffix(".compressed_summary.json")

    async def get_compressed_summary(self, chat_id: str) -> str | None:
        """Return the compressed history summary text for a chat, if any.

        The summary describes archived messages that were removed during
        compression.  Returns ``None`` when no compression has occurred.
        """
        summary_file = self.compressed_summary_file(chat_id)
        return await asyncio.to_thread(
            self._read_compressed_summary_sync, summary_file,
        )

    @staticmethod
    def _read_compressed_summary_sync(summary_file: Path) -> str | None:
        """Read compressed summary from file (sync, for thread pool)."""
        if not summary_file.exists():
            return None
        try:
            content = summary_file.read_text(encoding="utf-8")
            parsed = json_loads(content)
            if isinstance(parsed, dict):
                return parsed.get("content")
        except Exception:
            log_noncritical(
                NonCriticalCategory.COMPRESSION,
                "Failed to read compressed summary from %s",
                summary_file,
                logger=log,
            )
        return None

    async def compress_chat_history(self, chat_id: str) -> bool:
        """Compress a chat's history when the JSONL file exceeds the line threshold.

        Archives the oldest messages by replacing them with a summary stored in
        a separate ``.compressed_summary.json`` file.  The most recent messages
        are kept intact in the JSONL.  This reduces disk I/O and reverse-seek
        latency for long-lived conversations.

        Returns:
            ``True`` if compression was performed, ``False`` if skipped.
        """
        msg_file = self._message_file(chat_id)
        if not msg_file.exists():
            return False

        lock = await self._get_message_lock(chat_id)
        async with lock:
            result = await asyncio.to_thread(
                self._compress_chat_history_sync, chat_id, msg_file,
            )

        if result.get("compressed"):
            removed_ids = result.get("removed_ids", [])
            if removed_ids:
                async with self._get_index_lock():
                    for mid in removed_ids:
                        self._message_id_index.pop(mid, None)
                    self._mark_index_dirty()

            # Best-effort: embed compression summary for semantic retrieval
            summary_text = result.get("summary_text")
            if summary_text and self._vector_memory is not None:
                try:
                    await self._vector_memory.save(
                        chat_id, summary_text, category="compression_summary",
                    )
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.EMBEDDING,
                        "Failed to embed compression summary for %s",
                        chat_id,
                        logger=log,
                        extra=_db_log_extra(chat_id),
                    )

        return bool(result.get("compressed"))

    def _compress_chat_history_sync(
        self, chat_id: str, msg_file: Path,
    ) -> dict:
        """Synchronous compression logic (runs in thread pool).

        Returns a dict with ``compressed`` (bool) and ``removed_ids`` (list).
        """
        # Quick size gate: skip if file is too small to have threshold lines.
        # Minimum ~200 bytes per JSONL line is a conservative estimate.
        try:
            file_size = msg_file.stat().st_size
        except FileNotFoundError:
            log.debug(
                "compress: file disappeared before stat — %s [%s]",
                msg_file.name, chat_id,
            )
            return {"compressed": False}
        except OSError:
            log.warning(
                "compress: I/O error during stat — %s [%s]",
                msg_file.name, chat_id,
            )
            return {"compressed": False}

        if file_size < COMPRESSION_LINE_THRESHOLD * 200:
            return {"compressed": False}

        # Read the file
        try:
            content = msg_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.debug(
                "compress: file disappeared before read — %s [%s]",
                msg_file.name, chat_id,
            )
            return {"compressed": False}
        except OSError:
            log.warning(
                "compress: I/O error during read — %s [%s]",
                msg_file.name, chat_id,
            )
            return {"compressed": False}

        lines = content.splitlines()

        # Separate header from message lines
        header_line: str | None = None
        msg_lines: list[str] = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            if i == 0 and header_line is None:
                try:
                    parsed = json_loads(line)
                    if isinstance(parsed, dict) and parsed.get("type") == "header":
                        header_line = line
                        continue
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.COMPRESSION,
                        "Malformed JSONL header line in compression scan",
                        logger=log,
                    )
            msg_lines.append(line)

        if len(msg_lines) <= COMPRESSION_LINE_THRESHOLD:
            return {"compressed": False}

        # Split into old (to archive) and recent (to keep)
        compress_count = len(msg_lines) - COMPRESSION_KEEP_RECENT
        old_lines = msg_lines[:compress_count]
        recent_lines = msg_lines[compress_count:]

        # Extract metadata from old messages
        first_ts: float | None = None
        last_ts: float | None = None
        user_count = 0
        assistant_count = 0
        removed_ids: list[str] = []

        for line in old_lines:
            try:
                msg = json_loads(line)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.COMPRESSION,
                    "Skipping malformed JSONL line during compression",
                    logger=log,
                )
                continue

            msg_id = msg.get("id")
            if msg_id:
                removed_ids.append(msg_id)

            ts = msg.get("timestamp")
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            role = msg.get("role")
            if role == "user":
                user_count += 1
            elif role == "assistant":
                assistant_count += 1

        # Accumulate with any prior compression summary
        summary_file = msg_file.with_suffix(".compressed_summary.json")
        total_removed = len(old_lines)
        total_user = user_count
        total_assistant = assistant_count

        if summary_file.exists():
            try:
                existing = json_loads(summary_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    meta = existing.get("_metadata", {})
                    total_removed += meta.get("messages_removed", 0)
                    total_user += meta.get("user_messages", 0)
                    total_assistant += meta.get("assistant_messages", 0)
                    existing_first = meta.get("first_timestamp")
                    existing_last = meta.get("last_timestamp")
                    if existing_first is not None and (
                        first_ts is None or existing_first < first_ts
                    ):
                        first_ts = existing_first
                    if existing_last is not None and (
                        last_ts is None or existing_last > last_ts
                    ):
                        last_ts = existing_last
            except Exception:
                log_noncritical(
                    NonCriticalCategory.COMPRESSION,
                    "Failed to read existing summary metadata from %s during compression",
                    summary_file,
                    logger=log,
                )
        date_range = ""
        if first_ts is not None and last_ts is not None:
            start = datetime.datetime.fromtimestamp(
                first_ts, tz=datetime.timezone.utc,
            ).strftime("%Y-%m-%d")
            end = datetime.datetime.fromtimestamp(
                last_ts, tz=datetime.timezone.utc,
            ).strftime("%Y-%m-%d")
            date_range = f" from {start} to {end}"

        summary_text = (
            f"\U0001f4cb [Conversation History Compressed] "
            f"{total_removed} messages "
            f"({total_user} user, {total_assistant} assistant)"
            f"{date_range} have been archived. "
            f"The current conversation continues from the most recent messages. "
            f"Use memory_recall if you need to reference specific past interactions."
        )

        # Write summary file (small, fast — do this before truncating JSONL)
        summary_data = {
            "content": summary_text,
            "_metadata": {
                "messages_removed": total_removed,
                "user_messages": total_user,
                "assistant_messages": total_assistant,
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
                "last_compressed": time.time(),
            },
        }
        try:
            summary_file.write_text(
                json_dumps(summary_data, ensure_ascii=False), encoding="utf-8",
            )
        except OSError as exc:
            log.warning(
                "Failed to write compression summary for %s: %s", chat_id, exc,
                extra=_db_log_extra(chat_id),
            )
            return {"compressed": False}

        # Build new JSONL: header + recent messages
        new_lines: list[str] = []
        if header_line:
            new_lines.append(header_line)
        new_lines.extend(recent_lines)

        # Invalidate pooled handle before rewrite
        self._file_pool.invalidate(msg_file)

        # Write the truncated file atomically
        new_content = "\n".join(new_lines) + "\n"
        try:
            self._atomic_write(msg_file, new_content)
        except OSError as exc:
            log.warning(
                "Failed to write compressed JSONL for %s: %s", chat_id, exc,
                extra=_db_log_extra(chat_id),
            )
            return {"compressed": False}

        log.info(
            "Compressed chat history for %s: %d old messages archived, "
            "%d recent messages kept (file: %s)",
            chat_id,
            compress_count,
            len(recent_lines),
            msg_file.name,
            extra=_db_log_extra(chat_id),
        )

        return {
            "compressed": True,
            "removed_ids": removed_ids,
            "summary_text": summary_text,
        }
