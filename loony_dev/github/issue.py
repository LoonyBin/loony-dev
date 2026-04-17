"""Issue model with Active Record pattern for GitHub issues."""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING

from loony_dev.github.content import Content
from loony_dev.github.repo import parse_datetime

if TYPE_CHECKING:
    from loony_dev.github.comment import Comment
    from loony_dev.github.milestone import Milestone
    from loony_dev.github.pull_request import PullRequest
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GitHubItem — shared write operations for Issues and PRs
# ---------------------------------------------------------------------------


class GitHubItem:
    """Base class for Issue and PullRequest — shared write operations.

    Both issues and PRs use ``gh issue edit`` / ``gh issue comment`` for
    labels, assignments, and comments.
    """

    def __init__(self, *, number: int, _repo: Repo) -> None:
        self.number = number
        self._repo = _repo

    def add_comment(self, body: str) -> None:
        """Post a comment on this issue or PR."""
        from loony_dev.models import truncate_for_log

        logger.debug("add_comment(#%d): %s", self.number, truncate_for_log(body))
        self._repo.client.gh("issue", "comment", str(self.number), "--body", body)

    def add_label(self, label: str) -> None:
        try:
            self._repo.client.gh("issue", "edit", str(self.number), "--add-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to add label '%s' to #%d", label, self.number)

    def remove_label(self, label: str) -> None:
        try:
            self._repo.client.gh("issue", "edit", str(self.number), "--remove-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to remove label '%s' from #%d", label, self.number)

    def assign(self, user: str = "@me") -> None:
        try:
            self._repo.client.gh("issue", "edit", str(self.number), "--add-assignee", user)
        except subprocess.CalledProcessError:
            logger.warning("Failed to assign %s to #%d", user, self.number)


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


class Issue(GitHubItem):
    """Active Record model for a GitHub issue."""

    def __init__(
        self,
        *,
        number: int,
        title: Content | str = "",
        body: Content | str = "",
        author: str = "",
        updated_at: datetime | None = None,
        created_at: datetime | None = None,
        labels: list[str] | None = None,
        milestone: Milestone | None = None,
        assignees: list[str] | None = None,
        _repo: Repo,
    ) -> None:
        super().__init__(number=number, _repo=_repo)
        self.title = title if isinstance(title, Content) else Content(title)
        self.body = body if isinstance(body, Content) else Content(body)
        self.author = author
        self.updated_at = updated_at
        self.created_at = created_at
        self.labels: list[str] = labels or []
        self.milestone: Milestone | None = milestone
        self.assignees: list[str] = assignees or []

    # --- Class-level reads ---

    @classmethod
    def get(cls, number: int, *, repo: Repo) -> Issue:
        """Fetch a single issue by number."""
        data = repo.client.gh_json(
            "issue", "view", str(number),
            "--json", "number,title,body,labels,author,updatedAt,createdAt,milestone,assignees",
        )
        return cls._from_api(data, repo)

    @classmethod
    def list(cls, *, label: str, repo: Repo) -> IssueCollection:
        """List open issues with the given label."""
        data = repo.client.gh_json(
            "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,author,updatedAt,createdAt,milestone,assignees",
        )
        result = IssueCollection(cls._from_api(item, repo) for item in data)
        logger.debug("Issue.list(label=%r) returned %d issue(s)", label, len(result))
        return result

    @classmethod
    def _from_api(cls, data: dict, repo: Repo, *, sanitize_content: bool = False) -> Issue:
        from loony_dev.github.milestone import Milestone

        title_str = data.get("title", "")
        body_str = data.get("body") or ""
        if sanitize_content:
            from loony_dev.sanitize import sanitize_user_content
            title_str = sanitize_user_content(title_str).text
            body_str = sanitize_user_content(body_str).text

        ms_data = data.get("milestone")
        milestone = (
            Milestone(
                number=ms_data.get("number", 0),
                title=ms_data.get("title", ""),
                # gh issue list returns dueOn (camelCase); gh api milestones returns due_on
                due_on=parse_datetime(ms_data.get("dueOn") or ms_data.get("due_on")),
            )
            if ms_data
            else None
        )

        return cls(
            number=data["number"],
            title=Content(title_str, safe=sanitize_content),
            body=Content(body_str, safe=sanitize_content),
            author=data.get("author", {}).get("login", ""),
            updated_at=parse_datetime(data.get("updatedAt")),
            created_at=parse_datetime(data.get("createdAt")),
            labels=[l["name"] for l in data.get("labels", [])],
            milestone=milestone,
            assignees=[a["login"] for a in data.get("assignees", [])],
            _repo=repo,
        )

    # --- Instance reads ---

    @property
    def comments(self) -> list[Comment]:
        """Fetch comments for this issue."""
        from loony_dev.github.comment import Comment

        return Comment.list_for_issue(self.number, repo=self._repo)

    def find_pr(self) -> PullRequest | None:
        """Return the open PR referencing this issue, or None."""
        from loony_dev.github.pull_request import PullRequest

        for search_args in [
            ["--search", f"#{self.number} in:title"],
            ["--search", f"#{self.number}"],
        ]:
            try:
                data = self._repo.client.gh_json(
                    "pr", "list",
                    "--state", "open",
                    *search_args,
                    "--json", "number,createdAt",
                )
                if data:
                    sorted_prs = sorted(data, key=lambda p: p.get("createdAt", ""), reverse=True)
                    pr_number = sorted_prs[0]["number"]
                    return PullRequest.get(pr_number, repo=self._repo)
            except subprocess.CalledProcessError:
                logger.warning("gh pr list search failed for issue #%d", self.number)
        return None

    def is_assigned_to(self, username: str) -> bool:
        return any(login == username for login in self.assignees)

    def has_other_assignee(self, username: str) -> bool:
        """Return True if the issue is assigned to at least one person who is not *username*."""
        return any(login != username for login in self.assignees)

    def __repr__(self) -> str:
        return f"Issue(#{self.number}, {self.title!r})"


# ---------------------------------------------------------------------------
# IssueCollection — list subclass with chainable filters
# ---------------------------------------------------------------------------


class IssueCollection(list):
    """An ordered list of Issue instances with chainable filter methods.

    Inherits from ``list`` so it can be used anywhere a plain list is expected.
    """

    @classmethod
    def fetch_open(cls, *, repo: Repo) -> IssueCollection:
        """Fetch all open issues (tick-cached).

        User-controlled string fields are sanitized for prompt injection.
        Use ``repo.issues.invalidate()`` to force a fresh fetch within a tick.
        """
        cached = repo._tick_cache.get("issues_open_all")
        if cached is not None:
            logger.debug("IssueCollection.fetch_open tick-cache hit (%d issues)", len(cached))
            return cached

        try:
            data = repo.client.gh_json(
                "issue", "list",
                "--state", "open",
                "--json", "number,title,body,labels,milestone,createdAt,updatedAt,author,assignees",
                "--limit", "500",
            )
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch open issues for %s", repo.name)
            data = []

        if not isinstance(data, list):
            data = []

        result = cls(Issue._from_api(item, repo, sanitize_content=True) for item in data)
        repo._tick_cache["issues_open_all"] = result
        logger.debug("IssueCollection.fetch_open fetched %d open issue(s)", len(result))
        return result

    def where(self, *, label: str | None = None, milestone: str | None = None) -> IssueCollection:
        """Return a new IssueCollection filtered by the given criteria."""
        result = self
        if label is not None:
            result = [i for i in result if label in i.labels]
        if milestone is not None:
            result = [i for i in result if i.milestone and i.milestone.title == milestone]
        return IssueCollection(result)

    def numbers(self) -> set[int]:
        """Return the set of issue numbers in this collection."""
        return {i.number for i in self}


# ---------------------------------------------------------------------------
# _IssueQuery — proxy returned by repo.issues
# ---------------------------------------------------------------------------


class _IssueQuery:
    """Query proxy for issues on a repository.

    Accessed via ``repo.issues``; supports ``repo.issues.open`` and
    ``repo.issues.invalidate()``.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    @property
    def open(self) -> IssueCollection:
        """Return all open issues as an IssueCollection (tick-cached)."""
        return IssueCollection.fetch_open(repo=self._repo)

    def invalidate(self) -> None:
        """Discard the cached open issues list."""
        self._repo._tick_cache.pop("issues_open_all", None)
