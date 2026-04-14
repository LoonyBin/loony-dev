from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github import Repo
    from loony_dev.models import TaskResult


# Prefix strings — match both old markers (no last-seen) and new markers (with last-seen).
# Use these for startswith() checks so both formats are recognized.
FAILURE_MARKER_PREFIX = "<!-- loony-failure"
SUCCESS_MARKER_PREFIX = "<!-- loony-success"

# Legacy fixed-string markers kept for backward compatibility (old markers have no last-seen).
FAILURE_MARKER = "<!-- loony-failure -->"
SUCCESS_MARKER = "<!-- loony-success -->"

CI_FAILURE_MARKER = "<!-- loony-ci-failure -->"

_LAST_SEEN_RE = re.compile(r"last-seen=([^\s>]+)")


def encode_marker(prefix: str, last_seen: str) -> str:
    """Produce e.g. '<!-- loony-success last-seen=2025-01-15T10:32:00Z -->'."""
    return f"{prefix} last-seen={last_seen} -->"


def decode_last_seen(marker_body: str) -> str | None:
    """Extract the last-seen timestamp from a marker comment body, or None."""
    m = _LAST_SEEN_RE.search(marker_body)
    return m.group(1) if m else None


class Task(ABC):
    """A unit of work dispatched to an agent."""

    task_type: str
    priority: int  # Lower number = higher priority; used to order discovery across tick

    @staticmethod
    @abstractmethod
    def discover(repo: Repo) -> Iterator[Task]:
        """Yield tasks of this type discovered from GitHub.  Called each tick."""
        ...

    @property
    def session_key(self) -> str | None:
        """Key for Claude Code session continuity.

        Tasks that return the same key (for the same repo) will share a
        Claude session, allowing context to carry over between stages
        (e.g. planning -> implementation).  Return ``None`` to use a
        fresh session each time.
        """
        return None

    @abstractmethod
    def describe(self) -> str:
        """Human/agent-readable description of work to do."""
        ...

    @abstractmethod
    def on_start(self, repo: Repo) -> None:
        """Called before agent execution. Update GitHub state (labels etc)."""
        ...

    @abstractmethod
    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        """Called after successful completion. Update GitHub state."""
        ...

    @abstractmethod
    def on_failure(self, repo: Repo, error: Exception) -> None:
        """Called on failure. Update GitHub state."""
        ...
