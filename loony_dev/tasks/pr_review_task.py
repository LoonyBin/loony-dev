from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import (
    FAILURE_MARKER,
    FAILURE_MARKER_PREFIX,
    SUCCESS_MARKER,
    SUCCESS_MARKER_PREFIX,
    Task,
    decode_last_seen,
    encode_marker,
    issue_or_pr_keys,
)

if TYPE_CHECKING:
    from loony_dev.github import Comment, PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


def pr_review_action(pr: PullRequest, repo: Repo) -> PRReviewTask | None:
    """Pure predicate: a review task for *pr* if it has new authorized comments, else None."""
    if not pr.is_assigned_to(repo.bot_name):
        logger.debug("PR #%d is not assigned to bot — skipping", pr.number)
        return None
    logger.debug("Examining PR #%d: %s (labels=%s)", pr.number, pr.title, pr.labels)
    if "in-progress" in pr.labels:
        logger.debug("PR #%d is in-progress — skipping", pr.number)
        return None
    if "in-error" in pr.labels:
        logger.debug("PR #%d is in-error — skipping", pr.number)
        return None

    all_comments = PRReviewTask._assemble_comments(pr, repo)
    new_comments = PRReviewTask._new_since_bot(all_comments, repo.bot_name)

    if not new_comments:
        logger.debug("PR #%d has no new comments — skipping", pr.number)
        return None

    authorized_comments = [c for c in new_comments if repo.is_authorized(c.author)]
    if not authorized_comments:
        logger.debug(
            "PR #%d has %d new comment(s) but none from authorized users — skipping",
            pr.number, len(new_comments),
        )
        return None

    logger.debug(
        "PR #%d has %d authorized new comment(s) — yielding task",
        pr.number, len(authorized_comments),
    )
    # Create a new PR object with just the relevant data for the task.
    # `comments`/`reviews` are carried over (not just `new_comments`) so
    # the repeated-failure -> in-error escalation in on_failure can see
    # the bot's own prior failure comments via get_comments(); without
    # them get_comments() returns [] and the item never escalates.
    from loony_dev.github import PullRequest as PR
    return PRReviewTask(PR(
        number=pr.number,
        branch=pr.branch,
        title=pr.title,
        labels=pr.labels,
        comments=pr.comments,
        reviews=pr.reviews,
        new_comments=authorized_comments,
        _repo=pr._repo,
    ))


class PRReviewTask(Task):
    task_type = "address_review"
    priority = 20
    command_name = "address-reviews"

    def __init__(self, pr: PullRequest) -> None:
        self.pr = pr

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[PRReviewTask]:
        """Yield PRs that have new review comments from authorized users since the bot last responded."""
        from loony_dev.github import PullRequest

        for pr in PullRequest.list_open(repo=repo):
            task = pr_review_action(pr, repo)
            if task is not None:
                yield task

    @staticmethod
    def _assemble_comments(pr: PullRequest, repo: Repo) -> list[Comment]:
        """Combine general comments, review bodies, and inline review comments."""
        from loony_dev.github import Comment

        comments = list(pr.comments)
        comments += [r for r in pr.reviews if r.body]
        comments.extend(pr.inline_comments)
        comments.sort(key=lambda c: c.created_at)
        return comments

    @staticmethod
    def _new_since_bot(comments: list[Comment], bot_name: str) -> list[Comment]:
        """Return non-bot comments after the bot's last *successful* response."""
        bot_last_success_idx = -1
        bot_last_success_body: str | None = None
        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(SUCCESS_MARKER_PREFIX):
                bot_last_success_idx = i
                bot_last_success_body = c.body

        if bot_last_success_idx == -1:
            result = [c for c in comments if c.author != bot_name]
        else:
            last_seen = decode_last_seen(bot_last_success_body or "")
            if last_seen is not None:
                result = [c for c in comments if c.author != bot_name and c.created_at > last_seen]
            else:
                result = [c for c in comments[bot_last_success_idx + 1:] if c.author != bot_name]

        logger.debug(
            "_new_since_bot: last success marker at index %d, returning %d new comment(s)",
            bot_last_success_idx, len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return issue_or_pr_keys(self.pr)[0]

    @property
    def target_branch(self) -> str:
        return self.pr.branch

    @property
    def worktree_key(self) -> str:
        return issue_or_pr_keys(self.pr)[1]

    def describe(self) -> str:
        """Human-readable label for logging/dashboard (not sent as a turn).

        The work is driven via the ``/address-reviews`` slash command built from
        :meth:`context_payload` (issue #166).
        """
        return f"Address review comments on PR #{self.pr.number}: {self.pr.title}"

    def context_payload(self) -> dict:
        """Context for ``/address-reviews``.

        Carries the PR identity (``owner``/``repo``/``pr`` for the gh API calls
        in the command body), the ``allow_create_issues`` policy flag, and the
        pre-formatted ``comments`` blocks; the triage instructions live in the
        command markdown.
        """
        from loony_dev import config

        # The key is documented under [worker], which lands in
        # config.settings as a nested dict — not a flat top-level key.
        worker_cfg = config.settings.get("worker")
        allow_create_issues = True
        if isinstance(worker_cfg, dict):
            allow_create_issues = bool(
                worker_cfg.get("pr_review_allow_create_issues", True)
            )
        repo = self.pr._repo
        owner = repo.owner
        repo_name = repo.name.split("/", 1)[1] if "/" in repo.name else repo.name

        comments_text = "\n\n".join(
            self._format_comment(c) for c in self.pr.new_comments
        )

        return {
            "pr_number": self.pr.number,
            "title": self.pr.title,
            "branch": self.pr.branch,
            "owner": owner,
            "repo": repo_name,
            "pr": self.pr.number,
            "allow_create_issues": allow_create_issues,
            "comments": comments_text,
        }

    def _format_comment(self, comment: Comment) -> str:
        header_bits = [f"author={comment.author}", f"kind={comment.kind}"]
        if comment.id is not None:
            header_bits.append(f"id={comment.id}")
        if comment.thread_id:
            header_bits.append(f"thread_id={comment.thread_id}")
        if comment.in_reply_to_id is not None:
            header_bits.append(f"in_reply_to_id={comment.in_reply_to_id}")
        if comment.path:
            loc = comment.path + (f":{comment.line}" if comment.line else "")
            header_bits.append(f"location={loc}")
        if comment.html_url:
            header_bits.append(f"url={comment.html_url}")
        header = " | ".join(header_bits)
        return f"--- comment ---\n{header}\n\n{comment.body}"

    def on_start(self, repo: Repo) -> None:
        logger.debug("PR #%d: adding 'in-progress'", self.pr.number)
        self.pr.add_label("in-progress")
        self.pr.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug("PR #%d: removing 'in-progress'", self.pr.number)
        self.pr.remove_label("in-progress")
        last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
        marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen_ts) if last_seen_ts else SUCCESS_MARKER
        if result.post_summary:
            logger.debug("Completion comment body: %s", truncate_for_log(result.summary))
            self.pr.add_comment(
                f"{marker}\n\nReview comments addressed.\n\n{result.summary}",
            )
        else:
            logger.debug("PR #%d: no code changes detected — posting silent marker", self.pr.number)
            self.pr.add_comment(f"{marker}\n\nNo changes needed.")

    def on_failure(self, repo: Repo, error: Exception) -> None:
        logger.debug("PR #%d: task failed (%s), removing 'in-progress'", self.pr.number, error)
        self.pr.remove_label("in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
        marker = encode_marker(FAILURE_MARKER_PREFIX, last_seen_ts) if last_seen_ts else FAILURE_MARKER
        failure_body = f"{marker}\n\nFailed to address review comments: {error}"
        self.pr.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
