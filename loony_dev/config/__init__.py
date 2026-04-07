"""Centralised configuration for loony-dev.

Priority (highest wins):
  1. CLI options
  2. Environment vars    LOONY_DEV_<COMMAND>_<KEY>  (via Click auto_envvar_prefix)
  3. ./.loony-dev.toml   (repo-level / per-checkout)
  4. <user-config-dir>/loony-dev/config.toml  (platform-specific via click.get_app_dir)
  5. /etc/loony-dev/config.toml  (POSIX only)
  6. Click param defaults  (the application's baseline)

Usage
-----
Use ``cls=config.ClickGroup`` on the root CLI group::

    @click.group(cls=config.ClickGroup)
    def cli() -> None: ...

This injects config file + env var values via Click's built-in
``default_map`` so every option honours the configuration without any
change to ``@click.option(...)`` definitions.  The Click param
``default=...`` values become the baseline; config files sit on top,
and explicit CLI flags win over both.

Sub-commands registered via ``@cli.command(...)`` automatically use
:class:`ClickCommand`, which populates the module-level
:data:`config.settings` immutable object before the command body runs.
Command functions can therefore accept ``**_`` and read all resolved
values from ``config.settings`` instead of a long parameter list::

    @cli.command("worker")
    @click.option("--interval", default=60)
    def worker(**_) -> None:
        interval = config.settings["interval"]
        ...

For standalone commands (not sub-commands of a configured group), use
``cls=config.ClickCommand`` explicitly::

    @click.command(cls=config.ClickCommand)
    def cmd(**_): ...

"""
from __future__ import annotations

from ._settings import Settings
from ._loader import (
    _LEGACY_ENV_VARS,
    _apply_legacy_env_vars,
    _populate_settings,
    _get_config_files,
    _deep_merge,
    _load_config,
    _build_default_map,
    _inject_default_map,
)
from ._click import ClickGroup, ClickCommand

# Immutable snapshot of all resolved CLI + config + default values.
# Populated by ClickCommand.invoke() before the command body runs.
settings: Settings = Settings({})

__all__ = [
    "Settings",
    "settings",
    "ClickGroup",
    "ClickCommand",
    "_LEGACY_ENV_VARS",
    "_apply_legacy_env_vars",
    "_populate_settings",
    "_get_config_files",
    "_deep_merge",
    "_load_config",
    "_build_default_map",
    "_inject_default_map",
]
