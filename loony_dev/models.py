from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass
class TaskResult:
    success: bool
    output: str
    summary: str
