from __future__ import annotations

from typing import Any

import click

from ._loader import _inject_default_map, _load_config, _deep_merge, _populate_settings


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
