from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.github.repo import parse_datetime
from loony_dev.models import RateLimitedError
from loony_dev.tasks.base import CI_FAILURE_MARKER, Task

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from loony_dev.github import CheckRun, PullRequest, Repo
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
    def discover(repo: Repo) -> Iterator[CIFailureTask]:
        """Yield PRs with failing CI checks that haven't been handled yet."""
        from loony_dev.github import PullRequest

        for pr in PullRequest.list_open(repo=repo):
            if not pr.is_assigned_to(repo.bot_name):
                continue
            if "in-progress" in pr.labels:
                continue
            if not pr.head_sha:
                continue

            failed = pr.check_runs
            if not failed:
                continue

            # Idempotency: skip if the bot already posted a CI failure marker
            # comment after the most recent push (updatedAt).
            already_handled = False
            for comment in pr.comments:
                if CI_FAILURE_MARKER not in comment.body:
                    continue
                if comment.author != repo.bot_name:
                    continue
                comment_time = parse_datetime(comment.created_at)
                if comment_time and pr.updated_at and comment_time >= pr.updated_at:
                    already_handled = True
                    break

            if already_handled:
                continue

            yield CIFailureTask(pr=pr, failed_checks=failed)

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

    def on_start(self, repo: Repo) -> None:
        self.pr.add_label("in-progress")
        self.pr.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        self.pr.remove_label("in-progress")
        self.pr.add_comment(
            f"{CI_FAILURE_MARKER}\n\nCI failures addressed.\n\n{result.summary}",
        )

    def on_failure(self, repo: Repo, error: Exception) -> None:
        self.pr.remove_label("in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        self.pr.add_comment(
            f"{CI_FAILURE_MARKER}\n\nFailed to fix CI failures: {error}\n\nManual intervention is required.",
        )
