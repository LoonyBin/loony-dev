"""Tests for loony_dev.config — config file loading and CLI default injection."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

import loony_dev.config as config
from loony_dev.cli import cli


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

def test_load_config_missing(tmp_path, monkeypatch):
    """Returns an empty dict when no config files exist."""
    monkeypatch.chdir(tmp_path)
    result = config._load_config()
    assert result == {}


def test_load_config_merges(tmp_path, monkeypatch):
    """Config file values are loaded and available in the result."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text(textwrap.dedent("""\
        bot_name = "test-bot"

        [worker]
        interval = 30
    """))
    monkeypatch.chdir(tmp_path)
    result = config._load_config()
    assert result["bot_name"] == "test-bot"
    assert result["worker"]["interval"] == 30


def test_load_config_invalid_ignored(tmp_path, monkeypatch):
    """Malformed config files are skipped without raising."""
    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_bytes(b"not valid toml ][")
    monkeypatch.chdir(tmp_path)
    result = config._load_config()
    assert result == {}



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


def test_build_default_map_top_level_not_applied_to_subcommands():
    cfg = {
        "bot_name": "shared-bot",
        "worker": {"interval": 30},
    }
    # Strict scoping: top-level scalars do NOT flow into subcommands.
    assert "bot_name" not in config._build_default_map(cfg, "worker").get("worker", {})
    # A command with no section at all returns an empty map.
    assert config._build_default_map(cfg, "supervisor") == {}


def test_build_default_map_section_only():
    cfg = {
        "min_role": "triage",
        "worker": {"min_role": "write"},
    }
    # Strict scoping: worker only reads its own section; top-level is ignored.
    assert config._build_default_map(cfg, "worker")["worker"]["min_role"] == "write"
    # supervisor has no section → empty map (top-level value not inherited).
    assert config._build_default_map(cfg, "supervisor") == {}


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
    """Env var value takes precedence over config file for the same key.

    Click's auto_envvar_prefix=LOONY_DEV means the worker --interval option
    reads from LOONY_DEV_WORKER_INTERVAL (prefix + command + param name).
    """
    import click

    cfg = tmp_path / ".loony-dev.toml"
    cfg.write_text("[mytest]\ninterval = 30\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOONY_DEV_MYTEST_INTERVAL", "99")

    observed: list[int] = []

    @click.group(cls=config.ClickGroup)
    def grp() -> None:
        pass

    @grp.command("mytest")
    @click.option("--interval", default=60)
    def mytest_cmd(**_) -> None:
        observed.append(config.settings["interval"])

    runner = CliRunner()
    result = runner.invoke(grp, ["mytest"])
    assert result.exit_code == 0
    # env var (99) wins over config file (30) and Click default (60)
    assert observed[0] == 99


# ---------------------------------------------------------------------------
# settings — immutable global config object
# ---------------------------------------------------------------------------

def test_settings_populated_before_command_body(tmp_path, monkeypatch):
    """config.settings is an immutable snapshot of resolved params before the command body runs."""
    import click

    monkeypatch.chdir(tmp_path)
    observed: list = []

    @click.command(cls=config.ClickCommand)
    @click.option("--count", default=5)
    @click.option("--name", default="default")
    def cmd(**_) -> None:
        observed.append(dict(config.settings))

    runner = CliRunner()
    result = runner.invoke(cmd, ["--count", "42"])
    assert result.exit_code == 0
    assert observed[0]["count"] == 42
    assert observed[0]["name"] == "default"


def test_settings_is_immutable(tmp_path, monkeypatch):
    """config.settings raises TypeError on mutation attempts."""
    import click

    monkeypatch.chdir(tmp_path)
    errors: list = []

    @click.command(cls=config.ClickCommand)
    @click.option("--val", default=1)
    def cmd(**_) -> None:
        try:
            config.settings["val"] = 99  # type: ignore[index]
        except TypeError as exc:
            errors.append(exc)

    runner = CliRunner()
    runner.invoke(cmd, [])
    assert errors, "Expected TypeError when mutating settings"


def test_settings_populated_via_clickgroup(tmp_path, monkeypatch):
    """Sub-commands of a ClickGroup also populate config.settings."""
    import click

    monkeypatch.chdir(tmp_path)
    observed: list = []

    @click.group(cls=config.ClickGroup)
    def grp() -> None:
        pass

    @grp.command("sub")
    @click.option("--level", default=10)
    def sub_cmd(**_) -> None:
        observed.append(config.settings["level"])

    runner = CliRunner()
    result = runner.invoke(grp, ["sub", "--level", "7"])
    assert result.exit_code == 0
    assert observed[0] == 7


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def test_settings_log_level_verbose():
    """log_level returns logging.DEBUG when verbose is True."""
    import logging

    s = config.Settings({"verbose": True})
    assert s.log_level == logging.DEBUG


def test_settings_log_level_not_verbose():
    """log_level returns logging.INFO when verbose is False."""
    import logging

    s = config.Settings({"verbose": False})
    assert s.log_level == logging.INFO


def test_settings_supervisor_log_explicit(tmp_path):
    """supervisor_log returns Path(supervisor_log) when set."""
    p = str(tmp_path / "custom.log")
    s = config.Settings({"supervisor_log": p, "base_dir": str(tmp_path)})
    assert s.supervisor_log == Path(p)


def test_settings_supervisor_log_default(tmp_path):
    """supervisor_log defaults to <base_dir>/.logs/supervisor.log."""
    s = config.Settings({"supervisor_log": None, "base_dir": str(tmp_path)})
    assert s.supervisor_log == tmp_path.resolve() / ".logs" / "supervisor.log"


def test_settings_attribute_access():
    """Raw keys are accessible as attributes."""
    s = config.Settings({"verbose": True, "interval": 60})
    assert s.verbose is True
    assert s.interval == 60


def test_settings_missing_attribute_raises():
    """Accessing a missing key as an attribute raises AttributeError."""
    s = config.Settings({})
    with pytest.raises(AttributeError):
        _ = s.nonexistent
