"""
src/security/signing.py — HMAC-SHA256 payload signing for scheduler integrity.

Provides signing and verification utilities to protect scheduler task files
against tampering.  When ``SCHEDULER_HMAC_SECRET`` is set, the scheduler
writes an HMAC-SHA256 signature alongside each ``tasks.json`` file and
verifies it on load — rejecting files whose content has been modified
outside the application.

Usage::

    from src.security.signing import sign_payload, verify_payload

    sig = sign_payload(secret, json_bytes)
    if not verify_payload(secret, json_bytes, sig):
        raise IntegrityError("tasks.json has been tampered with")

The secret is loaded from the ``SCHEDULER_HMAC_SECRET`` environment variable.
If the variable is unset or empty, signing and verification are no-ops
(backward-compatible mode).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Final

from src.exceptions import CustomBotException
from src.utils.async_file import sync_atomic_write

log = logging.getLogger(__name__)

# Environment variable name for the scheduler HMAC secret.
SCHEDULER_HMAC_SECRET_ENV: Final = "SCHEDULER_HMAC_SECRET"


class _Sentinel:
    """Sentinel to distinguish 'not yet read' from 'cached None'."""


_SENTINEL = _Sentinel()
_cached_secret: str | None | _Sentinel = _SENTINEL


class IntegrityError(CustomBotException):
    """Raised when payload HMAC verification fails."""

    default_message = "Payload integrity verification failed"


def get_scheduler_secret() -> str | None:
    """Load the optional HMAC secret from the environment (cached).

    Returns ``None`` when signing is disabled (variable unset or empty).
    The value is read from ``os.environ`` once and cached at module level
    for the lifetime of the process.
    """
    global _cached_secret
    if isinstance(_cached_secret, _Sentinel):
        secret = os.environ.get(SCHEDULER_HMAC_SECRET_ENV, "").strip()
        _cached_secret = secret if secret else None
    return _cached_secret  # type: ignore[return-value]


def sign_payload(secret: str, payload: bytes) -> str:
    """Compute an HMAC-SHA256 hex digest over *payload* using *secret*.

    Args:
        secret: UTF-8 secret key.
        payload: Raw bytes to sign (e.g. JSON-encoded task data).

    Returns:
        64-character lowercase hex string.
    """
    return hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256,
    ).hexdigest()


def verify_payload(secret: str, payload: bytes, signature: str) -> bool:
    """Verify an HMAC-SHA256 *signature* over *payload*.

    Uses ``hmac.compare_digest`` for timing-safe comparison.
    Both the expected and provided signatures are always 64-char hex
    strings (SHA-256), so no additional padding is needed.

    Args:
        secret: UTF-8 secret key.
        payload: Raw bytes that were signed.
        signature: Claimed hex digest to verify.

    Returns:
        ``True`` if the signature is valid, ``False`` otherwise.
    """
    expected = sign_payload(secret, payload)

    if not hmac.compare_digest(expected, signature):
        log.warning("Scheduler HMAC verification failed: invalid signature")
        return False

    return True


def read_signature_file(sig_path: str | Path) -> str | None:
    """Read an HMAC signature from a sidecar ``.hmac`` file.

    Args:
        sig_path: Path to the signature file (``Path`` or str).

    Returns:
        The stripped signature string, or ``None`` if the file does not
        exist or cannot be read.
    """
    path = Path(sig_path) if not isinstance(sig_path, Path) else sig_path
    if not path.exists():
        return None
    try:
        return path.read_text().strip()
    except OSError as exc:
        log.warning("Failed to read HMAC signature file %s: %s", path, exc)
        return None


def write_signature_file(sig_path: str | Path, signature: str) -> None:
    """Write an HMAC signature to a sidecar ``.hmac`` file.

    Args:
        sig_path: Path to the signature file (``Path`` or str).
        signature: The hex digest to persist.
    """
    path = Path(sig_path) if not isinstance(sig_path, Path) else sig_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sync_atomic_write(path, signature)
    except OSError as exc:
        log.error("Failed to write HMAC signature file %s: %s", path, exc)
