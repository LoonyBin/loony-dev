"""Deterministic session IDs for Claude Code session continuity.

Tasks that share the same (repo, key) pair will produce the same UUID v5
session ID, allowing planning and implementation stages to share a single
Claude Code conversation.
"""
from __future__ import annotations

import uuid

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
