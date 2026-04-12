from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def truncate_for_log(text: str, head: int = 300, tail: int = 200) -> str:
    """Return text trimmed to head+tail chars for log readability."""
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n... [truncated] ...\n{text[-tail:]}"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str = ""
    updated_at: datetime | None = None


@dataclass
class Comment:
    author: str
    body: str
    created_at: str
    path: str | None = None
    line: int | None = None


@dataclass
class PullRequest:
    number: int
    branch: str
    title: str
    new_comments: list[Comment] = field(default_factory=list)
    mergeable: str | None = None
    updated_at: datetime | None = None
    head_sha: str = ""


@dataclass
class CheckRun:
    name: str
    status: str        # "completed" | "in_progress" | "queued"
    conclusion: str    # "failure" | "success" | "cancelled" | "timed_out" | ...
    details_url: str   # Link to the CI run log


class RateLimitedError(Exception):
    """Raised when an agent task fails due to rate limiting / quota exhaustion.

    Using a distinct exception type lets ``on_failure`` handlers restore GitHub
    state (labels, assignment) without posting an alarming error comment — the
    quota will clear on its own.
    """


@dataclass
class TaskResult:
    success: bool
    output: str
    summary: str
    post_summary: bool = True
    rate_limited: bool = False
