from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping as _Mapping
from pathlib import Path
from typing import Any


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
