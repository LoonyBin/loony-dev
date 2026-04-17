"""Issue model with Active Record pattern for GitHub issues."""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

from loony_dev.github.content import Content
from loony_dev.github.repo import parse_datetime

if TYPE_CHECKING:
    from loony_dev.github.comment import Comment
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
        updated_at=None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        _repo: Repo,
    ) -> None:
        super().__init__(number=number, _repo=_repo)
        self.title = title if isinstance(title, Content) else Content(title)
        self.body = body if isinstance(body, Content) else Content(body)
        self.author = author
        self.updated_at = updated_at
        self.labels: list[str] = labels or []
        self.assignees: list[str] = assignees or []

    # --- Class-level reads ---

    @classmethod
    def get(cls, number: int, *, repo: Repo) -> Issue:
        """Fetch a single issue by number."""
        data = repo.client.gh_json(
            "issue", "view", str(number),
            "--json", "number,title,body,labels,author,updatedAt,assignees",
        )
        return cls._from_api(data, repo)

    @classmethod
    def list(cls, *, label: str, repo: Repo) -> list[Issue]:
        """List open issues with the given label."""
        data = repo.client.gh_json(
            "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,author,updatedAt,assignees",
        )
        result = [cls._from_api(item, repo) for item in data]
        logger.debug("Issue.list(label=%r) returned %d issue(s)", label, len(result))
        return result

    @classmethod
    def _from_api(cls, data: dict, repo: Repo) -> Issue:
        return cls(
            number=data["number"],
            title=Content(data.get("title", "")),
            body=Content(data.get("body", "")),
            author=data.get("author", {}).get("login", ""),
            updated_at=parse_datetime(data.get("updatedAt")),
            labels=[l["name"] for l in data.get("labels", [])],
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
        return bool(self.assignees) and not self.is_assigned_to(username)

    def __repr__(self) -> str:
        return f"Issue(#{self.number}, {self.title!r})"
