"""
Tests for src/bot/response_delivery.py — Post-ReAct response delivery pipeline.

Covers:
- dedup-keyed variant: check_outbound_with_key returns duplicate → deliver_response returns None
- generation-conflict recovery path: re-read + merge + _deduplicate_batch
- persistence-failed skip path: no DB writes when persistence_failed=True
- send_to_chat no-channel fallback: dedup recording + event emission directly
- send_to_chat with channel: delegates to channel.send_and_track()
- _deduplicate_batch: removes duplicates by (role, content) pair
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.response_delivery import (
    _deduplicate_batch,
    deliver_response,
    send_to_chat,
)
from src.security.prompt_injection import ContentFilterResult


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_db(**overrides) -> AsyncMock:
    """Create a mocked Database with standard stubs."""
    db = AsyncMock()
    db.check_generation = MagicMock(return_value=True)
    db.get_generation = MagicMock(return_value=0)
    db.get_recent_messages = AsyncMock(return_value=[])
    db.save_messages_batch = AsyncMock(return_value=["id1"])
    for k, v in overrides.items():
        setattr(db, k, v)
    return db


def _make_dedup(**overrides) -> MagicMock:
    """Create a mocked DeduplicationService — no duplicates by default."""
    dedup = MagicMock()
    dedup.check_outbound_with_key = MagicMock(return_value=(False, "fake-dedup-key"))
    dedup.record_outbound_keyed = MagicMock()
    for k, v in overrides.items():
        setattr(dedup, k, v)
    return dedup


def _make_context_assembler(**overrides) -> MagicMock:
    """Create a mocked ContextAssembler — passes through text by default."""
    ca = MagicMock()
    ca.finalize_turn = MagicMock(side_effect=lambda _cid, text: text)
    for k, v in overrides.items():
        setattr(ca, k, v)
    return ca


def _make_channel(**overrides) -> AsyncMock:
    """Create a mocked BaseChannel."""
    ch = AsyncMock()
    ch.send_and_track = AsyncMock()
    for k, v in overrides.items():
        setattr(ch, k, v)
    return ch


# Patch targets used by deliver_response / send_to_chat
_PATCH_FILTER = "src.bot.response_delivery.filter_response_content"
_PATCH_EMIT = "src.bot._event_helpers.get_event_bus"
_PATCH_CORRELATION = "src.bot.response_delivery.get_correlation_id"


# ─────────────────────────────────────────────────────────────────────────────
# deliver_response — dedup-keyed variant
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverResponseDedupKeyed:
    """Tests for outbound dedup via check_outbound_with_key."""

    async def test_duplicate_response_returns_none(self):
        """When check_outbound_with_key returns (True, key), deliver_response returns None."""
        dedup = _make_dedup(check_outbound_with_key=MagicMock(return_value=(True, "dup-key")))
        db = _make_db()
        ca = _make_context_assembler()

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_dedup",
                raw_response="Hello",
                tool_log=[],
                buffered_persist=[],
                generation=0,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        assert result is None

    async def test_duplicate_response_skips_db_writes(self):
        """Dedup-suppressed responses must not persist to DB or record outbound."""
        dedup = _make_dedup(check_outbound_with_key=MagicMock(return_value=(True, "dup-key")))
        db = _make_db()
        ca = _make_context_assembler()

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
        ):
            mock_get_bus.return_value = AsyncMock()

            await deliver_response(
                chat_id="chat_dedup",
                raw_response="Hello",
                tool_log=[],
                buffered_persist=[],
                generation=0,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        db.save_messages_batch.assert_not_awaited()
        dedup.record_outbound_keyed.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# deliver_response — generation-conflict recovery path
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverResponseGenerationConflict:
    """Tests for generation-conflict detection, re-read, and merge."""

    async def test_conflict_triggers_reread_and_merge(self):
        """When check_generation returns False, re-read + _deduplicate_batch runs."""
        dedup = _make_dedup()
        db = _make_db(
            check_generation=MagicMock(return_value=False),
            get_generation=MagicMock(return_value=7),
            get_recent_messages=AsyncMock(return_value=[]),
        )
        ca = _make_context_assembler()
        buffered = [{"role": "tool", "content": "result", "name": "bash"}]

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-001"),
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_conflict",
                raw_response="Response",
                tool_log=[],
                buffered_persist=buffered,
                generation=5,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        assert result == "Response"
        db.check_generation.assert_called_once_with("chat_conflict", 5)
        db.get_generation.assert_called_once_with("chat_conflict")
        db.get_recent_messages.assert_awaited_once()

    async def test_conflict_emits_generation_conflict_event(self):
        """Generation conflict emits EVENT_GENERATION_CONFLICT with metadata."""
        from src.core.event_bus import EVENT_GENERATION_CONFLICT

        dedup = _make_dedup()
        db = _make_db(
            check_generation=MagicMock(return_value=False),
            get_generation=MagicMock(return_value=9),
            get_recent_messages=AsyncMock(return_value=[]),
        )
        ca = _make_context_assembler()

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-002"),
        ):
            mock_bus = AsyncMock()
            mock_get_bus.return_value = mock_bus

            await deliver_response(
                chat_id="chat_conflict_evt",
                raw_response="Hello",
                tool_log=[],
                buffered_persist=[],
                generation=3,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        # Find the generation-conflict event among emitted events
        conflict_calls = [
            c for c in mock_bus.emit.call_args_list
            if c[0][0].name == EVENT_GENERATION_CONFLICT
        ]
        assert len(conflict_calls) == 1
        evt = conflict_calls[0][0][0]
        assert evt.data["chat_id"] == "chat_conflict_evt"
        assert evt.data["expected_generation"] == 3
        assert evt.data["current_generation"] == 9

    async def test_conflict_deduplicate_batch_removes_overlap(self):
        """_deduplicate_batch removes messages already in existing."""
        batch = [
            {"role": "tool", "content": "result", "name": "bash"},
            {"role": "assistant", "content": "Hello"},
        ]
        existing = [
            {"role": "tool", "content": "result", "name": "bash"},
        ]

        result = _deduplicate_batch(batch, existing)
        assert len(result) == 1
        assert result[0] == {"role": "assistant", "content": "Hello"}

    async def test_conflict_all_deduplicated_early_return(self):
        """When all messages are already persisted, batch is empty → still delivers."""
        dedup = _make_dedup()
        # Simulate existing messages containing everything in the batch
        existing = [
            {"role": "tool", "content": "result", "name": "bash"},
            {"role": "assistant", "content": "Response"},
        ]
        db = _make_db(
            check_generation=MagicMock(return_value=False),
            get_generation=MagicMock(return_value=2),
            get_recent_messages=AsyncMock(return_value=existing),
        )
        ca = _make_context_assembler()
        buffered = [{"role": "tool", "content": "result", "name": "bash"}]

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-003"),
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_all_dedup",
                raw_response="Response",
                tool_log=[],
                buffered_persist=buffered,
                generation=1,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        # Response still delivered (returned to caller), but no DB save
        assert result == "Response"
        db.save_messages_batch.assert_not_awaited()
        # dedup recording should happen via send_to_chat
        dedup.record_outbound_keyed.assert_called_once_with("fake-dedup-key")

    async def test_conflict_saves_merged_batch(self):
        """When conflict + partial overlap, only non-duplicate messages are saved."""
        dedup = _make_dedup()
        existing = [
            {"role": "tool", "content": "result", "name": "bash"},
        ]
        db = _make_db(
            check_generation=MagicMock(return_value=False),
            get_generation=MagicMock(return_value=8),
            get_recent_messages=AsyncMock(return_value=existing),
        )
        ca = _make_context_assembler()
        buffered = [
            {"role": "tool", "content": "result", "name": "bash"},
            {"role": "tool", "content": "extra", "name": "bash"},
        ]

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-004"),
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_partial",
                raw_response="Answer",
                tool_log=[],
                buffered_persist=buffered,
                generation=5,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
            )

        assert result == "Answer"
        # Only non-duplicate messages + assistant should be saved
        expected_batch = [
            {"role": "tool", "content": "extra", "name": "bash"},
            {"role": "assistant", "content": "Answer"},
        ]
        db.save_messages_batch.assert_awaited_once_with(
            chat_id="chat_partial",
            messages=expected_batch,
        )


# ─────────────────────────────────────────────────────────────────────────────
# deliver_response — persistence-failed skip path
# ─────────────────────────────────────────────────────────────────────────────


class TestDeliverResponsePersistenceFailed:
    """Tests for persistence_failed=True skipping all DB writes."""

    async def test_no_db_writes_when_persistence_failed(self):
        """When persistence_failed=True, save_messages_batch is never called."""
        dedup = _make_dedup()
        db = _make_db()
        ca = _make_context_assembler()

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_nodb",
                raw_response="Hello",
                tool_log=[],
                buffered_persist=[{"role": "user", "content": "Hi"}],
                generation=0,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
                persistence_failed=True,
            )

        assert result == "Hello"
        db.save_messages_batch.assert_not_awaited()
        db.check_generation.assert_not_called()
        db.get_recent_messages.assert_not_awaited()

    async def test_persistence_failed_still_records_dedup(self):
        """Even with persistence_failed, dedup recording + send_to_chat still happen."""
        dedup = _make_dedup()
        db = _make_db()
        ca = _make_context_assembler()

        with (
            patch(_PATCH_FILTER, return_value=ContentFilterResult(flagged=False)),
            patch(_PATCH_EMIT) as mock_get_bus,
        ):
            mock_get_bus.return_value = AsyncMock()

            result = await deliver_response(
                chat_id="chat_nodb_dedup",
                raw_response="Hello",
                tool_log=[],
                buffered_persist=[],
                generation=0,
                verbose="",
                context_assembler=ca,
                db=db,
                dedup=dedup,
                persistence_failed=True,
            )

        assert result == "Hello"
        dedup.record_outbound_keyed.assert_called_once_with("fake-dedup-key")


# ─────────────────────────────────────────────────────────────────────────────
# send_to_chat — no-channel fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestSendToChatNoChannel:
    """Tests for send_to_chat when no channel is provided."""

    async def test_records_dedup_with_key(self):
        """When dedup_key is provided, record_outbound_keyed uses it directly."""
        dedup = _make_dedup()

        with (
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-100"),
        ):
            mock_get_bus.return_value = AsyncMock()

            await send_to_chat(
                chat_id="chat_no_ch",
                text="Hello",
                dedup=dedup,
                channel=None,
                dedup_key="precomputed-key",
            )

        dedup.record_outbound_keyed.assert_called_once_with("precomputed-key")

    async def test_records_dedup_without_key_computes_hash(self):
        """When dedup_key is None, outbound_key is computed inline."""
        dedup = _make_dedup()

        with (
            patch(_PATCH_EMIT) as mock_get_bus,
            patch("src.bot.response_delivery.outbound_key", return_value="computed-key") as mock_outbound_key,
            patch(_PATCH_CORRELATION, return_value="corr-101"),
        ):
            mock_get_bus.return_value = AsyncMock()

            await send_to_chat(
                chat_id="chat_no_ch",
                text="Hello",
                dedup=dedup,
                channel=None,
                dedup_key=None,
            )

        mock_outbound_key.assert_called_once_with("chat_no_ch", "Hello")
        dedup.record_outbound_keyed.assert_called_once_with("computed-key")

    async def test_emits_response_sent_event(self):
        """send_to_chat emits response_sent event with chat_id and response_length."""
        dedup = _make_dedup()

        with (
            patch(_PATCH_EMIT) as mock_get_bus,
            patch(_PATCH_CORRELATION, return_value="corr-102"),
        ):
            mock_bus = AsyncMock()
            mock_get_bus.return_value = mock_bus

            await send_to_chat(
                chat_id="chat_evt",
                text="Hello world!",
                dedup=dedup,
                channel=None,
                dedup_key="some-key",
            )

        mock_bus.emit.assert_awaited_once()
        event = mock_bus.emit.call_args[0][0]
        assert event.name == "response_sent"
        assert event.data == {"chat_id": "chat_evt", "response_length": 12}
        assert event.source == "response_delivery.send_to_chat"
        assert event.correlation_id == "corr-102"


# ─────────────────────────────────────────────────────────────────────────────
# send_to_chat — with channel
# ─────────────────────────────────────────────────────────────────────────────


class TestSendToChatWithChannel:
    """Tests for send_to_chat when a channel is provided."""

    async def test_delegates_to_channel_send_and_track(self):
        """When channel is provided, delegates to channel.send_and_track."""
        dedup = _make_dedup()
        channel = _make_channel()

        await send_to_chat(
            chat_id="chat_with_ch",
            text="Hello",
            dedup=dedup,
            channel=channel,
            dedup_key="ch-key",
        )

        channel.send_and_track.assert_awaited_once_with(
            "chat_with_ch", "Hello", dedup=dedup, dedup_key="ch-key",
        )

    async def test_does_not_record_dedup_directly(self):
        """With channel, dedup recording is handled by channel.send_and_track."""
        dedup = _make_dedup()
        channel = _make_channel()

        await send_to_chat(
            chat_id="chat_with_ch",
            text="Hello",
            dedup=dedup,
            channel=channel,
            dedup_key="ch-key",
        )

        # dedup.record_outbound_keyed should NOT be called directly
        dedup.record_outbound_keyed.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# _deduplicate_batch — unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDeduplicateBatch:
    """Tests for _deduplicate_batch — removes duplicates by (role, content)."""

    def test_removes_exact_duplicates(self):
        """Messages with same (role, content) as existing are removed."""
        batch = [
            {"role": "assistant", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ]
        existing = [
            {"role": "assistant", "content": "Hello"},
        ]

        result = _deduplicate_batch(batch, existing)
        assert result == [{"role": "assistant", "content": "World"}]

    def test_preserves_unique_messages(self):
        """Messages not in existing are preserved."""
        batch = [
            {"role": "assistant", "content": "Hello"},
        ]
        existing = [
            {"role": "user", "content": "Hi"},
        ]

        result = _deduplicate_batch(batch, existing)
        assert result == [{"role": "assistant", "content": "Hello"}]

    def test_empty_batch_returns_empty(self):
        """Empty batch returns empty list."""
        result = _deduplicate_batch([], [{"role": "assistant", "content": "Hi"}])
        assert result == []

    def test_empty_existing_preserves_all(self):
        """Empty existing preserves all batch messages."""
        batch = [
            {"role": "assistant", "content": "Hello"},
            {"role": "tool", "content": "result"},
        ]
        result = _deduplicate_batch(batch, [])
        assert result == batch

    def test_ignores_extra_fields_in_comparison(self):
        """Comparison is only by (role, content); extra fields like 'name' are ignored."""
        batch = [
            {"role": "tool", "content": "result", "name": "bash"},
        ]
        existing = [
            {"role": "tool", "content": "result", "name": "different_tool"},
        ]

        result = _deduplicate_batch(batch, existing)
        assert result == []  # Same (role, content) → duplicate

    def test_handles_missing_role_or_content(self):
        """Messages with missing role or content use None for comparison."""
        batch = [
            {"content": "Hello"},
            {"role": "assistant"},
        ]
        existing = [
            {"role": None, "content": "Hello"},
            {"role": "assistant", "content": None},
        ]

        result = _deduplicate_batch(batch, existing)
        assert result == []

    def test_full_overlap_returns_empty(self):
        """When batch is entirely contained in existing, result is empty."""
        batch = [
            {"role": "assistant", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        existing = [
            {"role": "assistant", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "assistant", "content": "C"},
        ]

        result = _deduplicate_batch(batch, existing)
        assert result == []
