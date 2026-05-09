"""
test_security_subsystem.py — Tests for the security subsystem modules.

Covers:
  - sandbox.py: ResourceSandboxConfig, path validation, subprocess limits
  - signing.py: sign_payload, verify_payload, get_scheduler_secret
  - encryption.py: ConversationEncryptor encrypt/decrypt round-trip
  - log_redaction.py: PIIRedactingFilter pattern matching
  - url_sanitizer.py: sanitize_url_for_logging
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.security.sandbox import ResourceSandboxConfig, ResourceSandbox
from src.security.signing import (
    sign_payload,
    verify_payload,
)
import src.security.signing as _signing_module
from src.security.encryption import ConversationEncryptor, EncryptionError
from src.security.log_redaction import PIIRedactingFilter, REDACTED
from src.security.url_sanitizer import sanitize_url_for_logging


# ── sandbox.py ──────────────────────────────────────────────────────────────


class TestResourceSandboxConfig:
    """Tests for ResourceSandboxConfig dataclass."""

    def test_defaults(self):
        cfg = ResourceSandboxConfig()
        assert cfg.max_cpu_seconds == 30.0
        assert cfg.max_memory_mb == 512
        assert cfg.allowed_paths == ()
        assert cfg.max_output_bytes == 1_048_576

    def test_custom_values(self):
        cfg = ResourceSandboxConfig(
            max_cpu_seconds=60.0,
            max_memory_mb=1024,
            allowed_paths=("/tmp", "/var"),
            max_output_bytes=2_097_152,
        )
        assert cfg.max_cpu_seconds == 60.0
        assert cfg.max_memory_mb == 1024
        assert cfg.allowed_paths == ("/tmp", "/var")

    def test_frozen(self):
        cfg = ResourceSandboxConfig()
        with pytest.raises(AttributeError):
            cfg.max_cpu_seconds = 99  # type: ignore[misc]


class TestResourceSandbox:
    """Tests for ResourceSandbox execution."""

    def test_init_with_config(self):
        cfg = ResourceSandboxConfig(max_cpu_seconds=10.0)
        sandbox = ResourceSandbox(cfg)
        assert sandbox._config is cfg

    def test_init_with_default_config(self):
        sandbox = ResourceSandbox()
        assert sandbox._config.max_cpu_seconds == 30.0

    def test_config_property(self):
        cfg = ResourceSandboxConfig(max_cpu_seconds=60.0)
        sandbox = ResourceSandbox(cfg)
        assert sandbox.config is cfg

    def test_validate_path_empty_whitelist_allows_all(self):
        sandbox = ResourceSandbox(ResourceSandboxConfig(allowed_paths=()))
        assert sandbox.validate_path("/any/path") is True

    def test_validate_path_whitelist_allows_allowed(self, tmp_path: Path) -> None:
        sandbox = ResourceSandbox(ResourceSandboxConfig(allowed_paths=(str(tmp_path),)))
        test_file = tmp_path / "sub" / "file.txt"
        assert sandbox.validate_path(test_file) is True

    def test_validate_path_whitelist_blocks_disallowed(self, tmp_path: Path) -> None:
        sandbox = ResourceSandbox(ResourceSandboxConfig(allowed_paths=(str(tmp_path),)))
        assert sandbox.validate_path("/etc/passwd") is False


# ── signing.py ──────────────────────────────────────────────────────────────


class TestSigning:
    """Tests for HMAC signing and verification."""

    def _reset_secret_cache(self):
        """Reset the cached secret so tests don't pollute each other."""
        _signing_module._cached_secret = _signing_module._SENTINEL

    def test_sign_and_verify_round_trip(self):
        secret = "test-secret-key"
        payload = b'{"task": "backup", "prompt": "hello"}'
        sig = sign_payload(secret, payload)
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex digest
        assert verify_payload(secret, payload, sig) is True

    def test_verify_fails_with_wrong_secret(self):
        sig = sign_payload("secret-a", b"data")
        assert verify_payload("secret-b", b"data", sig) is False

    def test_verify_fails_with_tampered_payload(self):
        sig = sign_payload("secret", b"original")
        assert verify_payload("secret", b"tampered", sig) is False

    def test_verify_fails_with_wrong_signature(self):
        assert verify_payload("secret", b"data", "0" * 64) is False

    def test_get_scheduler_secret_from_env(self):
        self._reset_secret_cache()
        with patch.dict(os.environ, {"SCHEDULER_HMAC_SECRET": "my-secret"}):
            result = _signing_module.get_scheduler_secret()
            assert result == "my-secret"
        self._reset_secret_cache()

    def test_get_scheduler_secret_returns_none_when_unset(self):
        self._reset_secret_cache()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("SCHEDULER_HMAC_SECRET", None)
            result = _signing_module.get_scheduler_secret()
            assert result is None
        self._reset_secret_cache()


# ── encryption.py ───────────────────────────────────────────────────────────


class TestConversationEncryptor:
    """Tests for ConversationEncryptor."""

    def test_disabled_by_default(self):
        enc = ConversationEncryptor()
        assert enc.enabled is False

    def test_passthrough_when_disabled(self):
        enc = ConversationEncryptor()
        data = b"hello world"
        assert enc.encrypt(data) == data
        assert enc.decrypt(data) == data

    def test_enabled_with_key(self):
        enc = ConversationEncryptor(key="test-key-12345")
        assert enc.enabled is True

    def test_encrypt_decrypt_round_trip(self):
        enc = ConversationEncryptor(key="test-key-12345")
        plaintext = b"Hello, this is a secret message!"
        encrypted = enc.encrypt(plaintext)
        assert encrypted != plaintext
        decrypted = enc.decrypt(encrypted)
        assert decrypted == plaintext

    def test_different_encryptions_differ(self):
        """Each encryption uses a random salt/nonce, so outputs differ."""
        enc = ConversationEncryptor(key="test-key-12345")
        data = b"same input"
        enc1 = enc.encrypt(data)
        enc2 = enc.encrypt(data)
        assert enc1 != enc2  # different salt/nonce

    def test_decrypt_with_wrong_key_fails(self):
        enc1 = ConversationEncryptor(key="key-a")
        enc2 = ConversationEncryptor(key="key-b-different")
        encrypted = enc1.encrypt(b"secret")
        with pytest.raises(EncryptionError):
            enc2.decrypt(encrypted)

    def test_decrypt_garbage_raises(self):
        enc = ConversationEncryptor(key="test-key-12345")
        with pytest.raises(EncryptionError):
            enc.decrypt(b"not-valid-encrypted-data-at-all")

    def test_encrypt_empty_data(self):
        enc = ConversationEncryptor(key="test-key-12345")
        encrypted = enc.encrypt(b"")
        decrypted = enc.decrypt(encrypted)
        assert decrypted == b""


# ── log_redaction.py ────────────────────────────────────────────────────────


class TestPIIRedactingFilter:
    """Tests for PII redaction in log messages."""

    def test_redacts_phone_number(self):
        f = PIIRedactingFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "Call +1234567890", (), None)
        f.filter(record)
        assert "+1234567890" not in record.getMessage()
        assert REDACTED in record.getMessage()

    def test_redacts_openai_api_key(self):
        f = PIIRedactingFilter()
        key = "sk-abcdefghijklmnopqrstuvwx"
        record = logging.LogRecord("test", logging.INFO, "", 0, f"Key: {key}", (), None)
        f.filter(record)
        assert key not in record.getMessage()
        assert REDACTED in record.getMessage()

    def test_redacts_email(self):
        f = PIIRedactingFilter()
        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "Contact: user@example.com", (), None
        )
        f.filter(record)
        assert "user@example.com" not in record.getMessage()

    def test_preserves_safe_text(self):
        f = PIIRedactingFilter()
        msg = "All systems operational"
        record = logging.LogRecord("test", logging.INFO, "", 0, msg, (), None)
        f.filter(record)
        assert record.getMessage() == msg


# ── url_sanitizer.py ────────────────────────────────────────────────────────


class TestUrlSanitizer:
    """Tests for URL sanitization before logging."""

    def test_strips_query_params(self):
        assert sanitize_url_for_logging("https://api.openai.com/v1?key=secret") == (
            "https://api.openai.com/v1"
        )

    def test_strips_fragment(self):
        assert sanitize_url_for_logging("http://localhost:11434/v1#frag") == (
            "http://localhost:11434/v1"
        )

    def test_none_returns_not_set(self):
        assert sanitize_url_for_logging(None) == "<not set>"

    def test_empty_returns_not_set(self):
        assert sanitize_url_for_logging("") == "<not set>"

    def test_simple_url_unchanged(self):
        assert sanitize_url_for_logging("https://api.openai.com/v1") == (
            "https://api.openai.com/v1"
        )

    def test_url_with_port(self):
        assert sanitize_url_for_logging("http://localhost:8080/api?token=x") == (
            "http://localhost:8080/api"
        )

    def test_preserves_path(self):
        assert sanitize_url_for_logging("https://example.com/a/b/c") == (
            "https://example.com/a/b/c"
        )
