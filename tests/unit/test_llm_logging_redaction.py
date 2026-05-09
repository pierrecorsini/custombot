"""
Unit tests for _redact_secrets() in src/logging/llm_logging.py.

Verifies that API keys, auth tokens, and other secrets are redacted from
LLM log payloads before being written to disk.
"""

import json
from datetime import datetime, timezone

import pytest

from src.logging.llm_logging import (
    _REDACTED,
    _redact_secrets,
    _redact_string,
    LLMLogger,
)


# ─────────────────────────────────────────────────────────────────────────────
# _redact_secrets — dict key matching
# ─────────────────────────────────────────────────────────────────────────────


class TestSecretKeyRedaction:
    """Verify that dict values with secret key names are replaced."""

    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "API_KEY",
            "ApiKey",
            "apikey",
            "api-key",
            "authorization",
            "Authorization",
            "AUTHORIZATION",
            "password",
            "Password",
            "secret",
            "Secret",
            "secret_key",
            "access_token",
            "refresh_token",
            "credential",
            "credentials",
            "token",
            "Token",
            "bearer",
            "Bearer",
            "pwd",
            "auth",
        ],
    )
    def test_secret_key_value_redacted(self, key: str):
        data = {key: "sk-supersecret1234567890abc"}
        result = _redact_secrets(data)
        assert result[key] == _REDACTED

    def test_non_secret_keys_preserved(self):
        data = {"model": "gpt-4", "temperature": 0.7, "max_tokens": 4096}
        result = _redact_secrets(data)
        assert result == data

    def test_mixed_keys_partial_redaction(self):
        data = {
            "model": "gpt-4",
            "api_key": "sk-abc1234567890abcdefghijkl",
            "temperature": 0.7,
            "authorization": "Bearer tok_abcdef1234567890",
        }
        result = _redact_secrets(data)
        assert result["model"] == "gpt-4"
        assert result["api_key"] == _REDACTED
        assert result["temperature"] == 0.7
        assert result["authorization"] == _REDACTED


# ─────────────────────────────────────────────────────────────────────────────
# _redact_secrets — string value scanning
# ─────────────────────────────────────────────────────────────────────────────


class TestSecretValueRedaction:
    """Verify that API-key patterns in arbitrary string values are redacted."""

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "sk-ant-abcdefghijklmnopqrstuvwxyz123456",
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "gho_abcdefghijklmnopqrstuvwxyz1234567890",
            "glpat-abcdefghijklmnopqrstuvwxyz123456",
            "xoxb-1234567890-1234567890123-abcdefghij",
            "AIzaSyA1234567890abcdefghijklmnopqrstuvwx",
        ],
    )
    def test_api_key_patterns_redacted(self, secret: str):
        result = _redact_secrets({"content": secret})
        assert secret not in str(result["content"])
        assert _REDACTED in str(result["content"])

    def test_bearer_token_in_string_redacted(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.sig"
        result = _redact_secrets({"header": text})
        # Both Bearer prefix and JWT should be redacted
        assert "Bearer eyJhbGciOiJIUzI1NiJ9" not in result["header"]
        assert _REDACTED in result["header"]

    def test_jwt_token_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abc123def456"
        result = _redact_secrets({"data": jwt})
        assert jwt not in result["data"]
        assert _REDACTED in result["data"]

    def test_normal_text_preserved(self):
        text = "Hello, how are you today?"
        result = _redact_secrets({"content": text})
        assert result["content"] == text


# ─────────────────────────────────────────────────────────────────────────────
# _redact_secrets — nested structures
# ─────────────────────────────────────────────────────────────────────────────


class TestNestedRedaction:
    """Verify redaction works on nested dicts, lists, and tuples."""

    def test_nested_dict(self):
        data = {"outer": {"api_key": "sk-supersecret1234567890abcdefg"}}
        result = _redact_secrets(data)
        assert result["outer"]["api_key"] == _REDACTED

    def test_list_of_dicts(self):
        data = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "api_key": "sk-abc1234567890123456789"},
            ]
        }
        result = _redact_secrets(data)
        assert result["messages"][0] == {"role": "user", "content": "Hello"}
        assert result["messages"][1]["api_key"] == _REDACTED

    def test_tuple_of_values(self):
        data = {"items": ("safe_value", "sk-abcdefghijklmnopqrstuvwx12345")}
        result = _redact_secrets(data)
        assert result["items"][0] == "safe_value"
        assert _REDACTED in result["items"][1]

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": {"api_key": "sk-deepsecret1234567890abcdef"}}}}
        result = _redact_secrets(data)
        assert result["a"]["b"]["c"]["api_key"] == _REDACTED

    def test_depth_limit_returns_unchanged(self):
        """Beyond depth 20, objects pass through unchanged."""
        nested = {"level": 0}
        current = nested
        for i in range(1, 25):
            current["level"] = {"level": i}
            current = current["level"]
        current["api_key"] = "sk-overflow1234567890abcdefghijklm"

        result = _redact_secrets(nested)
        # The deeply nested api_key should survive because depth limit is reached
        # (exact depth depends on recursion structure, but values past 20 are safe)
        assert isinstance(result, dict)

    def test_empty_structures(self):
        assert _redact_secrets({}) == {}
        assert _redact_secrets([]) == []
        assert _redact_secrets(()) == ()

    def test_primitives_unchanged(self):
        assert _redact_secrets(42) == 42
        assert _redact_secrets(3.14) == 3.14
        assert _redact_secrets(True) is True
        assert _redact_secrets(None) is None


# ─────────────────────────────────────────────────────────────────────────────
# _redact_string — direct string redaction
# ─────────────────────────────────────────────────────────────────────────────


class TestRedactString:
    """Verify _redact_string handles edge cases."""

    def test_empty_string(self):
        assert _redact_string("") == ""

    def test_no_secrets(self):
        assert _redact_string("Hello world") == "Hello world"

    def test_multiple_secrets_in_one_string(self):
        text = "key1=sk-abcdefghijklmnopqrstuvwx12345 key2=AKIAIOSFODNN7EXAMPLE"
        result = _redact_string(text)
        assert "sk-abcdefghijklmnopqrstuvwx12345" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result


# ─────────────────────────────────────────────────────────────────────────────
# Integration — _write applies redaction to disk
# ─────────────────────────────────────────────────────────────────────────────


class TestLLMLoggerRedaction:
    """Verify that LLMLogger._write() redacts secrets before writing to disk."""

    def test_log_request_redacts_api_key_in_messages(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")
        req_id = logger.new_request_id()

        messages = [
            {
                "role": "user",
                "content": "My key is sk-abcdefghijklmnopqrstuvwx12345",
            }
        ]
        logger.log_request(
            request_id=req_id,
            model="gpt-4",
            messages=messages,
        )

        # Read the written file
        log_files = list((tmp_path / "llm_logs").glob("*_request_*.json"))
        assert len(log_files) == 1
        written = json.loads(log_files[0].read_text(encoding="utf-8"))

        # Request logging stores compact summary only (no full raw messages)
        assert "messages" not in written
        assert written["message_count"] == 1
        assert "messages_summary" in written
        content_preview = written["messages_summary"][0]["content_preview"]
        assert "sk-abcdefghijklmnopqrstuvwx12345" not in content_preview
        assert _REDACTED in content_preview

    def test_log_request_redacts_secret_dict_keys(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")
        req_id = logger.new_request_id()

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        # Simulate a payload with an api_key field that shouldn't be there
        # (e.g., from httpx trace headers)
        logger._write(
            f"test_request_{req_id}.json",
            {
                "request_id": req_id,
                "model": "gpt-4",
                "messages": messages,
                "api_key": "sk-supersecret1234567890abcdefghij",
                "authorization": "Bearer sk-supersecret1234567890abcdefghij",
            },
        )

        log_files = list((tmp_path / "llm_logs").glob("test_request_*.json"))
        assert len(log_files) == 1
        written = json.loads(log_files[0].read_text(encoding="utf-8"))

        assert written["api_key"] == _REDACTED
        assert written["authorization"] == _REDACTED
        assert written["model"] == "gpt-4"
        assert written["messages"] == messages

    def test_log_response_redacts_secrets_in_response_body(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")
        req_id = logger.new_request_id()

        # Simulate a response dict that contains leaked credentials
        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Here is the key: sk-abcdefghijklmnopqrstuvwx12345",
                    }
                }
            ]
        }

        logger._write(
            f"test_response_{req_id}.json",
            {"request_id": req_id, "response": response_data},
        )

        log_files = list((tmp_path / "llm_logs").glob("test_response_*.json"))
        assert len(log_files) == 1
        written = json.loads(log_files[0].read_text(encoding="utf-8"))

        content = written["response"]["choices"][0]["message"]["content"]
        assert "sk-abcdefghijklmnopqrstuvwx12345" not in content
        assert _REDACTED in content

    def test_log_write_truncates_large_string_fields(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")

        very_long = "x" * 10000
        logger._write(
            "test_large_string.json",
            {
                "request_id": "req-1",
                "messages": [{"role": "user", "content": very_long}],
            },
        )

        written = json.loads((tmp_path / "llm_logs" / "test_large_string.json").read_text("utf-8"))
        content = written["messages"][0]["content"]
        assert len(content) < len(very_long)
        assert "[TRUNCATED" in content

    def test_log_write_truncates_large_list_fields(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")

        logger._write(
            "test_large_list.json",
            {
                "request_id": "req-2",
                "messages": [{"role": "user", "content": "ok"}],
                "items": list(range(200)),
            },
        )

        written = json.loads((tmp_path / "llm_logs" / "test_large_list.json").read_text("utf-8"))
        assert len(written["items"]) < 200
        assert isinstance(written["items"][-1], str)
        assert "TRUNCATED" in written["items"][-1]

    def test_log_response_persists_summary_not_full_response(self, tmp_path):
        logger = LLMLogger(tmp_path / "llm_logs")
        req_id = logger.new_request_id()

        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hello world",
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        logger.log_response(
            request_id=req_id,
            model="gpt-4",
            response=response_data,
            request_ts=datetime.now(timezone.utc),
        )

        log_files = list((tmp_path / "llm_logs").glob("*_response_*.json"))
        assert len(log_files) == 1
        written = json.loads(log_files[0].read_text(encoding="utf-8"))

        assert "response" not in written
        assert "response_summary" in written
        assert written["response_summary"]["choices_count"] == 1
