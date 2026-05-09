"""
Tests for src/config/config.py — save_config → load_config round-trip.

Verifies that serialising a Config to JSON and loading it back preserves
every field value, including:
  - Optional[int] fields that default to None
  - Nested dataclasses (LLMConfig, WhatsAppConfig, NeonizeConfig)
  - List fields (allowed_numbers)
  - Fields with non-default values
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.config import (
    Config,
    LLMConfig,
    MiddlewareConfig,
    NeonizeConfig,
    ShellConfig,
    WhatsAppConfig,
)
from src.config.config import _from_dict, load_config, save_config
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _fully_populated_config(workspace: Path) -> Config:
    """Return a Config where *every* field is set to a non-default value."""
    return Config(
        llm=LLMConfig(
            model="gpt-4-turbo",
            base_url="https://custom.api.example.com/v1",
            api_key="sk-round-trip-test-key",
            temperature=0.3,
            max_tokens=2048,
            timeout=60.0,
            system_prompt_prefix="[TEST PREFIX] ",
            max_tool_iterations=5,
            embedding_model="text-embedding-3-large",
            embedding_dimensions=3072,
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path=str(workspace / "neonize_test.db"),
            ),
            allowed_numbers=["1234567890", "0987654321"],
            allow_all=False,
        ),
        load_history=True,
        memory_max_history=25,
        skills_auto_load=False,
        skills_user_directory=str(workspace / "custom_skills"),
        log_incoming_messages=False,
        log_routing_info=True,
        shutdown_timeout=15.0,
        log_format="json",
        log_file=str(workspace / "logs" / "test.log"),
        log_max_bytes=5 * 1024 * 1024,
        log_backup_count=3,
        log_verbosity="verbose",
        log_llm=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigRoundTrip:
    """save_config() → load_config() preserves all fields."""

    def test_full_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fully-populated Config survives save → load unchanged."""
        # Prevent env-var overrides from polluting the round-trip
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        original = _fully_populated_config(tmp_path)
        config_path = tmp_path / "config.json"

        save_config(original, config_path)
        loaded = load_config(config_path)

        # Verify the file was actually written
        assert config_path.exists()

        # Compare the full dataclass dicts (excludes repr, methods, etc.)
        assert asdict(loaded) == asdict(original)

    def test_optional_max_tokens_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_tokens=None (the default) round-trips correctly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test"),
            whatsapp=WhatsAppConfig(provider="neonize"),
        )
        # Explicitly ensure max_tokens is None
        assert config.llm.max_tokens is None

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.llm.max_tokens is None

    def test_optional_max_tokens_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_tokens=2048 round-trips correctly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test", max_tokens=2048),
            whatsapp=WhatsAppConfig(provider="neonize"),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.llm.max_tokens == 2048

    def test_empty_allowed_numbers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty allowed_numbers list round-trips correctly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test"),
            whatsapp=WhatsAppConfig(provider="neonize", allowed_numbers=[]),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.whatsapp.allowed_numbers == []

    def test_allowed_numbers_with_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allowed_numbers with multiple entries round-trips correctly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        numbers = ["1234567890", "9876543210", "5555555555"]
        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test"),
            whatsapp=WhatsAppConfig(provider="neonize", allowed_numbers=numbers),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.whatsapp.allowed_numbers == numbers

    def test_neonize_config_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nested NeonizeConfig.db_path round-trips correctly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        custom_db = str(tmp_path / "custom_neonize.db")
        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test"),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=custom_db),
            ),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.whatsapp.neonize.db_path == custom_db

    def test_saved_file_includes_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save_config writes a $schema key that load_config tolerates."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config = Config(
            llm=LLMConfig(model="gpt-4o", api_key="sk-test"),
            whatsapp=WhatsAppConfig(provider="neonize"),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        assert "$schema" in raw

        # And loading still works fine
        loaded = load_config(config_path)
        assert asdict(loaded) == asdict(config)

    def test_all_config_fields_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every field on Config (and nested dataclasses) is preserved."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        original = _fully_populated_config(tmp_path)
        config_path = tmp_path / "config.json"

        save_config(original, config_path)
        loaded = load_config(config_path)

        # Walk every dataclass field and compare individually for clear diagnostics
        for f in fields(Config):
            orig_val = getattr(original, f.name)
            load_val = getattr(loaded, f.name)
            assert orig_val == load_val, (
                f"Mismatch on Config.{f.name}: {orig_val!r} != {load_val!r}"
            )

        # Also walk LLMConfig fields
        for f in fields(LLMConfig):
            assert getattr(original.llm, f.name) == getattr(loaded.llm, f.name), (
                f"Mismatch on LLMConfig.{f.name}"
            )

        # And WhatsAppConfig fields
        for f in fields(WhatsAppConfig):
            assert getattr(original.whatsapp, f.name) == getattr(loaded.whatsapp, f.name), (
                f"Mismatch on WhatsAppConfig.{f.name}"
            )

        # And NeonizeConfig fields
        for f in fields(NeonizeConfig):
            assert getattr(original.whatsapp.neonize, f.name) == getattr(
                loaded.whatsapp.neonize, f.name
            ), f"Mismatch on NeonizeConfig.{f.name}"

    def test_round_trip_preserves_unicode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unicode in system_prompt_prefix round-trips without corruption."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config = Config(
            llm=LLMConfig(
                model="gpt-4o",
                api_key="sk-test",
                system_prompt_prefix="Héllo wörld — こんにちは 🤖",
            ),
            whatsapp=WhatsAppConfig(provider="neonize"),
        )

        config_path = tmp_path / "config.json"
        save_config(config, config_path)
        loaded = load_config(config_path)

        assert loaded.llm.system_prompt_prefix == "Héllo wörld — こんにちは 🤖"


class TestConfigSchemaRejectsUnknownKeys:
    """load_config() rejects unknown keys via schema validation."""

    @staticmethod
    def _write_config(data: dict, path: Path) -> None:
        """Write raw config dict to a JSON file."""
        path.write_text(json.dumps(data), encoding="utf-8")

    def _get_error_paths(self, exc_info) -> list[str]:
        """Extract error field paths from a ConfigurationError."""
        return [e["path"] for e in exc_info.value.details["errors"]]

    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        """An unknown top-level key causes a ConfigurationError."""
        from src.exceptions import ConfigurationError

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o"},
                "whatsapp": {"provider": "neonize", "neonize": {"db_path": "test.db"}},
                "llm_mode": "fast",
            },
            config_path,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_path)

        assert any("llm_mode" in p for p in self._get_error_paths(exc_info))

    def test_unknown_nested_key_in_llm_rejected(self, tmp_path: Path) -> None:
        """An unknown key inside the llm object causes a ConfigurationError."""
        from src.exceptions import ConfigurationError

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o", "speed": "turbo"},
                "whatsapp": {"provider": "neonize", "neonize": {"db_path": "test.db"}},
            },
            config_path,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_path)

        assert any("speed" in p for p in self._get_error_paths(exc_info))

    def test_unknown_nested_key_in_whatsapp_rejected(self, tmp_path: Path) -> None:
        """An unknown key inside the whatsapp object causes a ConfigurationError."""
        from src.exceptions import ConfigurationError

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o"},
                "whatsapp": {
                    "provider": "neonize",
                    "neonize": {"db_path": "test.db"},
                    "bridge_url": "ws://localhost:8080",
                },
            },
            config_path,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_path)

        assert any("bridge_url" in p for p in self._get_error_paths(exc_info))

    def test_unknown_nested_key_in_neonize_rejected(self, tmp_path: Path) -> None:
        """An unknown key inside whatsapp.neonize causes a ConfigurationError."""
        from src.exceptions import ConfigurationError

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o"},
                "whatsapp": {
                    "provider": "neonize",
                    "neonize": {"db_path": "test.db", "timeout": 30},
                },
            },
            config_path,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_path)

        assert any("timeout" in p for p in self._get_error_paths(exc_info))

    def test_multiple_unknown_keys_all_reported(self, tmp_path: Path) -> None:
        """Multiple unknown keys are all reported in the validation errors."""
        from src.exceptions import ConfigurationError

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o", "speed": "fast"},
                "whatsapp": {
                    "provider": "neonize",
                    "neonize": {"db_path": "test.db"},
                    "region": "eu",
                },
                "debug_mode": True,
            },
            config_path,
        )

        with pytest.raises(ConfigurationError) as exc_info:
            load_config(config_path)

        paths = self._get_error_paths(exc_info)
        assert any("speed" in p for p in paths)
        assert any("region" in p for p in paths)
        assert any("debug_mode" in p for p in paths)

    def test_valid_config_with_no_unknown_keys_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config with only known keys loads without error."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "llm": {"model": "gpt-4o", "temperature": 0.5},
                "whatsapp": {
                    "provider": "neonize",
                    "neonize": {"db_path": "test.db"},
                },
                "load_history": False,
            },
            config_path,
        )

        config = load_config(config_path)
        assert config.llm.model == "gpt-4o"
        assert config.llm.temperature == 0.5
        assert config.load_history is False


class TestConfigUnknownKeyWarnings:
    """_check_unknown_keys() logs warnings with fuzzy-match suggestions."""

    @staticmethod
    def _write_config(data: dict, path: Path) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_typo_gets_did_you_mean_suggestion(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A typo like 'temperture' suggests 'temperature' via fuzzy match."""
        from src.config.config import _check_unknown_keys

        config_path = tmp_path / "config.json"
        self._write_config(
            {"llm": {"model": "gpt-4o", "temperture": 0.5}, "whatsapp": {"provider": "neonize"}},
            config_path,
        )

        data = json.loads(config_path.read_text(encoding="utf-8"))
        with caplog.at_level("WARNING"):
            _check_unknown_keys(data, config_path)

        assert any(
            "did you mean" in r.message.lower() and "temperature" in r.message.lower()
            for r in caplog.records
        ), (
            f"Expected 'did you mean … temperature' warning, got: {[r.message for r in caplog.records]}"
        )

    def test_unknown_key_no_match_gets_not_recognised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A completely unfamiliar key logs 'not recognised'."""
        from src.config.config import _check_unknown_keys

        config_path = tmp_path / "config.json"
        self._write_config(
            {"llm": {"model": "gpt-4o", "xylophone": True}, "whatsapp": {"provider": "neonize"}},
            config_path,
        )

        data = json.loads(config_path.read_text(encoding="utf-8"))
        with caplog.at_level("WARNING"):
            _check_unknown_keys(data, config_path)

        assert any(
            "not recognised" in r.message.lower() and "xylophone" in r.message.lower()
            for r in caplog.records
        ), (
            f"Expected 'not recognised … xylophone' warning, got: {[r.message for r in caplog.records]}"
        )

    def test_top_level_typo_gets_suggestion(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A top-level typo like 'lod_history' suggests 'load_history'."""
        from src.config.config import _check_unknown_keys

        config_path = tmp_path / "config.json"
        self._write_config(
            {"llm": {"model": "gpt-4o"}, "whatsapp": {"provider": "neonize"}, "lod_history": True},
            config_path,
        )

        data = json.loads(config_path.read_text(encoding="utf-8"))
        with caplog.at_level("WARNING"):
            _check_unknown_keys(data, config_path)

        assert any(
            "did you mean" in r.message.lower() and "load_history" in r.message.lower()
            for r in caplog.records
        ), (
            f"Expected 'did you mean … load_history' warning, got: {[r.message for r in caplog.records]}"
        )

    def test_valid_keys_no_warnings(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A config with only known keys produces no unknown-key warnings."""
        from src.config.config import _check_unknown_keys

        config_path = tmp_path / "config.json"
        self._write_config(
            {"llm": {"model": "gpt-4o", "temperature": 0.5}, "whatsapp": {"provider": "neonize"}},
            config_path,
        )

        data = json.loads(config_path.read_text(encoding="utf-8"))
        with caplog.at_level("WARNING"):
            _check_unknown_keys(data, config_path)

        unknown_warnings = [r for r in caplog.records if "unknown config key" in r.message.lower()]
        assert unknown_warnings == []

    def test_schema_dollar_key_ignored(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The '$schema' metadata key does not trigger a warning."""
        from src.config.config import _check_unknown_keys

        config_path = tmp_path / "config.json"
        self._write_config(
            {
                "$schema": "https://example.com/schema",
                "llm": {"model": "gpt-4o"},
                "whatsapp": {"provider": "neonize"},
            },
            config_path,
        )

        data = json.loads(config_path.read_text(encoding="utf-8"))
        with caplog.at_level("WARNING"):
            _check_unknown_keys(data, config_path)

        unknown_warnings = [r for r in caplog.records if "unknown config key" in r.message.lower()]
        assert unknown_warnings == []


# ─────────────────────────────────────────────────────────────────────────────
# Property-based roundtrip tests (Hypothesis)
# ─────────────────────────────────────────────────────────────────────────────

# JSON-safe floats — exclude NaN/inf which break equality comparisons.
_safe_float = st.floats(
    min_value=0.0,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)

# ── Sub-config dict strategies ────────────────────────────────────────────────

_neonize_dicts = st.fixed_dictionaries(
    {
        "db_path": st.text(min_size=1, max_size=300),
    }
)

_shell_dicts = st.fixed_dictionaries(
    {
        "command_denylist": st.lists(st.text(min_size=1, max_size=100), max_size=10),
        "command_allowlist": st.lists(st.text(min_size=1, max_size=100), max_size=10),
    }
)

_middleware_dicts = st.fixed_dictionaries(
    {
        "middleware_order": st.lists(st.text(min_size=1, max_size=80), max_size=10),
        "extra_middleware_paths": st.lists(st.text(min_size=1, max_size=120), max_size=10),
    }
)

_llm_dicts = st.fixed_dictionaries(
    {
        "model": st.text(min_size=1, max_size=100),
        "base_url": st.text(min_size=0, max_size=200),
        "api_key": st.text(min_size=0, max_size=100),
        "temperature": _safe_float,
        "max_tokens": st.one_of(st.none(), st.integers(min_value=1, max_value=128_000)),
        "timeout": _safe_float,
        "system_prompt_prefix": st.text(min_size=0, max_size=500),
        "max_tool_iterations": st.integers(min_value=1, max_value=50),
        "embedding_model": st.text(min_size=1, max_size=100),
        "embedding_dimensions": st.integers(min_value=1, max_value=8192),
        "embedding_base_url": st.text(min_size=0, max_size=200),
        "embedding_api_key": st.text(min_size=0, max_size=100),
        "stream_response": st.booleans(),
    }
)

_whatsapp_dicts = st.fixed_dictionaries(
    {
        "provider": st.text(min_size=1, max_size=50),
        "neonize": _neonize_dicts,
        "allowed_numbers": st.lists(st.text(min_size=1, max_size=20), max_size=20),
        "allow_all": st.booleans(),
    }
)

# ── Full Config dict strategy ─────────────────────────────────────────────────

_config_dicts = st.fixed_dictionaries(
    {
        "llm": _llm_dicts,
        "whatsapp": _whatsapp_dicts,
        "shell": _shell_dicts,
        "middleware": _middleware_dicts,
        "load_history": st.booleans(),
        "memory_max_history": st.integers(min_value=1, max_value=10000),
        "skills_auto_load": st.booleans(),
        "skills_user_directory": st.text(min_size=0, max_size=300),
        "log_incoming_messages": st.booleans(),
        "log_routing_info": st.booleans(),
        "shutdown_timeout": _safe_float,
        "log_format": st.sampled_from(["text", "json"]),
        "log_file": st.text(min_size=0, max_size=300),
        "log_max_bytes": st.integers(min_value=1024, max_value=100_000_000),
        "log_backup_count": st.integers(min_value=0, max_value=100),
        "log_verbosity": st.sampled_from(["quiet", "normal", "verbose"]),
        "log_llm": st.booleans(),
        "max_thread_pool_workers": st.one_of(st.none(), st.integers(min_value=1, max_value=256)),
        "max_chat_lock_cache_size": st.integers(min_value=1, max_value=100_000),
        "max_chat_lock_eviction_policy": st.sampled_from(["grow", "reject_on_full"]),
        "max_chat_lock_cache_ttl": st.one_of(st.none(), st.floats(min_value=0, max_value=86400, allow_nan=False, allow_infinity=False)),
        "max_concurrent_messages": st.integers(min_value=1, max_value=10000),
    }
)


class TestFromDictPropertyBased:
    """Hypothesis-driven tests for _from_dict() ↔ asdict() roundtrip."""

    @given(d=_config_dicts)
    @settings(max_examples=200)
    def test_config_roundtrip_idempotent(self, d: dict) -> None:
        """_from_dict → asdict → _from_dict produces identical Config."""
        first = _from_dict(Config, d)
        second = _from_dict(Config, asdict(first))
        assert asdict(first) == asdict(second)

    @given(d=_llm_dicts)
    @settings(max_examples=200)
    def test_llm_config_roundtrip(self, d: dict) -> None:
        """LLMConfig roundtrips: asdict(_from_dict(d)) == d."""
        assert asdict(_from_dict(LLMConfig, d)) == d

    @given(d=_whatsapp_dicts)
    @settings(max_examples=200)
    def test_whatsapp_config_roundtrip(self, d: dict) -> None:
        """WhatsAppConfig (with nested NeonizeConfig) roundtrips correctly."""
        assert asdict(_from_dict(WhatsAppConfig, d)) == d

    @given(d=_shell_dicts)
    @settings(max_examples=100)
    def test_shell_config_roundtrip(self, d: dict) -> None:
        """ShellConfig roundtrips: asdict(_from_dict(d)) == d."""
        assert asdict(_from_dict(ShellConfig, d)) == d

    @given(d=_middleware_dicts)
    @settings(max_examples=100)
    def test_middleware_config_roundtrip(self, d: dict) -> None:
        """MiddlewareConfig roundtrips: asdict(_from_dict(d)) == d."""
        assert asdict(_from_dict(MiddlewareConfig, d)) == d
