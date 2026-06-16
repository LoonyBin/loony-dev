"""Scratch context files for slash-command turns (issue #166).

Worker turns are short ``/<command> <path>`` invocations: instead of pasting a
multi-kilobyte prompt body into the persistent session, the agent writes the
structured context to a JSON file and sends the *path* as the command argument.
The slash command body (under ``<repo>/.claude/commands/``) reads and parses the
JSON at that path. This keeps the transcript readable and makes the bot's turns
byte-for-byte reproducible by an operator typing ``/<command> /path/to.json``.

Files live in a per-task scratch directory under the system temp dir (outside the
git worktree) so they never get swept into ``git status`` or a commit. The
directory is removed best-effort once the session closes.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Root for all per-task scratch dirs, under the system temp dir.
_CONTEXT_ROOT = "loony-context"

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class CommandNotInstalledError(RuntimeError):
    """Raised when a required slash command is missing from the worktree.

    #165 installs the bundled commands into every worker checkout/worktree, so a
    missing command file is config drift, not a normal condition. We surface it
    loudly (mirroring the worker's ``verify_hooks`` posture) rather than silently
    falling back to an inline prompt body.
    """


def _safe(task_key: str) -> str:
    """Return a filesystem-safe directory name for *task_key*.

    ``task_key`` already comes from ``Task.worktree_key`` (``issue-166``,
    ``pr-12-ci``, …), which is filesystem-safe by construction; this is a
    defensive normalisation so a stray separator can never escape the root.
    """
    cleaned = _UNSAFE_RE.sub("-", task_key).strip("-")
    # A name of only dots ('.' / '..') would resolve outside the root.
    if cleaned in ("", ".", ".."):
        return "task"
    return cleaned


def _context_dir(task_key: str) -> Path:
    return Path(tempfile.gettempdir()) / _CONTEXT_ROOT / _safe(task_key)


def write_context_file(command: str, payload: dict, *, task_key: str) -> Path:
    """Serialize *payload* to JSON in a per-task scratch dir; return its path.

    The returned path is absolute so it resolves from the session's cwd
    regardless of where the worktree lives.
    """
    base = _context_dir(task_key)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{command}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    resolved = path.resolve()
    logger.debug("Wrote context file for /%s at %s", command, resolved)
    return resolved


def cleanup_context_dir(task_key: str | None) -> None:
    """Best-effort removal of the per-task scratch dir."""
    if not task_key:
        return
    shutil.rmtree(_context_dir(task_key), ignore_errors=True)
