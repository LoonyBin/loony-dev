"""Centralised configuration for loony-dev.

Priority (highest wins):
  1. CLI options         (applied via initialize())
  2. Environment vars    LOONY_DEV_<KEY>  /  LOONY_DEV_<SECTION>__<KEY>
  3. ./.loony-dev.toml   (repo-level / per-checkout)
  4. ~/.config/loony-dev/config.toml
  5. /etc/loony-dev/config.toml
  6. Built-in defaults   (loony_dev/_defaults.toml)

Usage
-----
Import the module-level ``settings`` object to read values::

    from loony_dev import config
    ttl = config.settings.PERMISSION_CACHE_TTL
    interval = config.settings.WORKER.INTERVAL

At program startup (in cli.py), call ``initialize()`` exactly once to apply
CLI overrides and lock the settings against further mutation::

    config.initialize({"worker.interval": 120, "bot_name": "my-bot"})
"""
from __future__ import annotations

from pathlib import Path

from dynaconf import Dynaconf

_THIS_DIR = Path(__file__).parent

# Config-file search path (lowest to highest priority among files).
_CONFIG_FILES = [
    str(_THIS_DIR / "_defaults.toml"),       # shipped with the package
    "/etc/loony-dev/config.toml",
    "~/.config/loony-dev/config.toml",
    ".loony-dev.toml",
]


class ConfigImmutabilityError(RuntimeError):
    """Raised when code attempts to mutate settings after initialize() is called."""


class _FrozenSettings:
    """Thin proxy that forwards reads to a Dynaconf instance but blocks writes.

    dynaconf 3.2.x does not provide a built-in freeze() API, so we wrap the
    Settings object and raise ``ConfigImmutabilityError`` on any attempt to
    call ``set()`` or ``update()`` after the config has been locked.
    """

    def __init__(self, inner: Dynaconf) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_frozen", False)

    def _freeze(self) -> None:
        object.__setattr__(self, "_frozen", True)

    def set(self, *args: object, **kwargs: object) -> None:
        if object.__getattribute__(self, "_frozen"):
            raise ConfigImmutabilityError(
                "Cannot mutate settings after config.initialize() has been called."
            )
        object.__getattribute__(self, "_inner").set(*args, **kwargs)

    def update(self, *args: object, **kwargs: object) -> None:
        if object.__getattribute__(self, "_frozen"):
            raise ConfigImmutabilityError(
                "Cannot mutate settings after config.initialize() has been called."
            )
        object.__getattribute__(self, "_inner").update(*args, **kwargs)

    def get(self, *args: object, **kwargs: object) -> object:
        return object.__getattribute__(self, "_inner").get(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __repr__(self) -> str:
        frozen = object.__getattribute__(self, "_frozen")
        inner = object.__getattribute__(self, "_inner")
        return f"<_FrozenSettings frozen={frozen} inner={inner!r}>"


def _make_settings() -> _FrozenSettings:
    return _FrozenSettings(
        Dynaconf(
            envvar_prefix="LOONY_DEV",
            settings_files=_CONFIG_FILES,
            environments=False,
            load_dotenv=False,
        )
    )


settings: _FrozenSettings = _make_settings()

_initialized: bool = False


def initialize(overrides: dict) -> None:
    """Apply CLI overrides (non-None, non-sentinel values only), then lock.

    Must be called exactly once, from cli.py, before any module reads
    ``settings``.  After this call, ``settings`` is read-only; any attempt to
    mutate it raises ``ConfigImmutabilityError``.

    Parameters
    ----------
    overrides:
        Key→value pairs from CLI options.  Keys may use dot-notation for
        nested sections (e.g. ``"worker.interval"``).  Values of ``None``
        are silently ignored so that un-provided CLI flags fall through to
        the lower-priority sources.
    """
    global _initialized
    if _initialized:
        raise RuntimeError(
            "config.initialize() has already been called. "
            "It must only be invoked once per process, from cli.py."
        )
    _initialized = True

    for key, value in overrides.items():
        if value is not None:
            settings.set(key, value)

    settings._freeze()


def _reset_for_testing() -> None:
    """Reset module state so tests can call initialize() more than once.

    NOT for production use.  Call this in test setUp / teardown to get a
    fresh, unlocked settings object between test cases.
    """
    global settings, _initialized

    _initialized = False
    settings = _make_settings()
