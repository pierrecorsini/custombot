"""
Tests for src/security/url_sanitizer.py — URL sanitization for safe logging.

Covers:
- sanitize_url_for_logging: stripping query params and fragments
- Edge cases: None, empty, invalid URLs, URLs with credentials
"""

from __future__ import annotations

import pytest

from src.security.url_sanitizer import sanitize_url_for_logging


class TestSanitizeUrlForLogging:
    """Tests for sanitize_url_for_logging()."""

    def test_strips_query_params(self):
        result = sanitize_url_for_logging("https://api.openai.com/v1?key=secret")
        assert result == "https://api.openai.com/v1"
        assert "secret" not in result

    def test_strips_fragment(self):
        result = sanitize_url_for_logging("http://localhost:11434/v1#frag")
        assert result == "http://localhost:11434/v1"

    def test_strips_both_query_and_fragment(self):
        result = sanitize_url_for_logging("https://host/path?token=abc#section")
        assert result == "https://host/path"
        assert "token" not in result

    def test_none_returns_not_set(self):
        assert sanitize_url_for_logging(None) == "<not set>"

    def test_empty_string_returns_not_set(self):
        assert sanitize_url_for_logging("") == "<not set>"

    def test_whitespace_returns_not_set(self):
        assert sanitize_url_for_logging("   ") == "<not set>"

    def test_url_without_query_passes_through(self):
        url = "https://api.openai.com/v1/chat/completions"
        assert sanitize_url_for_logging(url) == url

    def test_preserves_port(self):
        result = sanitize_url_for_logging("http://localhost:8080/v1?key=x")
        assert result == "http://localhost:8080/v1"

    def test_preserves_path(self):
        result = sanitize_url_for_logging("https://host/a/b/c?k=v")
        assert result == "https://host/a/b/c"

    def test_strips_api_key_in_query(self):
        result = sanitize_url_for_logging("http://localhost:11434/v1?key=sk-abc123def456")
        assert "sk-abc123def456" not in result
        assert result == "http://localhost:11434/v1"

    def test_multiple_query_params(self):
        result = sanitize_url_for_logging("https://host/path?a=1&b=2&c=3")
        assert result == "https://host/path"

    def test_url_with_port_and_path(self):
        result = sanitize_url_for_logging("https://host:443/api/v1/models")
        assert result == "https://host:443/api/v1/models"

    def test_localhost_url(self):
        result = sanitize_url_for_logging("http://127.0.0.1:11434/v1")
        assert result == "http://127.0.0.1:11434/v1"

    def test_url_with_trailing_slash(self):
        result = sanitize_url_for_logging("https://host/path/?k=v")
        assert result == "https://host/path/"

    def test_non_http_scheme(self):
        result = sanitize_url_for_logging("ftp://files.example.com/data?user=admin")
        assert result == "ftp://files.example.com/data"
