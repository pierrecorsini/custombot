"""
utils/phone.py — Shared phone-number normalization utility.

Provides ``normalize_phone`` for use by any channel implementation
(WhatsApp, Telegram, Discord, etc.) and the NeonizeBackend.
"""

from __future__ import annotations


def normalize_phone(number: str) -> str:
    """Normalize a phone number for comparison by stripping formatting.

    Handles: ``+`` prefix, ``00`` international prefix, ``0`` national trunk
    prefix, spaces, dashes, and parentheses.
    """
    digits = "".join(c for c in number if c.isdigit())
    if digits.startswith("00"):
        return digits[2:]
    if digits.startswith("0"):
        return digits[1:]
    return digits
