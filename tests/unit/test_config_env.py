"""
Tests for src/config/config.py — Environment variable overrides.

Unit tests covering:
  - _apply_env_overrides(): OPENAI_API_KEY and OPENAI_BASE_URL env var handling
  - load_config() integration: env vars override file values
  - load_config() integration: env vars used when config file key is missing
  - Logging: env var usage is logged; api_key is redacted in effective config log
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
from src.config.config import _apply_env_overrides, load_config


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def base_config() -> Config:
    """Provide a minimally valid Config for unit tests of _apply_env_overrides."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-file-key",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path="/tmp/test.db"),
        ),
    )


@pytest.fixture
def valid_config_dict() -> dict:
    """Provide a valid config dict that passes schema validation."""
    return {
        "llm": {
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-file-key-12345",
            "temperature": 0.7,
            "timeout": 120,
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {"db_path": "/tmp/test.db"},
        },
    }


@pytest.fixture
def config_file(tmp_path: Path, valid_config_dict: dict) -> Path:
    """Write a valid config dict to a temp JSON file and return its path."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(valid_config_dict, indent=2), encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests: _apply_env_overrides()
# ─────────────────────────────────────────────────────────────────────────────


class TestApplyEnvOverrides:
    """Direct tests of _apply_env_overrides() with isolated Config objects."""

    def test_api_key_from_env(self, base_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_API_KEY env var overrides config.llm.api_key."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-override")
        _apply_env_overrides(base_config)
        assert base_config.llm.api_key == "sk-env-key-override"

    def test_base_url_from_env(self, base_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_BASE_URL env var overrides config.llm.base_url."""
        monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.api.local/v1")
        _apply_env_overrides(base_config)
        assert base_config.llm.base_url == "https://custom.api.local/v1"

    def test_both_env_vars_applied(self, base_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both env vars are applied simultaneously."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://env.api.local/v1")
        _apply_env_overrides(base_config)
        assert base_config.llm.api_key == "sk-env-key"
        assert base_config.llm.base_url == "https://env.api.local/v1"

    def test_no_env_vars_leaves_config_unchanged(self, base_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """When neither env var is set, config values remain unchanged."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        original_key = base_config.llm.api_key
        original_url = base_config.llm.base_url
        _apply_env_overrides(base_config)
        assert base_config.llm.api_key == original_key
        assert base_config.llm.base_url == original_url

    def test_empty_env_var_ignored(self, base_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty-string env var is falsy and does NOT override the config."""
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.setenv("OPENAI_BASE_URL", "")
        original_key = base_config.llm.api_key
        original_url = base_config.llm.base_url
        _apply_env_overrides(base_config)
        assert base_config.llm.api_key == original_key
        assert base_config.llm.base_url == original_url


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests: load_config() with env vars
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadConfigEnvOverrides:
    """Tests for load_config() ensuring env vars interact correctly with file values."""

    def test_env_var_overrides_file_value(
        self,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(a) OPENAI_API_KEY env var overrides the value in config.json."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-override-key")
        config = load_config(config_file)
        assert config.llm.api_key == "sk-env-override-key"

    def test_base_url_env_var_overrides_file_value(
        self,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(a) OPENAI_BASE_URL env var overrides the value in config.json."""
        monkeypatch.setenv("OPENAI_BASE_URL", "https://override.api.local/v1")
        config = load_config(config_file)
        assert config.llm.base_url == "https://override.api.local/v1"

    def test_env_var_used_when_file_key_missing(
        self,
        tmp_path: Path,
        valid_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(b) When config.json omits api_key, the env var is used instead."""
        # Remove api_key from the dict — the file won't contain it
        valid_config_dict["llm"].pop("api_key", None)
        config_path = tmp_path / "config_no_key.json"
        config_path.write_text(json.dumps(valid_config_dict, indent=2), encoding="utf-8")

        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env-only")
        config = load_config(config_path)
        assert config.llm.api_key == "sk-from-env-only"

    def test_base_url_env_var_used_when_file_key_missing(
        self,
        tmp_path: Path,
        valid_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(b) When config.json omits base_url, the env var is used instead."""
        valid_config_dict["llm"].pop("base_url", None)
        config_path = tmp_path / "config_no_url.json"
        config_path.write_text(json.dumps(valid_config_dict, indent=2), encoding="utf-8")

        monkeypatch.setenv("OPENAI_BASE_URL", "https://env-only.api.local/v1")
        config = load_config(config_path)
        assert config.llm.base_url == "https://env-only.api.local/v1"

    def test_env_var_used_when_minimal_config_file(
        self,
        tmp_path: Path,
        valid_config_dict: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(b) A config file with no api_key still gets env var override applied."""
        valid_config_dict["llm"].pop("api_key", None)
        config_path = tmp_path / "no_api_key.json"
        config_path.write_text(json.dumps(valid_config_dict, indent=2), encoding="utf-8")

        monkeypatch.setenv("OPENAI_API_KEY", "sk-minimal-env-key")
        config = load_config(config_path)
        assert config.llm.api_key == "sk-minimal-env-key"


# ─────────────────────────────────────────────────────────────────────────────
# Logging tests
# ─────────────────────────────────────────────────────────────────────────────


class TestEnvOverrideLogging:
    """Tests verifying that env var usage and secret redaction are logged."""

    def test_env_var_usage_logged(
        self,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(c) Using OPENAI_API_KEY from env triggers a debug log message."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-logged-key")
        with caplog.at_level(logging.DEBUG, logger="src.config.config"):
            load_config(config_file)
        assert any(
            "Using OPENAI_API_KEY from environment variable" in record.message
            for record in caplog.records
        )

    def test_base_url_env_var_usage_logged(
        self,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(c) Using OPENAI_BASE_URL from env triggers a debug log message."""
        monkeypatch.setenv("OPENAI_BASE_URL", "https://logged.api.local/v1")
        with caplog.at_level(logging.DEBUG, logger="src.config.config"):
            load_config(config_file)
        assert any(
            "Using OPENAI_BASE_URL from environment variable" in record.message
            for record in caplog.records
        )

    def test_api_key_redacted_in_effective_config_log(
        self,
        config_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(c) The effective config log redacts api_key — the raw key never appears."""
        secret_key = "sk-super-secret-key-do-not-leak"
        monkeypatch.setenv("OPENAI_API_KEY", secret_key)
        with caplog.at_level(logging.DEBUG, logger="src.config.config"):
            load_config(config_file)

        # The full redacted config JSON should contain REDACTED, not the raw key
        redacted_log_messages = [
            r.message for r in caplog.records if "Full redacted config" in r.message
        ]
        assert len(redacted_log_messages) == 1, "Expected exactly one full redacted config log"
        assert secret_key not in redacted_log_messages[0]
        assert "***REDACTED***" in redacted_log_messages[0]
