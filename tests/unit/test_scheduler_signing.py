"""
Tests for HMAC-SHA256 signing of scheduled task files.

Covers:
  - src/security/signing.py — sign_payload, verify_payload, file I/O helpers
  - src/scheduler.py — HMAC signing on write, verification on load
  - Backward compatibility when no secret is configured
  - Tamper detection and rejection
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.constants import SCHEDULER_HMAC_SECRET_ENV, SCHEDULER_HMAC_SIG_EXT
from src.scheduler import SCHEDULER_DIR, TASKS_FILE, TaskScheduler
from src.security.signing import (
    IntegrityError,
    get_scheduler_secret,
    read_signature_file,
    sign_payload,
    verify_payload,
    write_signature_file,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─── Helpers ──────────────────────────────────────────────────────────────

SECRET = "test-scheduler-hmac-secret-32ch"


def _tasks_file(workspace: Path, chat_id: str) -> Path:
    return workspace / chat_id / SCHEDULER_DIR / TASKS_FILE


def _sig_file(workspace: Path, chat_id: str) -> Path:
    return _tasks_file(workspace, chat_id).with_suffix(
        _tasks_file(workspace, chat_id).suffix + SCHEDULER_HMAC_SIG_EXT,
    )


def _make_task(
    schedule_type: str = "interval",
    prompt: str = "test prompt",
    **schedule_overrides,
) -> dict:
    defaults = {"seconds": 60} if schedule_type == "interval" else {"hour": 9, "minute": 0}
    schedule = {"type": schedule_type, **defaults, **schedule_overrides}
    return {"prompt": prompt, "label": "Test", "schedule": schedule}


# ─── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def on_trigger() -> AsyncMock:
    return AsyncMock(return_value="result")


@pytest.fixture
def scheduler(workspace: Path, on_trigger: AsyncMock) -> TaskScheduler:
    s = TaskScheduler()
    s.configure(workspace=workspace, on_trigger=on_trigger, on_send=AsyncMock())
    return s


@pytest.fixture(autouse=True)
def _clean_env():
    """Ensure SCHEDULER_HMAC_SECRET is not set between tests.

    Also resets the module-level cache so that ``get_scheduler_secret()``
    re-reads the environment on each test.  Without this, the cached value
    from an earlier test leaks into subsequent tests.
    """
    import src.security.signing as _signing_mod

    original = os.environ.pop(SCHEDULER_HMAC_SECRET_ENV, None)
    _signing_mod._cached_secret = _signing_mod._SENTINEL
    yield
    if original is not None:
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = original
    elif SCHEDULER_HMAC_SECRET_ENV in os.environ:
        del os.environ[SCHEDULER_HMAC_SECRET_ENV]
    _signing_mod._cached_secret = _signing_mod._SENTINEL


# ═══════════════════════════════════════════════════════════════════════════
# signing.py — sign_payload / verify_payload
# ═══════════════════════════════════════════════════════════════════════════


class TestSignPayload:
    """Tests for sign_payload()."""

    def test_returns_hex_string(self):
        payload = b'{"prompt": "hello"}'
        sig = sign_payload(SECRET, payload)
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in sig)

    def test_deterministic(self):
        payload = b"same content"
        assert sign_payload(SECRET, payload) == sign_payload(SECRET, payload)

    def test_different_secrets_different_sigs(self):
        payload = b"same content"
        sig1 = sign_payload("secret-a", payload)
        sig2 = sign_payload("secret-b", payload)
        assert sig1 != sig2

    def test_different_payloads_different_sigs(self):
        sig1 = sign_payload(SECRET, b"content-a")
        sig2 = sign_payload(SECRET, b"content-b")
        assert sig1 != sig2


class TestVerifyPayload:
    """Tests for verify_payload()."""

    def test_valid_signature(self):
        payload = b'[{"prompt": "hello"}]'
        sig = sign_payload(SECRET, payload)
        assert verify_payload(SECRET, payload, sig) is True

    def test_wrong_payload(self):
        sig = sign_payload(SECRET, b"original")
        assert verify_payload(SECRET, b"tampered", sig) is False

    def test_wrong_secret(self):
        sig = sign_payload("correct-secret", b"payload")
        assert verify_payload("wrong-secret", b"payload", sig) is False

    def test_empty_signature(self):
        assert verify_payload(SECRET, b"payload", "") is False

    def test_truncated_signature(self):
        sig = sign_payload(SECRET, b"payload")
        assert verify_payload(SECRET, b"payload", sig[:32]) is False

    def test_corrupted_signature(self):
        sig = sign_payload(SECRET, b"payload")
        corrupted = sig[:32] + "deadbeef" + sig[40:]
        assert verify_payload(SECRET, b"payload", corrupted) is False


class TestGetSchedulerSecret:
    """Tests for get_scheduler_secret()."""

    def test_returns_none_when_unset(self):
        assert get_scheduler_secret() is None

    def test_returns_secret_when_set(self):
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = "my-secret"
        assert get_scheduler_secret() == "my-secret"

    def test_trims_whitespace(self):
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = "  my-secret  "
        assert get_scheduler_secret() == "my-secret"

    def test_returns_none_for_empty(self):
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = ""
        assert get_scheduler_secret() is None

    def test_returns_none_for_whitespace_only(self):
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = "   "
        assert get_scheduler_secret() is None


# ═══════════════════════════════════════════════════════════════════════════
# signing.py — read_signature_file / write_signature_file
# ═══════════════════════════════════════════════════════════════════════════


class TestSignatureFileIO:
    """Tests for read_signature_file / write_signature_file."""

    def test_write_and_read_round_trip(self, tmp_path: Path):
        sig_path = tmp_path / "tasks.json.hmac"
        write_signature_file(sig_path, "abcdef1234567890" * 4)
        assert read_signature_file(sig_path) == "abcdef1234567890" * 4

    def test_read_missing_file_returns_none(self, tmp_path: Path):
        assert read_signature_file(tmp_path / "nonexistent.hmac") is None

    def test_read_strips_whitespace(self, tmp_path: Path):
        sig_path = tmp_path / "tasks.json.hmac"
        sig_path.write_text("  abc123  \n")
        assert read_signature_file(sig_path) == "abc123"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        sig_path = tmp_path / "deep" / "nested" / "tasks.json.hmac"
        write_signature_file(sig_path, "sig")
        assert sig_path.exists()
        assert sig_path.read_text() == "sig"


# ═══════════════════════════════════════════════════════════════════════════
# IntegrityError
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrityError:
    """Tests for the IntegrityError exception class."""

    def test_is_exception(self):
        assert issubclass(IntegrityError, Exception)

    def test_message(self):
        err = IntegrityError("tampered")
        assert str(err) == "tampered"


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler integration — signing on write
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerWriteSigning:
    """Tests for HMAC signing when tasks are persisted."""

    @pytest.mark.asyncio
    async def test_no_sig_file_without_secret(self, scheduler: TaskScheduler, workspace: Path):
        """When SCHEDULER_HMAC_SECRET is unset, no .hmac file is created."""
        await scheduler.add_task("chat1", _make_task(prompt="no secret"))
        sig_path = _sig_file(workspace, "chat1")
        assert not sig_path.exists()
        # tasks.json should still be written normally
        assert _tasks_file(workspace, "chat1").exists()

    @pytest.mark.asyncio
    async def test_sig_file_created_with_secret(self, scheduler: TaskScheduler, workspace: Path):
        """When SCHEDULER_HMAC_SECRET is set, a .hmac file is created."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        await scheduler.add_task("chat1", _make_task(prompt="with secret"))
        sig_path = _sig_file(workspace, "chat1")
        assert sig_path.exists()
        sig = sig_path.read_text().strip()
        assert len(sig) == 64

    @pytest.mark.asyncio
    async def test_sig_matches_payload(self, scheduler: TaskScheduler, workspace: Path):
        """The .hmac signature matches the tasks.json content."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        await scheduler.add_task("chat1", _make_task(prompt="verify me"))
        tasks_path = _tasks_file(workspace, "chat1")
        sig_path = _sig_file(workspace, "chat1")

        content = tasks_path.read_text()
        sig = sig_path.read_text().strip()
        assert verify_payload(SECRET, content.encode("utf-8"), sig)

    @pytest.mark.asyncio
    async def test_sig_updated_on_removal(self, scheduler: TaskScheduler, workspace: Path):
        """Signature is refreshed when a task is removed."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        tid = await scheduler.add_task("chat1", _make_task(prompt="first"))
        sig_before = _sig_file(workspace, "chat1").read_text().strip()

        await scheduler.remove_task_async("chat1", tid)
        sig_after = _sig_file(workspace, "chat1").read_text().strip()

        # Different content → different signature
        assert sig_before != sig_after

    @pytest.mark.asyncio
    async def test_sig_after_execution_persist(
        self,
        scheduler: TaskScheduler,
        workspace: Path,
        on_trigger: AsyncMock,
    ):
        """Signature is updated after task execution + persist."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        on_trigger.return_value = "done"
        await scheduler.add_task("chat1", _make_task(prompt="exec me"))
        task = scheduler.list_tasks("chat1")[0]
        task["task_id"] = "task_001"

        sig_before = _sig_file(workspace, "chat1").read_text().strip()
        await scheduler._execute_task("chat1", task)
        await scheduler._persist("chat1")
        sig_after = _sig_file(workspace, "chat1").read_text().strip()

        # last_run/last_result changed → new signature
        assert sig_before != sig_after

        # Verify the new signature is valid
        content = _tasks_file(workspace, "chat1").read_text()
        assert verify_payload(SECRET, content.encode("utf-8"), sig_after)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler integration — verification on load
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerLoadVerification:
    """Tests for HMAC verification when tasks are loaded from disk."""

    @pytest.mark.asyncio
    async def test_load_without_secret_succeeds(
        self,
        scheduler: TaskScheduler,
        workspace: Path,
    ):
        """Without secret, loading unsigned tasks works as before."""
        await scheduler.add_task("chat1", _make_task(prompt="legacy"))
        # New scheduler, no secret
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "legacy"

    @pytest.mark.asyncio
    async def test_load_with_valid_sig_succeeds(self, workspace: Path):
        """With secret set, loading signed tasks succeeds."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="signed task"))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "signed task"

    @pytest.mark.asyncio
    async def test_load_rejects_tampered_content(self, workspace: Path):
        """Tampering with tasks.json causes load to reject the file."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="original"))

        # Tamper with the tasks.json content
        tasks_path = _tasks_file(workspace, "chat1")
        data = json.loads(tasks_path.read_text())
        data[0]["prompt"] = "INJECTED MALICIOUS PROMPT"
        tasks_path.write_text(json.dumps(data, indent=2))

        # Try to load — should be rejected
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert tasks == []

    @pytest.mark.asyncio
    async def test_load_rejects_missing_sig_file(self, workspace: Path):
        """Missing .hmac file causes load to reject when secret is set."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="has sig"))

        # Delete the signature file
        sig_path = _sig_file(workspace, "chat1")
        assert sig_path.exists()
        sig_path.unlink()

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert tasks == []

    @pytest.mark.asyncio
    async def test_load_rejects_corrupted_sig(self, workspace: Path):
        """Corrupted HMAC signature causes load to reject."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="original"))

        # Corrupt the signature
        sig_path = _sig_file(workspace, "chat1")
        sig_path.write_text("0" * 64)

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        tasks = s2.list_tasks("chat1")
        assert tasks == []

    @pytest.mark.asyncio
    async def test_load_all_verifies_each_chat(self, workspace: Path):
        """load_all verifies HMAC per chat and skips tampered ones."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chatA", _make_task(prompt="valid A"))
        await s1.add_task("chatB", _make_task(prompt="valid B"))

        # Tamper chatA
        path_a = _tasks_file(workspace, "chatA")
        data = json.loads(path_a.read_text())
        data[0]["prompt"] = "TAMPERED"
        path_a.write_text(json.dumps(data, indent=2))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2.load_all()

        # chatA rejected, chatB accepted
        assert s2.list_tasks("chatA") == []
        assert len(s2.list_tasks("chatB")) == 1
        assert s2.list_tasks("chatB")[0]["prompt"] == "valid B"

    @pytest.mark.asyncio
    async def test_full_round_trip_with_secret(self, workspace: Path):
        """Full add → persist → load round trip with HMAC enabled."""
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET

        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="round trip"))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")

        tasks = s2.list_tasks("chat1")
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "round trip"

    @pytest.mark.asyncio
    async def test_secret_rotation_invalidates_old_sigs(self, workspace: Path):
        """Changing the secret invalidates previously signed files."""
        # Write with secret A
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = "secret-alpha"
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="signed with alpha"))

        # Rotate to secret B
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = "secret-beta"
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")

        # Old signature no longer valid
        assert s2.list_tasks("chat1") == []


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler — backward compatibility
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerBackwardCompat:
    """Tests for backward compatibility when HMAC is not configured."""

    @pytest.mark.asyncio
    async def test_unsigned_files_load_fine_without_secret(self, workspace: Path):
        """Files written without HMAC load fine when secret is unset."""
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="unsigned"))

        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        assert len(s2.list_tasks("chat1")) == 1

    @pytest.mark.asyncio
    async def test_unsigned_files_rejected_when_secret_enabled(self, workspace: Path):
        """Pre-existing unsigned files are rejected when secret is later enabled."""
        # Write without secret
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="old unsigned"))

        # Enable secret and try to load
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        assert s2.list_tasks("chat1") == []

    @pytest.mark.asyncio
    async def test_enabling_secret_then_rewriting_fixes_files(self, workspace: Path):
        """Enabling secret then re-saving the file produces valid signatures."""
        s1 = TaskScheduler()
        s1.configure(workspace=workspace, on_trigger=AsyncMock())
        await s1.add_task("chat1", _make_task(prompt="will be re-signed"))

        # Now enable the secret and re-persist
        os.environ[SCHEDULER_HMAC_SECRET_ENV] = SECRET
        await s1._persist("chat1")

        # Verify the re-signed file loads
        s2 = TaskScheduler()
        s2.configure(workspace=workspace, on_trigger=AsyncMock())
        await s2._load("chat1")
        assert len(s2.list_tasks("chat1")) == 1
