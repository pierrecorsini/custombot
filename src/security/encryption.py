"""
src/security/encryption.py — At-rest encryption for stored conversations.

Provides optional authenticated encryption for JSONL conversation files
using stdlib-only primitives (hashlib + hmac + os).  When the environment
variable ``CONVERSATION_ENCRYPTION_KEY`` is set, all JSONL writes are
encrypted and reads are decrypted transparently.  Without the key,
behaviour is unchanged (plaintext).

Encryption scheme:
  1. Key derivation: PBKDF2-HMAC-SHA256 (600 000 iterations, random salt)
  2. Stream cipher: SHA-256 counter mode (keyed hash as keystream)
  3. Authentication: HMAC-SHA256 over ciphertext + salt + nonce

Wire format (bytes):
    [salt:32][nonce:16][hmac:32][ciphertext:...]

Usage:
    from src.security.encryption import ConversationEncryptor

    enc = ConversationEncryptor()
    encrypted = enc.encrypt(b"hello")
    decrypted = enc.decrypt(encrypted)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# PBKDF2 iterations (OWASP 2023 recommendation for SHA-256)
_PBKDF2_ITERATIONS = 600_000

# Byte sizes
_SALT_SIZE = 32
_NONCE_SIZE = 16
_HMAC_SIZE = 32
_KEY_SIZE = 32  # derived encryption key length
_MAC_KEY_SIZE = 32  # derived MAC key length


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


class ConversationEncryptor:
    """Authenticated encryption for conversation data.

    When initialised with a non-empty *key*, :meth:`encrypt` and
    :meth:`decrypt` apply full authenticated encryption.  With an empty
    key, both methods are identity passthrough (no encryption).
    """

    def __init__(self, key: Optional[str] = None) -> None:
        self._enabled = bool(key)
        if self._enabled:
            assert key is not None  # for type narrowing
            self._raw_key = key.encode("utf-8")
            log.info("Conversation encryption enabled")
        else:
            self._raw_key = b""
            log.debug("Conversation encryption disabled (no key)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt *data* if a key is configured, otherwise return as-is."""
        if not self._enabled:
            return data
        return self._encrypt_impl(data)

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt *data* if a key is configured, otherwise return as-is."""
        if not self._enabled:
            return data
        return self._decrypt_impl(data)

    def _derive_keys(self, salt: bytes) -> tuple[bytes, bytes]:
        """Derive encryption key and MAC key from password + salt."""
        material = hashlib.pbkdf2_hmac(
            "sha256",
            self._raw_key,
            salt,
            _PBKDF2_ITERATIONS,
            dklen=_KEY_SIZE + _MAC_KEY_SIZE,
        )
        return material[:_KEY_SIZE], material[_KEY_SIZE:]

    def _encrypt_impl(self, plaintext: bytes) -> bytes:
        salt = os.urandom(_SALT_SIZE)
        nonce = os.urandom(_NONCE_SIZE)
        enc_key, mac_key = self._derive_keys(salt)

        # Generate keystream via SHA-256 counter mode
        ciphertext = bytearray(len(plaintext))
        counter = 0
        offset = 0
        while offset < len(plaintext):
            block_input = enc_key + nonce + counter.to_bytes(8, "big")
            block = hashlib.sha256(block_input).digest()
            chunk_size = min(32, len(plaintext) - offset)
            for i in range(chunk_size):
                ciphertext[offset + i] = plaintext[offset + i] ^ block[i]
            offset += chunk_size
            counter += 1

        ct_bytes = bytes(ciphertext)

        # HMAC over salt + nonce + ciphertext
        mac = hmac.new(mac_key, salt + nonce + ct_bytes, hashlib.sha256).digest()

        return salt + nonce + mac + ct_bytes

    def _decrypt_impl(self, data: bytes) -> bytes:
        min_size = _SALT_SIZE + _NONCE_SIZE + _HMAC_SIZE
        if len(data) < min_size:
            raise EncryptionError(
                f"Ciphertext too short ({len(data)} bytes, need ≥{min_size})"
            )

        salt = data[:_SALT_SIZE]
        nonce = data[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
        stored_mac = data[_SALT_SIZE + _NONCE_SIZE : _SALT_SIZE + _NONCE_SIZE + _HMAC_SIZE]
        ciphertext = data[_SALT_SIZE + _NONCE_SIZE + _HMAC_SIZE :]

        enc_key, mac_key = self._derive_keys(salt)

        # Verify HMAC before decryption
        expected_mac = hmac.new(mac_key, salt + nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(stored_mac, expected_mac):
            raise EncryptionError("HMAC verification failed — data tampered or wrong key")

        # Decrypt via SHA-256 counter mode
        plaintext = bytearray(len(ciphertext))
        counter = 0
        offset = 0
        while offset < len(ciphertext):
            block_input = enc_key + nonce + counter.to_bytes(8, "big")
            block = hashlib.sha256(block_input).digest()
            chunk_size = min(32, len(ciphertext) - offset)
            for i in range(chunk_size):
                plaintext[offset + i] = ciphertext[offset + i] ^ block[i]
            offset += chunk_size
            counter += 1

        return bytes(plaintext)


def get_encryption_key_from_env() -> Optional[str]:
    """Load the encryption key from the ``CONVERSATION_ENCRYPTION_KEY`` env var."""
    key = os.environ.get("CONVERSATION_ENCRYPTION_KEY", "").strip()
    return key if key else None


def create_encryptor() -> ConversationEncryptor:
    """Create a :class:`ConversationEncryptor` from the environment config."""
    key = get_encryption_key_from_env()
    return ConversationEncryptor(key=key)


__all__ = [
    "ConversationEncryptor",
    "EncryptionError",
    "create_encryptor",
    "get_encryption_key_from_env",
]
