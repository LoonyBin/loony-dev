from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

import click

from ._settings import Settings

logger = logging.getLogger(__name__)


_LEGACY_ENV_VARS: dict[str, str] = {
    # Maps legacy env var name → settings key.  Values are applied only when
    # the settings key has not already been set via CLI or config file.
    # Support will be removed in a future major version.
    "LOONY_STUCK_THRESHOLD_HOURS": "stuck_threshold_hours",
}


def _apply_legacy_env_vars(data: dict[str, Any]) -> None:
    """Warn about and optionally apply deprecated environment variables.

    Each entry in :data:`_LEGACY_ENV_VARS` is checked; if the env var is set
    and the corresponding settings key was not already provided via CLI or
    config file (i.e. its value is ``None``), the env var value is used as a
    fallback.
    """
    for env_var, settings_key in _LEGACY_ENV_VARS.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        logger.warning(
            "%s is deprecated and will be removed in a future major version. "
            "Use --%s or '%s' in the config file instead.",
            env_var,
            settings_key.replace("_", "-"),
            settings_key,
        )
        if data.get(settings_key) is None:
            try:
                data[settings_key] = int(value)
            except ValueError:
                logger.warning(
                    "Could not parse %s=%r as an integer; ignoring.", env_var, value
                )


def _populate_settings(ctx: click.Context) -> None:
    """Snapshot *ctx.params* into the immutable module-level :data:`settings`.

    Called from :meth:`ClickCommand.invoke` so that all code inside the
    command body can read configuration via ``config.settings`` instead of
    relying on the command function's parameter list.

    In addition to the command's own parameters, any shared (non-command)
    config sections (e.g. ``[github]``) are included as nested dicts so
    that ``config.settings["github"]`` works without extra loading.
    """
    import loony_dev.config as _config_module
    data = dict(ctx.params)
    _apply_legacy_env_vars(data)
    # Include shared config sections (dict values whose key isn't already a
    # CLI param) so cross-cutting config like [github] is accessible.
    for key, value in _load_config().items():
        if isinstance(value, dict) and key not in data:
            data[key] = value
    _config_module.settings = Settings(data)


def _get_config_files() -> list[str]:
    """Return platform-appropriate config file paths, lowest to highest priority."""
    files = []
    if os.name == "posix":
        files.append("/etc/loony-dev/config.toml")
    files.append(str(Path(click.get_app_dir("loony-dev")) / "config.toml"))
    files.append(".loony-dev.toml")
    return files


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
