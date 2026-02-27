from __future__ import annotations

from dataclasses import dataclass, field


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
    mergeable: str | None = None


@dataclass
class TaskResult:
    success: bool
    output: str
    summary: str
