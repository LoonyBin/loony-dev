from __future__ import annotations

from dataclasses import dataclass, field


def truncate_for_log(text: str, head: int = 300, tail: int = 200) -> str:
    """Return text trimmed to head+tail chars for log readability."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n... [truncated] ...\n{text[-tail:]}"


class RateLimitedError(Exception):
    """Raised when an agent task fails due to rate limiting / quota exhaustion.

    Using a distinct exception type lets ``on_failure`` handlers restore GitHub
    state (labels, assignment) without posting an alarming error comment — the
    quota will clear on its own.
    """


class GitError(Exception):
    """Raised on a git command failure unrelated to hooks."""


class HookFailureError(Exception):
    """Raised when a git commit or push is rejected by a pre-commit/pre-push hook."""

    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


@dataclass
class TaskResult:
    success: bool
    output: str
    summary: str
    post_summary: bool = True
    rate_limited: bool = False
