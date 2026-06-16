"""Framework-agnostic data layer for installing/updating skills and commands.

This is the first *mutating* part of the web dashboard (issue #133). It lets the
dashboard install, update and remove the two kinds of Claude entry that a
freshly-spawned Claude session picks up automatically:

* **skills**   — a directory ``<claude-dir>/skills/<name>/SKILL.md``
* **commands** — a single file ``<claude-dir>/commands/<name>.md``

Two scopes are supported:

* **global** — ``<claude-dir>`` = ``~/.claude`` (injected as ``global_root`` so
  tests can point it at a temp tree).
* **repo**   — ``<claude-dir>`` = ``<base_dir>/<owner>/<repo>/.claude`` (the repo
  checkout created by ``supervisor.ensure_repo_checked_out``).

As in :mod:`loony_dev.web.services`, no FastAPI imports live here so the route
layer stays a thin wrapper and these functions are directly unit-testable. All
path-safety lives in this module (``_validate_name`` + a containment check that
mirrors ``services._safe_repo_log_path``).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loony_dev.commands import MANAGED_MARKER

CLAUDE_DIR_NAME = ".claude"

# Cap how much of each content file the metadata reader pulls in. Frontmatter
# plus the managed marker live in the first few lines, so a few KiB is ample and
# keeps the per-file read during listing bounded.
_META_HEAD_BYTES = 8192

# Best-effort lifecycle phase for the loony-dev managed commands. There is no
# `phase` frontmatter field today (it is a mockup invention), so cards fall back
# to this name→phase table when frontmatter omits it. Unknown names yield None
# and the card simply hides the phase chip.
_KNOWN_COMMAND_PHASES: dict[str, str] = {
    "plan-issue": "planning",
    "implement-issue": "development",
    "fix-ci": "ci",
    "fix-review": "review",
    "address-reviews": "review",
    "resolve-conflicts": "conflict",
    "cleanup-stuck": "stuck",
}

# Pull a "use when …" / "triggers on …" clause out of a description when no
# explicit `trigger` frontmatter field is present.
_TRIGGER_RE = re.compile(r"(?:use when|triggers on)\b[\s:—-]*(.+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Kind table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Kind:
    """Describes one entry type and its on-disk layout."""

    subdir: str            # container under <claude-dir> ("skills" / "commands")
    is_dir: bool           # True => directory-with-SKILL.md, False => single .md
    content_name: str | None  # filename inside the entry dir (skills only)


KINDS: dict[str, Kind] = {
    "skills": Kind(subdir="skills", is_dir=True, content_name="SKILL.md"),
    "commands": Kind(subdir="commands", is_dir=False, content_name=None),
}


# ---------------------------------------------------------------------------
# Data view (plain dataclass, JSON-serialisable via asdict)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryView:
    name: str
    path: str                 # path to SKILL.md or <name>.md
    size: int                 # bytes of the markdown content file
    modified_at: str | None   # ISO-8601 UTC mtime of the content file
    # Card metadata, derived best-effort from the content file's head. All
    # optional: a frontmatter-less or unreadable file lists with these None
    # (listing never raises). The read/write/delete paths are unaffected.
    description: str | None = None   # frontmatter `description`
    owner: str | None = None         # "trixy" (managed marker present) / "capo"
    trigger: str | None = None       # "triggers on …" clause, when derivable
    phase: str | None = None         # lifecycle phase, when derivable


# ---------------------------------------------------------------------------
# Exceptions (parallel to services.LogNotFoundError)
# ---------------------------------------------------------------------------

class EntryError(Exception):
    """Invalid kind/name/scope/path — maps to HTTP 400."""


class EntryNotFoundError(Exception):
    """Requested entry does not exist — maps to HTTP 404."""


# ---------------------------------------------------------------------------
# Path resolution + safety
# ---------------------------------------------------------------------------

def _resolve_kind(kind: str) -> Kind:
    try:
        return KINDS[kind]
    except KeyError:
        raise EntryError(f"unknown kind: {kind!r}") from None


def _validate_name(name: str) -> None:
    """Reject any name that could escape its container directory.

    Rejects empty, ``.``, ``..`` and anything containing a path separator or a
    null byte. This satisfies the acceptance criterion that ``{name}`` path
    traversal (anything containing ``/`` or ``..``) is rejected.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise EntryError(f"invalid name: {name!r}")


def _iso_mtime(path: Path) -> str | None:
    """Return *path*'s mtime as a UTC ISO-8601 string, or None if unavailable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _claude_dir(global_root: Path, base_dir: Path, scope: str,
                owner: str | None, repo: str | None) -> Path:
    """Resolve the ``<claude-dir>`` root for *scope*.

    ``global`` → *global_root*; ``repo`` → ``<base_dir>/<owner>/<repo>/.claude``
    (with ``owner``/``repo`` validated by the same rules as entry names).
    """
    if scope == "global":
        return Path(global_root)
    if scope == "repo":
        if not owner or not repo:
            raise EntryError("scope 'repo' requires owner and repo")
        _validate_name(owner)
        _validate_name(repo)
        return Path(base_dir) / owner / repo / CLAUDE_DIR_NAME
    raise EntryError(f"invalid scope: {scope!r}")


def _entry_paths(kind: Kind, claude_dir: Path, name: str) -> tuple[Path, Path]:
    """Return ``(entry_dir, content_path)`` for *name* under *claude_dir*.

    For skills the entry is the directory ``skills/<name>/`` whose content file
    is ``SKILL.md``; for commands the entry "dir" is ``commands/`` and the
    content file is ``<name>.md``.
    """
    container = claude_dir / kind.subdir
    if kind.is_dir:
        entry_dir = container / name
        content_path = entry_dir / kind.content_name  # type: ignore[operator]
    else:
        entry_dir = container
        content_path = container / f"{name}.md"
    return entry_dir, content_path


def _resolve_paths(kind_name: str, name: str, *, global_root: Path, base_dir: Path,
                   scope: str, owner: str | None, repo: str | None) -> tuple[Kind, Path, Path]:
    """Validate inputs and return ``(kind, entry_dir, content_path)``.

    Applies a belt-and-suspenders containment check (mirroring
    ``services._safe_repo_log_path``): the resolved scope root must be an
    ancestor of the resolved content path, guarding against symlink/``..``
    escapes even though ``_validate_name`` already blocks segment traversal.
    """
    kind = _resolve_kind(kind_name)
    _validate_name(name)
    claude_dir = _claude_dir(global_root, base_dir, scope, owner, repo)
    entry_dir, content_path = _entry_paths(kind, claude_dir, name)

    root = claude_dir.resolve()
    resolved = content_path.resolve()
    if root != resolved and root not in resolved.parents:
        raise EntryError("resolved path escapes the claude directory")
    return kind, entry_dir, content_path


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def list_entries(kind_name: str, *, global_root: Path, base_dir: Path,
                 scope: str = "global", owner: str | None = None,
                 repo: str | None = None) -> list[EntryView]:
    """Return every installed entry of *kind_name* in the given scope.

    Returns ``[]`` (never raises) when the container directory is absent.
    Results are sorted by name.
    """
    kind = _resolve_kind(kind_name)
    claude_dir = _claude_dir(global_root, base_dir, scope, owner, repo)
    container = claude_dir / kind.subdir
    if not container.is_dir():
        return []

    root = claude_dir.resolve()
    is_command = not kind.is_dir
    views: list[EntryView] = []
    if kind.is_dir:
        for child in sorted(container.iterdir()):
            content_path = child / kind.content_name  # type: ignore[operator]
            if child.is_dir() and content_path.is_file() and _contained(root, content_path):
                views.append(_view(child.name, content_path, is_command))
    else:
        for content_path in sorted(container.glob("*.md")):
            if content_path.is_file() and _contained(root, content_path):
                views.append(_view(content_path.stem, content_path, is_command))
    return views


def _contained(root: Path, content_path: Path) -> bool:
    """True if *content_path*'s resolved target stays within *root*.

    Mirrors the containment check in :func:`_resolve_paths` so a symlinked entry
    whose target escapes the ``.claude`` root is never surfaced via listing.
    """
    resolved = content_path.resolve()
    return root == resolved or root in resolved.parents


def _read_head(content_path: Path) -> str:
    """Return the first ``_META_HEAD_BYTES`` of *content_path* as text, or ""."""
    try:
        with content_path.open("rb") as fh:
            raw = fh.read(_META_HEAD_BYTES)
    except OSError:
        return ""
    # Read in binary so the cap bounds *bytes* (multibyte UTF-8 would let a
    # text-mode char cap read past the intended I/O window), then decode.
    return raw.decode("utf-8", errors="replace")


def _parse_frontmatter(head: str) -> dict[str, str]:
    """Parse a leading ``---``-fenced YAML block into a flat ``key: value`` dict.

    Dependency-free (PyYAML is not a project dependency): only simple top-level
    ``key: value`` scalar lines are understood, which covers the frontmatter the
    skills/commands use (``description``, ``argument-hint``, ``name``, …). A file
    without a leading fence yields ``{}``.
    """
    if not head.startswith("---\n") and not head.startswith("---\r\n"):
        return {}
    lines = head.splitlines()
    # Require a closing fence within the head. Without it the block is truncated
    # or malformed, and parsing onward would mis-read body lines as metadata.
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if not line[:1].strip() and line.strip():
            # Indented (nested) line — skip; only top-level scalars are parsed.
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if not key or key.startswith("#"):
            continue  # Skip blanks and comments.
        fields[key] = value.strip().strip("'\"")
    return fields


def _derive_metadata(name: str, head: str, is_command: bool
                     ) -> tuple[str | None, str, str | None, str | None]:
    """Return ``(description, owner, trigger, phase)`` derived from *head*.

    ``owner`` is always concrete: ``"trixy"`` when the managed marker is present
    in the head (a loony-dev-installed file), else ``"capo"`` (hand-authored).
    The remaining fields are surfaced when derivable and ``None`` otherwise.
    """
    fm = _parse_frontmatter(head)
    description = fm.get("description") or None
    owner = "trixy" if MANAGED_MARKER in head else "capo"

    trigger = fm.get("trigger") or None
    if trigger is None and description:
        match = _TRIGGER_RE.search(description)
        if match:
            trigger = match.group(1).strip() or None

    # The known-command phase map keys off command names, so it applies only to
    # the commands kind — never to a skill that merely shares a command's name.
    phase = fm.get("phase")
    if phase is None and is_command:
        phase = _KNOWN_COMMAND_PHASES.get(name)
    return description, owner, trigger, phase


def _view(name: str, content_path: Path, is_command: bool) -> EntryView:
    try:
        size = content_path.stat().st_size
    except OSError:
        size = 0
    description, owner, trigger, phase = _derive_metadata(
        name, _read_head(content_path), is_command)
    return EntryView(
        name=name,
        path=str(content_path),
        size=size,
        modified_at=_iso_mtime(content_path),
        description=description,
        owner=owner,
        trigger=trigger,
        phase=phase,
    )


def read_entry(kind_name: str, name: str, *, global_root: Path, base_dir: Path,
               scope: str = "global", owner: str | None = None,
               repo: str | None = None) -> str:
    """Return the markdown content of an entry; raise EntryNotFoundError if missing."""
    _kind, _entry_dir, content_path = _resolve_paths(
        kind_name, name, global_root=global_root, base_dir=base_dir,
        scope=scope, owner=owner, repo=repo,
    )
    try:
        return content_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise EntryNotFoundError(f"no {kind_name} entry: {name}") from exc


def write_entry(kind_name: str, name: str, content: str, *, global_root: Path,
                base_dir: Path, scope: str = "global", owner: str | None = None,
                repo: str | None = None) -> EntryView:
    """Create or overwrite an entry's content file (idempotent) and return its view."""
    kind, entry_dir, content_path = _resolve_paths(
        kind_name, name, global_root=global_root, base_dir=base_dir,
        scope=scope, owner=owner, repo=repo,
    )
    entry_dir.mkdir(parents=True, exist_ok=True)
    content_path.write_text(content, encoding="utf-8")
    return _view(name, content_path, not kind.is_dir)


def delete_entry(kind_name: str, name: str, *, global_root: Path, base_dir: Path,
                 scope: str = "global", owner: str | None = None,
                 repo: str | None = None) -> None:
    """Remove an entry; raise EntryNotFoundError if it does not exist.

    Skills delete the whole ``skills/<name>/`` directory; commands unlink the
    single ``<name>.md`` file.
    """
    kind, entry_dir, content_path = _resolve_paths(
        kind_name, name, global_root=global_root, base_dir=base_dir,
        scope=scope, owner=owner, repo=repo,
    )
    if kind.is_dir:
        # Only delete a *canonical* skill dir (one containing SKILL.md); never
        # rmtree an unrelated directory that merely shares the entry name.
        if entry_dir.is_symlink() or not entry_dir.is_dir() or not content_path.is_file():
            raise EntryNotFoundError(f"no {kind_name} entry: {name}")
        shutil.rmtree(entry_dir)
    else:
        try:
            content_path.unlink()
        except FileNotFoundError as exc:
            raise EntryNotFoundError(f"no {kind_name} entry: {name}") from exc
