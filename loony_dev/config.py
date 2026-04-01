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

import logging
import os
import tomllib
from collections.abc import Iterator, Mapping as _Mapping
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)

def _get_config_files() -> list[str]:
    """Return platform-appropriate config file paths, lowest to highest priority."""
    files = []
    if os.name == "posix":
        files.append("/etc/loony-dev/config.toml")
    files.append(str(Path(click.get_app_dir("loony-dev")) / "config.toml"))
    files.append(".loony-dev.toml")
    return files

class Settings(_Mapping[str, Any]):
    """Immutable snapshot of resolved CLI + config + default values.

    Supports both dict-style access (``settings["key"]``) and attribute-style
    access (``settings.key``) for raw values.  Computed property helpers are
    provided for patterns that recur across commands:

    * ``settings.log_level``  — ``logging.DEBUG`` / ``logging.INFO`` based on
      the ``--verbose`` flag.
    * ``settings.supervisor_log``  — resolved :class:`~pathlib.Path`; defaults
      to ``<base_dir>/.logs/supervisor.log`` when ``--supervisor-log`` is unset.

    Mutation raises ``TypeError`` (no ``__setitem__``), matching the behaviour
    of the former :class:`~types.MappingProxyType`.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    # --- Mapping protocol (read-only) ---

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"Settings({self._data!r})"

    # --- Attribute-style access for raw keys ---

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    # --- Typed helpers ---

    @property
    def log_level(self) -> int:
        """Return ``logging.DEBUG`` when ``--verbose`` is set, else ``logging.INFO``."""
        return logging.DEBUG if self._data.get("verbose") else logging.INFO

    @property
    def base_dir(self) -> Path:
        """Return ``--base-dir`` as a resolved :class:`~pathlib.Path`."""
        return Path(self._data["base_dir"]).resolve()

    @property
    def include(self) -> list[str] | None:
        """Return ``--include`` patterns as a list, or ``None`` if unset."""
        patterns = self._data.get("include_patterns")
        return list(patterns) if patterns else None

    @property
    def exclude(self) -> list[str] | None:
        """Return ``--exclude`` patterns as a list, or ``None`` if unset."""
        patterns = self._data.get("exclude_patterns")
        return list(patterns) if patterns else None

    @property
    def supervisor_log(self) -> Path:
        """Resolve the supervisor log path.

        Returns the ``--supervisor-log`` value as a :class:`~pathlib.Path` when
        set; otherwise defaults to ``<base_dir>/.logs/supervisor.log``.
        """
        if self._data.get("supervisor_log"):
            return Path(self._data["supervisor_log"])
        return self.base_dir / ".logs" / "supervisor.log"


# Immutable snapshot of all resolved CLI + config + default values.
# Populated by ClickCommand.invoke() before the command body runs.
settings: Settings = Settings({})


def _populate_settings(ctx: click.Context) -> None:
    """Snapshot *ctx.params* into the immutable module-level :data:`settings`.

    Called from :meth:`ClickCommand.invoke` so that all code inside the
    command body can read configuration via ``config.settings`` instead of
    relying on the command function's parameter list.
    """
    global settings
    settings = Settings(dict(ctx.params))


# ---------------------------------------------------------------------------
# Config loading helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Merge *override* into *base* in-place; nested dicts are merged recursively."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _load_config() -> dict[str, Any]:
    """Load config from TOML files, returning a merged dict.

    File priority (lowest to highest): /etc, user-config-dir, ./.loony-dev.toml.
    Environment variables are handled natively by Click via auto_envvar_prefix.
    """
    result: dict[str, Any] = {}
    for path in _get_config_files():
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            _deep_merge(result, data)
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("Failed to load config file %s", path, exc_info=True)
    return result


def _build_default_map(
    cfg: dict[str, Any], cmd_name: str | None = None
) -> dict[str, Any]:
    """Build Click's ``default_map`` for *cmd_name* from the loaded config data.

    Strict scoping: top-level scalar values apply only to the root CLI group.
    Sub-command defaults come exclusively from their own TOML section
    (e.g. ``[worker]`` for the worker command).

    When *cmd_name* is ``None`` (no sub-command detected), the top-level
    scalars are returned directly as group-level defaults.
    """
    if cmd_name is None:
        return {k: v for k, v in cfg.items() if not isinstance(v, dict)}

    cmd_section = cfg.get(cmd_name, {})
    if isinstance(cmd_section, dict) and cmd_section:
        return {cmd_name: dict(cmd_section)}
    return {}


def _inject_default_map(cmd_name: str | None, extra: dict[str, Any]) -> None:
    """Load config and inject Click ``default_map`` into *extra* in-place."""
    cfg = _load_config()
    dm = _build_default_map(cfg, cmd_name)
    if dm:
        # Caller-supplied default_map wins over config-file values.
        merged: dict[str, Any] = dict(dm)
        _deep_merge(merged, extra.pop("default_map", {}))
        extra["default_map"] = merged


# ---------------------------------------------------------------------------
# Public Click classes
# ---------------------------------------------------------------------------

class ClickGroup(click.Group):
    """Click ``Group`` that injects config file + env var values as ``default_map``.

    Sub-commands registered via ``@cli.command(...)`` automatically use
    :class:`ClickCommand` so that :data:`settings` is populated before
    each command body runs.

    Usage::

        @click.group(cls=config.ClickGroup)
        def cli() -> None: ...
    """

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        extra.setdefault("auto_envvar_prefix", "LOONY_DEV")
        cmd_name = next((a for a in args if not a.startswith("-")), None)
        _inject_default_map(cmd_name, extra)
        return super().make_context(info_name, args, parent=parent, **extra)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        """Register a sub-command, defaulting to :class:`ClickCommand` so that
        :data:`settings` is populated before every sub-command body runs.
        """
        kwargs.setdefault("cls", ClickCommand)
        return super().command(*args, **kwargs)


class ClickCommand(click.Command):
    """Click ``Command`` that injects config file + env var values as ``default_map``
    and populates the immutable :data:`settings` object before the command body runs.

    Useful for top-level standalone commands (not sub-commands of a
    :class:`ClickGroup`, which already handles injection and registration).

    Usage::

        @click.command(cls=config.ClickCommand)
        def cmd(**_): ...
    """

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        if parent is None:
            # Standalone command (no parent group): inject a flat default_map
            # using only the command-specific section (strict scoping).
            # Sub-commands skip this — their parent ClickGroup.make_context()
            # already injects the nested map, and Click auto-propagates the
            # flat sub-map to each sub-command context via parent.default_map.
            extra.setdefault("auto_envvar_prefix", "LOONY_DEV")
            cfg = _load_config()
            section = cfg.get(info_name or "", {})
            dm = dict(section) if isinstance(section, dict) else {}
            if dm:
                merged: dict[str, Any] = dict(dm)
                _deep_merge(merged, extra.pop("default_map", {}))
                extra["default_map"] = merged
        return super().make_context(info_name, args, parent=parent, **extra)

    def invoke(self, ctx: click.Context) -> Any:
        _populate_settings(ctx)
        return super().invoke(ctx)

