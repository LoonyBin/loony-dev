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


def _normalize_failure_body(body: str) -> str:
    """Strip the leading marker line from a failure comment body.

    Removes ``<!-- ... -->`` lines from the top so that comparisons between
    failure comments are stable even when the marker encodes a dynamic value
    such as a ``last-seen`` timestamp.
    """
    lines = body.strip().splitlines()
    while lines and lines[0].strip().startswith("<!--") and lines[0].strip().endswith("-->"):
        lines = lines[1:]
    return "\n".join(lines).strip()


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

    def get_comments(self) -> list[Comment]:
        """Return all timeline comments on this item, fetched fresh from the API."""
        from loony_dev.github.comment import Comment

        return Comment.list_for_issue(self.number, repo=self._repo)

    def _recent_bot_failure_comments(self, bot_name: str, n: int) -> list[Comment]:
        """Return the last *n* bot-authored failure comments on this item."""
        from loony_dev.tasks.base import CI_FAILURE_MARKER, FAILURE_MARKER_PREFIX

        all_comments = self.get_comments()
        failure_comments = [
            c for c in all_comments
            if c.author == bot_name and (
                FAILURE_MARKER_PREFIX in c.body or CI_FAILURE_MARKER in c.body
            )
        ]
        return failure_comments[-n:]

    def check_and_post_failure(
        self, failure_body: str, bot_name: str, n: int, fallback_owner: str
    ) -> bool:
        """Decide whether to post a regular failure comment or trigger in-error.

        Compares the last *n* bot failure comments against *failure_body*
        (ignoring dynamic marker attributes).  If all *n* match, applies the
        ``in-error`` label and posts a sleeping notice tagging the item owner.

        Returns True if the in-error path was taken, False otherwise.
        """
        from loony_dev.tasks.base import IN_ERROR_MARKER

        normalized_current = _normalize_failure_body(failure_body)
        recent = self._recent_bot_failure_comments(bot_name, n)

        if len(recent) >= n and all(
            _normalize_failure_body(str(c.body)) == normalized_current
            for c in recent
        ):
            item_owner = getattr(self, "author", "") or fallback_owner
            self.add_label("in-error")
            self.add_comment(
                f"{IN_ERROR_MARKER}\n\n"
                f"I've encountered the same failure {n + 1} time(s) in a row and will stop "
                f"retrying. @{item_owner}, please review.\n\n"
                f"<details><summary>Repeated failure</summary>\n\n"
                f"{failure_body}\n\n</details>"
            )
            logger.warning(
                "#%d marked in-error after %d identical consecutive failure(s)",
                self.number, n + 1,
            )
            return True

        self.add_comment(failure_body)
        return False


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
        return any(login != username for login in self.assignees)

    def __repr__(self) -> str:
        return f"Issue(#{self.number}, {self.title!r})"
