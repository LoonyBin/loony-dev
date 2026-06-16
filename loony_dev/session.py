"""Deterministic session IDs and on-disk transcript paths for Claude Code.

Tasks that share the same (repo, key) pair will produce the same UUID v5
session ID, allowing planning and implementation stages to share a single
Claude Code conversation.

The transcript-path helpers (:func:`jsonl_path_for` and friends) live here —
next to :func:`session_id_for` — rather than in
:mod:`loony_dev.agents.claude_session` so the web/dashboard layer can compute a
session's JSONL path without importing the PTY/pexpect-heavy agent module
(issue #202). ``claude_session`` re-exports them for backward compatibility.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

# Fixed namespace derived from a project-specific URL.  Changing this
# invalidates all previously generated session IDs (existing sessions
# will be abandoned, not corrupted — the next run simply starts fresh).
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://loonybin.dev/session")


def session_id_for(repo: str, key: str) -> str:
    """Return a deterministic UUID v5 string for the given repo and key.

    >>> session_id_for("LoonyBin/target-repo", "issue:42")  # doctest: +SKIP
    'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'

    The same (repo, key) always produces the same UUID, so independently
    computed session IDs for planning and implementation match automatically.
    """
    return str(uuid.uuid5(_NAMESPACE, f"{repo}:{key}"))


def project_slug(cwd: Path) -> str:
    """Return Claude's transcript-directory slug for *cwd*.

    Claude replaces every non-alphanumeric character of the absolute working
    directory with ``-`` (e.g. ``/home/u/loony-dev`` → ``-home-u-loony-dev``).
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(str(cwd)))


def claude_config_dir() -> Path:
    """Return the Claude config root (honours ``CLAUDE_CONFIG_DIR``)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def jsonl_path_for(cwd: Path, session_id: str) -> Path:
    """Compute the JSONL transcript path for *session_id* run in *cwd*."""
    return claude_config_dir() / "projects" / project_slug(cwd) / f"{session_id}.jsonl"
