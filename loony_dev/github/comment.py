"""Comment and WarningComment models for GitHub issue/PR comments."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from loony_dev.github.content import Content

if TYPE_CHECKING:
    from loony_dev.sanitize import InjectionType
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


class Comment:
    """A GitHub issue or PR comment."""

    def __init__(
        self,
        *,
        author: str,
        body: Content | str,
        created_at: str,
        path: str | None = None,
        line: int | None = None,
    ) -> None:
        self.author = author
        self.body = body if isinstance(body, Content) else Content(body)
        self.created_at = created_at
        self.path = path
        self.line = line

    # --- Class-level reads ---

    @classmethod
    def list_for_issue(cls, number: int, *, repo: Repo) -> list[Comment]:
        """Get all comments on an issue, sorted by creation time."""
        data = repo.client.gh_json("issue", "view", str(number), "--json", "comments")
        if not isinstance(data, dict):
            return []
        comments = [cls._from_api(c, repo) for c in data.get("comments", [])]
        comments.sort(key=lambda c: c.created_at)
        logger.debug("Comment.list_for_issue(#%d) returned %d comment(s)", number, len(comments))
        return comments

    @classmethod
    def list_inline_for_pr(cls, pr_number: int, *, repo: Repo) -> list[Comment]:
        """Fetch inline review comments for a PR.

        Uses each comment's associated review ``submitted_at`` as the effective
        ``created_at`` timestamp.
        """
        import subprocess

        try:
            data = repo.client.gh_api(f"pulls/{pr_number}/comments")
            data_reviews = repo.client.gh_api(f"pulls/{pr_number}/reviews")
            review_submitted_at: dict[int, str] = {}
            if isinstance(data_reviews, list):
                review_submitted_at = {
                    r["id"]: r["submitted_at"]
                    for r in data_reviews
                    if r.get("submitted_at")
                }
            logger.debug(
                "Comment.list_inline_for_pr(#%d) fetched %d review(s)",
                pr_number, len(review_submitted_at),
            )
            if isinstance(data, list):
                comments = []
                for c in data:
                    author = c.get("user", {}).get("login", "")
                    body_text = c.get("body", "")
                    safe = (author == repo.bot_name)
                    review_id = c.get("pull_request_review_id")
                    effective_ts = (
                        review_submitted_at.get(review_id)
                        if review_id is not None
                        else None
                    ) or c.get("created_at", "")
                    comments.append(Comment(
                        author=author,
                        body=Content(body_text, safe=safe),
                        created_at=effective_ts,
                        path=c.get("path"),
                        line=c.get("line"),
                    ))
                logger.debug("Comment.list_inline_for_pr(#%d) returned %d comment(s)", pr_number, len(comments))
                return comments
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch inline review comments for PR #%d", pr_number)
        return []

    @classmethod
    def _from_api(cls, data: dict, repo: Repo) -> Comment:
        author = data.get("author", {}).get("login", "")
        body_text = data.get("body", "")
        safe = (author == repo.bot_name)
        return cls(
            author=author,
            body=Content(body_text, safe=safe),
            created_at=data.get("createdAt", ""),
        )

    def __repr__(self) -> str:
        return f"Comment(author={self.author!r}, body={self.body[:50]!r}...)"


class WarningComment(Comment):
    """A comment that warns maintainers about detected injection attempts.

    Subclass of ``Comment`` with a sentinel string for deduplication.
    Uses ``.exists()`` to check if an equivalent warning has already been
    posted, and ``.save()`` to post it if not.
    """

    SENTINEL_PREFIX = "<!-- loonybin-injection-warning field="

    def __init__(
        self,
        *,
        number: int,
        field_name: str,
        injections: list[InjectionType],
        _repo: Repo,
    ) -> None:
        self._number = number
        self._field_name = field_name
        self._injections = injections
        self._repo = _repo
        sentinel = f'{self.SENTINEL_PREFIX}"{field_name}" -->'
        injection_labels = ", ".join(f"`{i.value}`" for i in injections)
        body = (
            f"{sentinel}\n"
            "> [!WARNING]\n"
            "> **Potential prompt injection attempt detected.**\n"
            ">\n"
            f"> Hidden content was found in the **{field_name}** field of this item "
            f"(detected: {injection_labels}).\n"
            "> The hidden content was stripped before processing and did not reach the AI agent.\n"
            ">\n"
            "> This may indicate a malicious actor attempting to hijack the AI agent. "
            "> A human should review the original content of this item."
        )
        super().__init__(
            author=_repo.bot_name,
            body=Content(body, safe=True),
            created_at="",
        )

    def exists(self) -> bool:
        """Check if this warning has already been posted (raw comment scan).

        Uses the raw ``gh_json`` transport to avoid triggering sanitization.
        """
        sentinel = f'{self.SENTINEL_PREFIX}"{self._field_name}" -->'
        data = self._repo.client.gh_json(
            "issue", "view", str(self._number), "--json", "comments",
        )
        if not isinstance(data, dict):
            return False
        return any(sentinel in c.get("body", "") for c in data.get("comments", []))

    def save(self) -> None:
        """Post this warning comment if it doesn't already exist."""
        if self.exists():
            return
        try:
            self._repo.client.gh(
                "issue", "comment", str(self._number), "--body", str(self.body),
            )
        except Exception as exc:
            logger.warning(
                "Failed to post injection warning comment on #%d: %s",
                self._number, exc,
            )
