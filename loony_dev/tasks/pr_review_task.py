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
)

if TYPE_CHECKING:
    from loony_dev.github import Comment, PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


class PRReviewTask(Task):
    task_type = "address_review"
    priority = 20

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
            if not pr.is_assigned_to(repo.bot_name):
                logger.debug("PR #%d is not assigned to bot — skipping", pr.number)
                continue
            logger.debug("Examining PR #%d: %s (labels=%s)", pr.number, pr.title, pr.labels)
            if "in-progress" in pr.labels:
                logger.debug("PR #%d is in-progress — skipping", pr.number)
                continue

            all_comments = PRReviewTask._assemble_comments(pr, repo)
            new_comments = PRReviewTask._new_since_bot(all_comments, repo.bot_name)

            if not new_comments:
                logger.debug("PR #%d has no new comments — skipping", pr.number)
                continue

            authorized_comments = [
                c for c in new_comments
                if repo.is_authorized(c.author)
            ]
            if not authorized_comments:
                logger.debug(
                    "PR #%d has %d new comment(s) but none from authorized users — skipping",
                    pr.number, len(new_comments),
                )
                continue

            logger.debug(
                "PR #%d has %d authorized new comment(s) — yielding task",
                pr.number, len(authorized_comments),
            )
            # Create a new PR object with just the relevant data for the task
            from loony_dev.github import PullRequest as PR
            yield PRReviewTask(PR(
                number=pr.number,
                branch=pr.branch,
                title=pr.title,
                new_comments=authorized_comments,
                _repo=pr._repo,
            ))

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
        return f"pr:{self.pr.number}"

    def describe(self) -> str:
        comments_text = "\n\n".join(
            self._format_comment(c) for c in self.pr.new_comments
        )
        return (
            f"Address review comments on PR #{self.pr.number}: {self.pr.title}\n\n"
            f"You are on branch: {self.pr.branch}\n\n"
            f"New review comments to address:\n\n{comments_text}\n\n"
            f"Instructions:\n"
            f"- Read and understand each review comment\n"
            f"- Make the requested changes\n"
            f"- Commit and push your changes"
        )

    def _format_comment(self, comment: Comment) -> str:
        location = ""
        if comment.path:
            location = f" ({comment.path}"
            if comment.line:
                location += f":{comment.line}"
            location += ")"
        return f"**{comment.author}**{location}:\n{comment.body}"

    def on_start(self, repo: Repo) -> None:
        logger.debug("PR #%d: adding 'in-progress'", self.pr.number)
        self.pr.add_label("in-progress")
        self.pr.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug("PR #%d: removing 'in-progress'", self.pr.number)
        self.pr.remove_label("in-progress")
        if result.post_summary:
            last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
            marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen_ts) if last_seen_ts else SUCCESS_MARKER
            logger.debug("Completion comment body: %s", truncate_for_log(result.summary))
            self.pr.add_comment(
                f"{marker}\n\nReview comments addressed.\n\n{result.summary}",
            )
        else:
            logger.debug("PR #%d: no code changes detected — skipping summary comment", self.pr.number)

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
        self.pr.add_comment(
            f"{marker}\n\nFailed to address review comments: {error}",
        )
