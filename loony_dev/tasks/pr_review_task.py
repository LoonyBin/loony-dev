from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.github import is_authorized
from loony_dev.models import Comment, PullRequest, RateLimitedError, truncate_for_log
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
    from loony_dev.github import GitHubClient
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
    def discover(github: GitHubClient) -> Iterator[PRReviewTask]:
        """Yield PRs that have new review comments from authorized users since the bot last responded."""
        for item in github.list_open_prs():
            pr_number = item["number"]
            if not github.is_assigned_to_bot(item):
                logger.debug("PR #%d is not assigned to bot — skipping", pr_number)
                continue
            labels = [l["name"] for l in item.get("labels", [])]
            logger.debug("Examining PR #%d: %s (labels=%s)", pr_number, item.get("title", ""), labels)
            if "in-progress" in labels:
                logger.debug("PR #%d is in-progress — skipping", pr_number)
                continue

            all_comments = PRReviewTask._assemble_comments(item, github)
            new_comments = PRReviewTask._new_since_bot(all_comments, github.bot_name)

            if not new_comments:
                logger.debug("PR #%d has no new comments — skipping", pr_number)
                continue

            authorized_comments = [
                c for c in new_comments
                if is_authorized(github, c.author)
            ]
            if not authorized_comments:
                logger.debug(
                    "PR #%d has %d new comment(s) but none from authorized users — skipping",
                    pr_number, len(new_comments),
                )
                continue

            logger.debug(
                "PR #%d has %d authorized new comment(s) — yielding task",
                pr_number, len(authorized_comments),
            )
            yield PRReviewTask(PullRequest(
                number=pr_number,
                branch=item["headRefName"],
                title=item["title"],
                new_comments=authorized_comments,
            ))

    @staticmethod
    def _assemble_comments(pr_data: dict, github: GitHubClient) -> list[Comment]:
        """Combine general comments, review bodies, and inline review comments."""
        comments = [
            Comment(
                author=c.get("author", {}).get("login", ""),
                body=c.get("body", ""),
                created_at=c.get("createdAt", ""),
            )
            for c in pr_data.get("comments", [])
        ]
        comments += [
            Comment(
                author=review.get("author", {}).get("login", ""),
                body=review.get("body", ""),
                created_at=review.get("submittedAt", ""),
            )
            for review in pr_data.get("reviews", [])
            if review.get("body")
        ]
        comments.extend(github.get_pr_inline_comments(pr_data["number"]))
        comments.sort(key=lambda c: c.created_at)
        return comments

    @staticmethod
    def _new_since_bot(comments: list[Comment], bot_name: str) -> list[Comment]:
        """Return non-bot comments after the bot's last *successful* response.

        When the marker encodes a ``last-seen`` timestamp, comments are filtered
        by timestamp (strictly after last-seen) so that comments posted *during*
        the previous task run are not permanently missed.  Old markers without
        ``last-seen`` fall back to position-based filtering for backward
        compatibility.
        """
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
                # Backward compat: old marker without last-seen → position-based filter.
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

    def on_start(self, github: GitHubClient) -> None:
        logger.debug("PR #%d: adding 'in-progress'", self.pr.number)
        github.add_label(self.pr.number, "in-progress")
        github.assign_self(self.pr.number)

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        logger.debug("PR #%d: removing 'in-progress'", self.pr.number)
        github.remove_label(self.pr.number, "in-progress")
        if result.post_summary:
            last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
            marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen_ts) if last_seen_ts else SUCCESS_MARKER
            logger.debug("Completion comment body: %s", truncate_for_log(result.summary))
            github.post_comment(
                self.pr.number,
                f"{marker}\n\nReview comments addressed.\n\n{result.summary}",
            )
        else:
            logger.debug("PR #%d: no code changes detected — skipping summary comment", self.pr.number)

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        logger.debug("PR #%d: task failed (%s), removing 'in-progress'", self.pr.number, error)
        github.remove_label(self.pr.number, "in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
        marker = encode_marker(FAILURE_MARKER_PREFIX, last_seen_ts) if last_seen_ts else FAILURE_MARKER
        github.post_comment(
            self.pr.number,
            f"{marker}\n\nFailed to address review comments: {error}",
        )
