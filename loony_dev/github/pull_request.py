"""PullRequest model with Active Record pattern."""
from __future__ import annotations

import json as _json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

from loony_dev.github.content import Content
from loony_dev.github.issue import GitHubItem
from loony_dev.github.repo import CheckRunsCacheEntry, parse_datetime

if TYPE_CHECKING:
    from loony_dev.github.check_run import CheckRun
    from loony_dev.github.comment import Comment
    from loony_dev.github.issue import IssueCollection
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


class PullRequest(GitHubItem):
    """Active Record model for a GitHub pull request."""

    def __init__(
        self,
        *,
        number: int,
        branch: str = "",
        title: Content | str = "",
        body: Content | str = "",
        head_sha: str = "",
        mergeable: str | None = None,
        updated_at=None,
        labels: list[str] | None = None,
        comments: list[Comment] | None = None,
        reviews: list[Comment] | None = None,
        assignees: list[dict] | None = None,
        new_comments: list[Comment] | None = None,
        is_draft: bool = False,
        _repo: Repo,
    ) -> None:
        super().__init__(number=number, _repo=_repo)
        self.branch = branch
        self.title = title if isinstance(title, Content) else Content(title)
        self.body = body if isinstance(body, Content) else Content(body)
        self.head_sha = head_sha
        self.mergeable = mergeable
        self.updated_at = updated_at
        self.labels: list[str] = labels or []
        self.comments: list[Comment] = comments or []
        self.reviews: list[Comment] = reviews or []
        self.assignees: list[dict] = assignees or []
        self.new_comments: list[Comment] = new_comments or []
        self.is_draft = is_draft

    # --- Class-level reads ---

    @classmethod
    def get(cls, number: int, *, repo: Repo) -> PullRequest:
        """Fetch a single PR by number."""
        data = repo.client.gh_json(
            "pr", "view", str(number),
            "--json", "number,headRefName,headRefOid,title,body,comments,reviews,labels,mergeable,updatedAt,assignees,isDraft",
        )
        return cls._from_api(data, repo)

    @classmethod
    def list_open(cls, *, repo: Repo) -> list[PullRequest]:
        """Fetch all open PRs.

        Results are cached for the duration of the current tick (cleared by
        ``Repo.clear_tick_cache()`` at the start of each tick).
        """
        cached = repo._tick_cache.get("open_prs")
        if cached is not None:
            logger.debug("PullRequest.list_open() tick-cache hit (%d PRs)", len(cached))
            return cached
        data = repo.client.gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,headRefOid,title,body,comments,reviews,labels,mergeable,updatedAt,assignees,isDraft",
        )
        result = [cls._from_api(item, repo) for item in data]
        logger.debug("PullRequest.list_open() returned %d open PR(s)", len(result))
        repo._tick_cache["open_prs"] = result
        return result

    @classmethod
    def _from_api(cls, data: dict, repo: Repo) -> PullRequest:
        from loony_dev.github.comment import Comment

        pr_number = data["number"]
        comments = [
            Comment(
                author=c.get("author", {}).get("login", ""),
                body=Content(
                    c.get("body", ""),
                    safe=(c.get("author", {}).get("login", "") == repo.bot_name),
                ),
                created_at=c.get("createdAt", ""),
            )
            for c in data.get("comments", [])
        ]
        reviews = [
            Comment(
                author=r.get("author", {}).get("login", ""),
                body=Content(
                    r.get("body", ""),
                    safe=(r.get("author", {}).get("login", "") == repo.bot_name),
                ),
                created_at=r.get("submittedAt", ""),
            )
            for r in data.get("reviews", [])
        ]
        return cls(
            number=pr_number,
            branch=data.get("headRefName", ""),
            title=Content(data.get("title", "")),
            body=Content(data.get("body") or ""),
            head_sha=data.get("headRefOid", ""),
            mergeable=data.get("mergeable"),
            updated_at=parse_datetime(data.get("updatedAt")),
            labels=[label["name"] for label in data.get("labels", [])],
            comments=comments,
            reviews=reviews,
            assignees=data.get("assignees", []),
            is_draft=data.get("isDraft", False),
            _repo=repo,
        )

    # --- Instance reads ---

    def is_assigned_to(self, username: str) -> bool:
        """Return True if *username* is listed as an assignee."""
        return any(a.get("login", "") == username for a in self.assignees)

    @property
    def issues(self) -> IssueCollection:
        """Issues referenced in this PR's title, body, or branch name.

        Returns an :class:`IssueCollection` of stub :class:`Issue` instances
        (``number`` populated; other fields fetched lazily via
        :meth:`~loony_dev.github.issue.Issue.get` if needed).
        """
        from loony_dev.github.issue import Issue, IssueCollection

        numbers: list[int] = []
        seen: set[int] = set()

        def _collect(text: str) -> None:
            for m in re.finditer(r"#(\d+)", text):
                n = int(m.group(1))
                if n not in seen:
                    seen.add(n)
                    numbers.append(n)

        # Explicit #N references in the title
        _collect(str(self.title))

        # Keyword + #N references in the body (e.g. "Closes #123", "Fixes #45")
        _collect(str(self.body))

        # Numbers embedded in the branch name (e.g. feat/123-my-feature)
        for m in re.finditer(r"(?:^|[/-])(\d+)(?:[/-]|$)", self.branch):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                numbers.append(n)

        return IssueCollection(Issue(number=n, _repo=self._repo) for n in numbers)

    @property
    def merged_at(self):
        """Return the merged-at datetime if this PR has been merged, else ``None``."""
        try:
            output = self._repo.client.gh(
                "pr", "view", str(self.number),
                "--json", "state,merged,mergedAt",
                "-q", ".",
            )
            data = _json.loads(output)
            if data.get("merged"):
                return parse_datetime(data.get("mergedAt"))
        except (subprocess.CalledProcessError, ValueError):
            logger.warning("Failed to fetch merge state for PR #%d", self.number)
        return None

    @property
    def inline_comments(self) -> list[Comment]:
        """Fetch inline review comments for this PR."""
        from loony_dev.github.comment import Comment

        return Comment.list_inline_for_pr(self.number, repo=self._repo)

    @property
    def check_runs(self) -> list[CheckRun]:
        """Return completed failing check runs for this PR's head SHA."""
        from loony_dev.github.check_run import CheckRun

        return CheckRun.list_failing(self.head_sha, repo=self._repo)

    # --- PR-specific writes ---

    def add_reviewer(self, reviewer: str) -> None:
        """Request a review from *reviewer* on this PR."""
        try:
            self._repo.client.gh("pr", "edit", str(self.number), "--add-reviewer", reviewer)
            logger.debug("add_reviewer(#%d, %r) succeeded", self.number, reviewer)
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to add reviewer %r to PR #%d: %s", reviewer, self.number, e)
            raise

    def merge(self, method: str = "squash") -> bool:
        """Merge this PR using *method* (``squash``, ``merge``, or ``rebase``).

        Returns ``True`` on success and invalidates the issues and open-PRs caches.
        """
        try:
            self._repo.client.gh("pr", "merge", str(self.number), f"--{method}")
            logger.info("Merged PR #%d via %s", self.number, method)
            self._repo.issues.invalidate()
            self._repo.pull_requests.invalidate()
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to merge PR #%d: %s",
                self.number, ((exc.stderr or "") + (exc.stdout or "")).strip()[:300],
            )
            return False

    def __repr__(self) -> str:
        return f"PullRequest(#{self.number}, {self.title!r})"


# ---------------------------------------------------------------------------
# PullRequestCollection
# ---------------------------------------------------------------------------


class PullRequestCollection:
    """A collection proxy for pull requests on a repository."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    @property
    def open(self) -> list[PullRequest]:
        """Return all open PRs, tick-cached."""
        return PullRequest.list_open(repo=self._repo)

    def invalidate(self) -> None:
        """Discard the tick-cached open PR list."""
        self._repo._tick_cache.pop("open_prs", None)
