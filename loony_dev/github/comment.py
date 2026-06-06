"""Comment and WarningComment models for GitHub issue/PR comments."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from loony_dev.github.content import Content

if TYPE_CHECKING:
    from loony_dev.sanitize import InjectionType
    from loony_dev.github.repo import Repo

logger = logging.getLogger(__name__)


class CommentFetchError(Exception):
    """Raised when an item's comments cannot be fetched from GitHub.

    Callers MUST treat this as "comment state is unknown" — never as "the item
    has no comments." A transient ``gh`` failure (5xx, network blip, timeout)
    used to be swallowed into an empty list here, which downstream code read as
    absence: ``PlanningTask`` saw no existing plan and posted a duplicate plan on
    a dormant issue, and failure-comment dedup could double-post. Raising forces
    the caller to skip the tick and retry, rather than act on phantom emptiness.
    """


def _author_login(author: dict | None) -> str:
    """Extract a login from a GraphQL author node, appending ``[bot]`` for Bots.

    GitHub's REST API renders a Bot actor's login with a trailing ``[bot]``
    suffix (e.g. ``coderabbitai[bot]``); GraphQL's ``author { login }`` returns
    just ``coderabbitai``. Operators configure ``allowed_users`` with the REST
    form, so normalise here so the two halves can be compared directly.
    """
    if not author:
        return ""
    login = author.get("login", "")
    if login and author.get("__typename") == "Bot":
        return f"{login}[bot]"
    return login


class Comment:
    """A GitHub issue or PR comment."""

    def __init__(
        self,
        *,
        author: str,
        body: Content | str,
        created_at: str,
        id: int | None = None,
        path: str | None = None,
        line: int | None = None,
        kind: str = "issue",
        html_url: str | None = None,
        thread_id: str | None = None,
        in_reply_to_id: int | None = None,
    ) -> None:
        self.author = author
        self.body = body if isinstance(body, Content) else Content(body)
        self.created_at = created_at
        self.id = id
        self.path = path
        self.line = line
        # kind: "issue" (conversation comment), "review_body" (review summary),
        # or "inline" (inline review-thread comment).  Determines how Claude
        # should reply and whether the thread is resolvable.
        self.kind = kind
        self.html_url = html_url
        self.thread_id = thread_id
        self.in_reply_to_id = in_reply_to_id

    # --- Class-level reads ---

    _ISSUE_COMMENTS_QUERY = """\
fragment CommentFields on IssueComment {
  databaseId
  author { __typename login }
  body
  url
  createdAt
}

query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issueOrPullRequest(number:$number) {
      ... on Issue {
        comments(first:100) { nodes { ...CommentFields } }
      }
      ... on PullRequest {
        comments(first:100) { nodes { ...CommentFields } }
      }
    }
  }
}
"""

    @classmethod
    def list_for_issue(cls, number: int, *, repo: Repo) -> list[Comment]:
        """Get all comments on an issue (or PR), sorted by creation time.

        Uses GraphQL so each comment carries ``databaseId`` (the integer REST
        ID needed to edit it via ``PATCH /repos/{owner}/{repo}/issues/comments/{id}``).
        ``gh issue view --json comments`` only exposes the GraphQL node ID,
        which the REST edit endpoint rejects — so the plan-comment-reuse path
        silently fell through to posting a new comment.
        """
        import subprocess

        owner, _, name = repo.name.partition("/")
        try:
            response = repo.client.gh_graphql(
                cls._ISSUE_COMMENTS_QUERY,
                owner=owner,
                repo=name,
                number=number,
            )
        except subprocess.CalledProcessError as exc:
            detail = ((exc.stderr or "") + (exc.stdout or "")).strip()[:200]
            logger.warning("Failed to fetch comments for #%d: %s", number, detail)
            raise CommentFetchError(
                f"Failed to fetch comments for #{number}"
            ) from exc

        node = (
            response.get("data", {})
            .get("repository", {})
            .get("issueOrPullRequest")
        ) or {}
        raw = (node.get("comments") or {}).get("nodes") or []
        comments = [cls._from_api(c, repo) for c in raw]
        comments.sort(key=lambda c: c.created_at)
        logger.debug("Comment.list_for_issue(#%d) returned %d comment(s)", number, len(comments))
        return comments

    _INLINE_REVIEW_THREADS_QUERY = """\
query($owner:String!, $repo:String!, $pr:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$pr) {
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          comments(first:50) {
            nodes {
              databaseId
              author { __typename login }
              body
              url
              createdAt
              path
              line
              replyTo { databaseId }
              pullRequestReview { submittedAt }
            }
          }
        }
      }
    }
  }
}
"""

    @classmethod
    def list_inline_for_pr(cls, pr_number: int, *, repo: Repo) -> list[Comment]:
        """Fetch inline review-thread comments for a PR via GraphQL.

        Uses the parent review's ``submittedAt`` as the effective ``created_at``
        timestamp (REST's ``createdAt`` on an inline comment is the *drafted*
        time, which silently dropped comments from the polling loop — see #78).
        Each returned :class:`Comment` carries ``thread_id`` (GraphQL node ID,
        used to resolve the thread) and ``in_reply_to_id`` (databaseId of the
        top-level comment in the thread, used for REST reply endpoints).
        """
        import subprocess

        owner, _, name = repo.name.partition("/")
        try:
            response = repo.client.gh_graphql(
                cls._INLINE_REVIEW_THREADS_QUERY,
                owner=owner,
                repo=name,
                pr=pr_number,
            )
        except subprocess.CalledProcessError as exc:
            detail = ((exc.stderr or "") + (exc.stdout or "")).strip()[:200]
            logger.warning(
                "Failed to fetch inline review comments for PR #%d: %s", pr_number, detail
            )
            raise CommentFetchError(
                f"Failed to fetch inline review comments for PR #{pr_number}"
            ) from exc

        threads = (
            response.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        ) or []

        comments: list[Comment] = []
        for thread in threads:
            thread_id = thread.get("id")
            thread_comments = (thread.get("comments") or {}).get("nodes") or []
            # The first comment in the thread is the top-level; replies carry
            # in_reply_to pointing at its databaseId.
            top_db_id: int | None = None
            for node in thread_comments:
                author = _author_login(node.get("author"))
                body_text = node.get("body", "")
                safe = (author == repo.bot_name)
                db_id = node.get("databaseId")
                if top_db_id is None:
                    top_db_id = db_id
                reply_to = (node.get("replyTo") or {}).get("databaseId")
                # Prefer the parent review's submittedAt; fall back to createdAt
                # so we never lose a comment that hasn't been assigned to a
                # published review yet.
                review = node.get("pullRequestReview") or {}
                effective_ts = review.get("submittedAt") or node.get("createdAt", "")
                comments.append(Comment(
                    author=author,
                    body=Content(body_text, safe=safe),
                    created_at=effective_ts,
                    id=db_id,
                    path=node.get("path"),
                    line=node.get("line"),
                    kind="inline",
                    html_url=node.get("url"),
                    thread_id=thread_id,
                    in_reply_to_id=reply_to if reply_to is not None else (
                        None if db_id == top_db_id else top_db_id
                    ),
                ))

        logger.debug(
            "Comment.list_inline_for_pr(#%d) returned %d comment(s) across %d thread(s)",
            pr_number, len(comments), len(threads),
        )
        return comments

    @classmethod
    def _from_api(cls, data: dict, repo: Repo) -> Comment:
        author = _author_login(data.get("author"))
        body_text = data.get("body", "")
        safe = (author == repo.bot_name)
        return cls(
            author=author,
            body=Content(body_text, safe=safe),
            created_at=data.get("createdAt", ""),
            id=data.get("databaseId"),
            kind="issue",
            html_url=data.get("url"),
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
