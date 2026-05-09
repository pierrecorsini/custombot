"""
src/security/url_sanitizer.py — Strip sensitive data from URLs before logging.

Some LLM providers embed API keys or tokens as query parameters in the
``base_url`` (e.g. ``http://localhost:11434/v1?key=secret``).  This module
provides a small helper that removes query strings and fragments so that
credential material never appears in log output.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from src.core.errors import NonCriticalCategory, log_noncritical

log = logging.getLogger(__name__)


def sanitize_url_for_logging(url: str | None) -> str:
    """Return *url* with query parameters and fragment stripped.

    Only the ``scheme://host[:port]/path`` portion is preserved.  If *url*
    is ``None`` or empty, ``"<not set>"`` is returned so that log lines
    remain readable.

    Examples::

        >>> sanitize_url_for_logging("https://api.openai.com/v1?key=secret")
        'https://api.openai.com/v1'
        >>> sanitize_url_for_logging("http://localhost:11434/v1#frag")
        'http://localhost:11434/v1'
        >>> sanitize_url_for_logging(None)
        '<not set>'
    """
    if not url:
        return "<not set>"

    try:
        parsed = urlparse(url)
        # Rebuild with empty query and fragment
        sanitized = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",  # params
                "",  # query  ← this is where API keys can hide
                "",  # fragment
            )
        )
        return sanitized
    except Exception:
        # If parsing fails for any reason, return a safe placeholder
        log_noncritical(
            NonCriticalCategory.URL_PARSING,
            "Failed to sanitize URL for logging",
            logger=log,
            extra={"url_length": len(url)},
        )
        return "<invalid-url>"
