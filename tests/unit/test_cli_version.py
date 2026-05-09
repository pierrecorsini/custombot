"""Tests for the CLI --version flag."""

from __future__ import annotations

from click.testing import CliRunner

from main import cli


def test_version_flag_outputs_version_string(cli_runner: CliRunner) -> None:
    """--version prints the version from src.__version__ and exits with 0."""
    from src.__version__ import __version__

    result = cli_runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
    assert "custombot" in result.output.lower()


def test_version_flag_exits_without_subcommand(cli_runner: CliRunner) -> None:
    """--version works as a global option before any subcommand."""
    result = cli_runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() != ""


def test_version_flag_does_not_require_config(cli_runner: CliRunner, tmp_path, monkeypatch) -> None:
    """--version succeeds even when config file is absent."""
    import main as main_mod

    monkeypatch.setattr(main_mod, "CONFIG_PATH", tmp_path / "nonexistent_config.json")

    result = cli_runner.invoke(cli, ["--version"])

    assert result.exit_code == 0
