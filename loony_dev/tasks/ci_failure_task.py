from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.github import CI_FAILURE_MARKER
from loony_dev.models import CheckRun, PullRequest, RateLimitedError
from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import TaskResult


class CIFailureTask(Task):
    task_type = "fix_ci"
    priority = 15

    def __init__(self, pr: PullRequest, failed_checks: list[CheckRun]) -> None:
        self.pr = pr
        self.failed_checks = failed_checks

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(github: GitHubClient) -> Iterator[CIFailureTask]:
        """Yield PRs with failing CI checks that haven't been handled yet."""
        from loony_dev.github import _parse_datetime

        for item in github.list_open_prs():
            if not github.is_assigned_to_bot(item):
                continue

            labels = [l["name"] for l in item.get("labels", [])]
            if "in-progress" in labels:
                continue

            head_sha = item.get("headRefOid", "")
            if not head_sha:
                continue

            failed = github.get_pr_check_runs(head_sha)
            if not failed:
                continue

            # Idempotency: skip if the bot already posted a CI failure marker
            # comment after the most recent push (updatedAt).
            pr_updated_at = _parse_datetime(item.get("updatedAt"))
            already_handled = False
            for comment in item.get("comments", []):
                body = comment.get("body", "")
                if CI_FAILURE_MARKER not in body:
                    continue
                author = comment.get("author", {}).get("login", "")
                if author != github.bot_name:
                    continue
                comment_time = _parse_datetime(comment.get("createdAt"))
                if comment_time and pr_updated_at and comment_time >= pr_updated_at:
                    already_handled = True
                    break

            if already_handled:
                continue

            yield CIFailureTask(
                pr=PullRequest(
                    number=item["number"],
                    branch=item["headRefName"],
                    title=item["title"],
                    head_sha=head_sha,
                    mergeable=item.get("mergeable"),
                ),
                failed_checks=failed,
            )

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return f"pr:{self.pr.number}"

    def describe(self) -> str:
        check_lines = "\n".join(
            f"- {c.name} ({c.conclusion}): {c.details_url}"
            for c in self.failed_checks
        )
        return (
            f"Fix CI failures on PR #{self.pr.number}: {self.pr.title}\n\n"
            f"Branch: {self.pr.branch}\n\n"
            f"The following CI checks are failing:\n{check_lines}\n\n"
            f"Instructions:\n"
            f"- Review the CI logs at the URLs above\n"
            f"- Identify the root cause of each failure\n"
            f"- Make targeted fixes on branch {self.pr.branch}\n"
            f"- Do not change unrelated code\n"
            f"- Push the fixes when done"
        )

    def on_start(self, github: GitHubClient) -> None:
        github.add_label(self.pr.number, "in-progress")
        github.assign_self(self.pr.number)

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.remove_label(self.pr.number, "in-progress")
        github.post_comment(
            self.pr.number,
            f"{CI_FAILURE_MARKER}\n\nCI failures addressed.\n\n{result.summary}",
        )

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.remove_label(self.pr.number, "in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        github.post_comment(
            self.pr.number,
            f"{CI_FAILURE_MARKER}\n\nFailed to fix CI failures: {error}\n\nManual intervention is required.",
        )
