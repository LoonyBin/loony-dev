"""Tests for loony_dev.config — config file loading and CLI default injection."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

import loony_dev.config as config
from loony_dev.cli import cli


# ---------------------------------------------------------------------------
# _load_config_files
# ---------------------------------------------------------------------------

def test_load_config_files_missing(tmp_path, monkeypatch):
    """Returns an empty dict when no config files exist."""
    monkeypatch.chdir(tmp_path)
    result = config._load_config_files()
    assert result == {}


def test_load_config_files_merges(tmp_path, monkeypatch):
    """Config file values are loaded and available in the result."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text(textwrap.dedent("""\
        bot_name = "test-bot"

        [worker]
        interval = 30
    """))
    monkeypatch.chdir(tmp_path)
    result = config._load_config_files()
    assert result["bot_name"] == "test-bot"
    assert result["worker"]["interval"] == 30


def test_load_config_files_invalid_ignored(tmp_path, monkeypatch):
    """Malformed config files are skipped without raising."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_bytes(b"not valid toml ][")
    monkeypatch.chdir(tmp_path)
    result = config._load_config_files()
    assert result == {}


# ---------------------------------------------------------------------------
# _apply_env_vars
# ---------------------------------------------------------------------------

def test_apply_env_vars_top_level(monkeypatch):
    monkeypatch.setenv("LOONY_DEV_BOT_NAME", "env-bot")
    cfg: dict = {}
    config._apply_env_vars(cfg)
    assert cfg["bot_name"] == "env-bot"


def test_apply_env_vars_section(monkeypatch):
    monkeypatch.setenv("LOONY_DEV_WORKER__INTERVAL", "45")
    cfg: dict = {}
    config._apply_env_vars(cfg)
    assert cfg["worker"]["interval"] == "45"


def test_apply_env_vars_ignores_other_prefixes(monkeypatch):
    monkeypatch.setenv("OTHER_BOT_NAME", "ignored")
    cfg: dict = {}
    config._apply_env_vars(cfg)
    assert "bot_name" not in cfg


# ---------------------------------------------------------------------------
# _build_default_map
# ---------------------------------------------------------------------------

def test_build_default_map_sections():
    cfg = {
        "worker": {"interval": 30, "work_dir": "/tmp"},
        "supervisor": {"base_dir": "/srv"},
    }
    assert config._build_default_map(cfg, "worker")["worker"]["interval"] == 30
    assert config._build_default_map(cfg, "supervisor")["supervisor"]["base_dir"] == "/srv"


def test_build_default_map_top_level_applied_to_invoked_command():
    cfg = {
        "bot_name": "shared-bot",
        "worker": {"interval": 30},
    }
    # top-level bot_name applies to whichever command is invoked.
    assert config._build_default_map(cfg, "worker")["worker"]["bot_name"] == "shared-bot"
    assert config._build_default_map(cfg, "supervisor")["supervisor"]["bot_name"] == "shared-bot"


def test_build_default_map_section_overrides_top_level():
    cfg = {
        "min_role": "triage",
        "worker": {"min_role": "write"},
    }
    assert config._build_default_map(cfg, "worker")["worker"]["min_role"] == "write"
    # supervisor gets the top-level value.
    assert config._build_default_map(cfg, "supervisor")["supervisor"]["min_role"] == "triage"


def test_build_default_map_empty():
    assert config._build_default_map({}) == {}
    assert config._build_default_map({}, None) == {}


def test_build_default_map_no_cmd_name_returns_top_level():
    """Without a cmd_name, top-level scalars are returned as group-level defaults."""
    cfg = {"bot_name": "loony", "worker": {"interval": 30}}
    dm = config._build_default_map(cfg, None)
    assert dm == {"bot_name": "loony"}
    assert "worker" not in dm


def test_build_default_map_only_builds_invoked_command():
    cfg = {"worker": {"interval": 30}, "supervisor": {"base_dir": "/srv"}}
    dm = config._build_default_map(cfg, "worker")
    assert "worker" in dm
    assert "supervisor" not in dm


# ---------------------------------------------------------------------------
# CLI integration — config file values respected as defaults
# ---------------------------------------------------------------------------

def test_worker_interval_from_config_file(tmp_path, monkeypatch):
    """Config file sets worker interval; CLI should use that value."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text("[worker]\ninterval = 42\n")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    # Invoke --help to avoid actually running the worker; the default value
    # shown in help output reflects the config-injected default_map.
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0
    # The help text should mention our configured interval.
    assert "42" in result.output


def test_supervisor_base_dir_from_config_file(tmp_path, monkeypatch):
    """Config file sets supervisor base_dir default."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text('[supervisor]\nbase_dir = "/custom/base"\n')
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["supervisor", "--help"])
    assert result.exit_code == 0
    assert "/custom/base" in result.output


def test_env_var_overrides_config_file(tmp_path, monkeypatch):
    """Env var value takes precedence over config file for the same key."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text("[worker]\ninterval = 30\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOONY_DEV_WORKER__INTERVAL", "99")

    runner = CliRunner()
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0
    assert "99" in result.output


# ---------------------------------------------------------------------------
# capture_explicit / get_explicit_params
# ---------------------------------------------------------------------------

def test_get_explicit_params_initially_empty():
    assert config.get_explicit_params() == frozenset()


def test_capture_explicit_records_cli_params(tmp_path, monkeypatch):
    """@capture_explicit sets the module-level explicit params when CLI flags are used."""
    monkeypatch.chdir(tmp_path)

    captured: list[frozenset] = []

    import click
    import functools

    @click.command()
    @click.option("--foo", default="x")
    @click.option("--bar", default="y")
    @config.capture_explicit
    def cmd(foo: str, bar: str) -> None:
        captured.append(config.get_explicit_params())

    runner = CliRunner()
    runner.invoke(cmd, ["--foo", "hello"])
    assert captured[0] == {"foo"}


def test_capture_explicit_empty_when_all_defaults(tmp_path, monkeypatch):
    """@capture_explicit reports empty set when no flags were supplied."""
    monkeypatch.chdir(tmp_path)

    captured: list[frozenset] = []

    import click

    @click.command()
    @click.option("--foo", default="x")
    @config.capture_explicit
    def cmd(foo: str) -> None:
        captured.append(config.get_explicit_params())

    runner = CliRunner()
    runner.invoke(cmd, [])
    assert captured[0] == frozenset()
