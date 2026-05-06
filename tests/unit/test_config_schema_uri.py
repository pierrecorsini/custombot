"""
Tests for URI format validation in config_schema_defs.py.

Verifies that the hand-rolled validator enforces format: "uri" on base_url,
accepting valid URIs and rejecting malformed ones per RFC 3986.
"""

from __future__ import annotations

import pytest

from src.config.config_schema_defs import validate_config_dict


def _make_config(base_url: str) -> dict:
    """Return a minimal valid config dict with the given base_url."""
    return {
        "llm": {
            "model": "gpt-4o",
            "base_url": base_url,
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {"db_path": "/tmp/test.db"},
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Valid URIs — should pass without errors
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "https://api.openai.com/v1",
        "http://localhost:11434",
        "https://my-llm.example.com:8080/v1/chat",
        "http://127.0.0.1:4000",
        "https://user:pass@host.example.com/path",
    ],
)
def test_valid_uri_passes(uri: str) -> None:
    result = validate_config_dict(_make_config(uri))
    assert result["valid"], f"Expected '{uri}' to be valid, got errors: {result['errors']}"


# ─────────────────────────────────────────────────────────────────────────────
# Invalid URIs — should produce a validation error
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "not-a-uri",
        "missing_scheme.com",
        "://no-scheme.com",
        "just-text",
    ],
)
def test_invalid_uri_rejected(uri: str) -> None:
    result = validate_config_dict(_make_config(uri))
    assert not result["valid"], f"Expected '{uri}' to be rejected"
    uri_errors = [e for e in result["errors"] if "not a valid URI" in e["message"]]
    assert len(uri_errors) == 1, f"Expected exactly one URI error, got: {result['errors']}"


# ─────────────────────────────────────────────────────────────────────────────
# Empty string base_url — allowed (means "use provider default")
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_string_base_url_passes() -> None:
    result = validate_config_dict(_make_config(""))
    assert result["valid"], (
        f"Empty base_url should pass (means provider default), got: {result['errors']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# base_url is optional — omitting it should not trigger URI validation
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_base_url_is_ok() -> None:
    config = {
        "llm": {"model": "gpt-4o"},
        "whatsapp": {
            "provider": "neonize",
            "neonize": {"db_path": "/tmp/test.db"},
        },
    }
    result = validate_config_dict(config)
    assert result["valid"]


# ─────────────────────────────────────────────────────────────────────────────
# URI error path points to the right field
# ─────────────────────────────────────────────────────────────────────────────


def test_uri_error_path() -> None:
    result = validate_config_dict(_make_config("garbage"))
    assert not result["valid"]
    paths = [e["path"] for e in result["errors"] if "not a valid URI" in e["message"]]
    assert "llm.base_url" in paths
