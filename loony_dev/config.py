"""Centralised configuration for loony-dev.

Priority (highest wins):
  1. CLI options
  2. Environment vars    LOONY_DEV_<KEY>  /  LOONY_DEV_<SECTION>__<KEY>
  3. ./.loony-dev.toml   (repo-level / per-checkout)
  4. ~/.config/loony-dev/config.toml
  5. /etc/loony-dev/config.toml
  6. Click param defaults  (the application's baseline)

Usage
-----
Replace ``@click.group()`` with ``@config.group()`` on the root CLI group::

    @config.group()
    def cli() -> None: ...

This injects config file + env var values via Click's built-in
``default_map`` so every option honours the configuration without any
change to ``@click.option(...)`` definitions.  The Click param
``default=...`` values become the baseline; config files sit on top,
and explicit CLI flags win over both.

To capture which options were explicitly supplied on the command line
(so a command can selectively forward them to subprocesses), decorate
the command with ``@config.capture_explicit``::

    @cli.command("supervisor")
    @click.option(...)
    @config.capture_explicit
    def supervisor_cmd(...):
        explicit = config.get_explicit_params()  # frozenset of param names
        ...
"""
from __future__ import annotations

import functools
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

logger = logging.getLogger(__name__)

_CONFIG_FILES = [
    "/etc/loony-dev/config.toml",
    "~/.config/loony-dev/config.toml",
    ".loony-dev.toml",
]

# Module-level: populated by @capture_explicit before each command body runs.
_explicit_params: frozenset[str] = frozenset()


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


def _load_config_files() -> dict[str, Any]:
    """Read and merge config files from lowest to highest priority."""
    merged: dict[str, Any] = {}
    for path_str in _CONFIG_FILES:
        path = Path(path_str).expanduser()
        if not path.exists():
            continue
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            _deep_merge(merged, data)
            logger.debug("Loaded config file: %s", path)
        except Exception:
            logger.warning("Failed to read config file %s", path, exc_info=True)
    return merged


def _apply_env_vars(cfg: dict[str, Any]) -> None:
    """Apply ``LOONY_DEV_*`` environment variables on top of *cfg*.

    Naming conventions:
      ``LOONY_DEV_BOT_NAME``          -> top-level ``bot_name``
      ``LOONY_DEV_WORKER__INTERVAL``  -> section ``worker``, key ``interval``
    """
    prefix = "LOONY_DEV_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        rest = env_key[len(prefix):].lower()
        if "__" in rest:
            section, key = rest.split("__", 1)
            cfg.setdefault(section, {})[key] = env_val
        else:
            cfg[rest] = env_val


def _build_default_map(cfg: dict[str, Any]) -> dict[str, Any]:
    """Build Click's ``default_map`` from the loaded config data.

    Top-level scalar values apply to all sub-commands.  Section dicts
    (``[worker]``, ``[supervisor]``, ``[ui]``) override top-level values
    for that specific sub-command.
    """
    # Split top-level scalars from section dicts.
    top_level = {k: v for k, v in cfg.items() if not isinstance(v, dict)}
    sections = {k: v for k, v in cfg.items() if isinstance(v, dict)}

    default_map: dict[str, Any] = {}
    # Known CLI sub-commands.
    for cmd in ("worker", "supervisor", "ui"):
        combined = {**top_level, **sections.get(cmd, {})}
        if combined:
            default_map[cmd] = combined

    return default_map


# ---------------------------------------------------------------------------
# Custom Click group
# ---------------------------------------------------------------------------

class _ConfigGroup(click.Group):
    """Click ``Group`` that injects config file + env var values as ``default_map``."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: click.Context | None = None,
        **extra: Any,
    ) -> click.Context:
        cfg = _load_config_files()
        _apply_env_vars(cfg)
        dm = _build_default_map(cfg)
        if dm:
            # Caller-supplied default_map wins over config-file values.
            merged: dict[str, Any] = dict(dm)
            _deep_merge(merged, extra.pop("default_map", {}))
            extra["default_map"] = merged
        return super().make_context(info_name, args, parent=parent, **extra)


def group(*args: Any, **kwargs: Any) -> Any:
    """Replacement for ``@click.group()`` that adds config file support.

    Usage::

        @config.group()
        def cli() -> None: ...
    """
    kwargs.setdefault("cls", _ConfigGroup)
    return click.group(*args, **kwargs)


# ---------------------------------------------------------------------------
# Explicit-param tracking
# ---------------------------------------------------------------------------

def capture_explicit(fn: Any) -> Any:
    """Decorator that records which CLI options were explicitly provided.

    Place this *directly above the function definition* (inside the Click
    decorators) so it wraps the command callback::

        @cli.command("supervisor")
        @click.option(...)
        @config.capture_explicit
        def supervisor_cmd(...): ...

    Call ``config.get_explicit_params()`` from inside the command body to
    retrieve the set of param names that came from the command line (not
    from defaults or config files).
    """
    @click.pass_context
    @functools.wraps(fn)
    def wrapper(ctx: click.Context, **kwargs: Any) -> Any:
        global _explicit_params
        _explicit_params = frozenset(
            name
            for name in kwargs
            if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
        )
        return fn(**kwargs)

    return wrapper


def get_explicit_params() -> frozenset[str]:
    """Return the set of param names explicitly supplied on the command line.

    Only valid *inside* a command decorated with ``@config.capture_explicit``.
    """
    return _explicit_params
