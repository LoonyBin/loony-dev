from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.github.repo import parse_datetime
from loony_dev.models import RateLimitedError
from loony_dev.tasks.base import CI_FAILURE_MARKER, Task, issue_or_pr_keys

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from loony_dev.github import CheckRun, PullRequest, Repo
    from loony_dev.models import TaskResult


def ci_already_handled(pr: PullRequest, bot_name: str) -> bool:
    """True if the bot already posted a CI-failure marker after the latest push.

    Idempotency for the CI rung: a marker comment whose timestamp is at or after
    the PR's ``updatedAt`` (the most recent push) means this failure has been
    addressed and must not be re-dispatched.
    """
    for comment in pr.comments:
        if CI_FAILURE_MARKER not in comment.body:
            continue
        if comment.author != bot_name:
            continue
        comment_time = parse_datetime(comment.created_at)
        if comment_time and pr.updated_at and comment_time >= pr.updated_at:
            return True
    return False


def ci_failure_action(pr: PullRequest, bot_name: str) -> CIFailureTask | None:
    """Pure predicate: a CI-fix task for *pr* if it has unhandled failures, else None."""
    if not pr.is_assigned_to(bot_name):
        return None
    if "in-progress" in pr.labels:
        return None
    if "in-error" in pr.labels:
        return None
    if not pr.head_sha:
        return None

    failed = pr.check_runs
    if not failed:
        return None

    if ci_already_handled(pr, bot_name):
        return None

    return CIFailureTask(pr=pr, failed_checks=failed)


class CIFailureTask(Task):
    task_type = "fix_ci"
    priority = 15
    command_name = "fix-ci"

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
            task = ci_failure_action(pr, repo.bot_name)
            if task is not None:
                yield task

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

        The work is driven via the ``/fix-ci`` slash command built from
        :meth:`context_payload` (issue #166).
        """
        return f"Fix CI failures on PR #{self.pr.number}: {self.pr.title}"

    def context_payload(self) -> dict:
        """Context for ``/fix-ci``."""
        return {
            "pr_number": self.pr.number,
            "title": self.pr.title,
            "branch": self.pr.branch,
            "failed_checks": [
                {
                    "name": c.name,
                    "conclusion": c.conclusion,
                    "url": c.details_url,
                }
                for c in self.failed_checks
            ],
        }

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
        failure_body = (
            f"{CI_FAILURE_MARKER}\n\nFailed to fix CI failures: {error}\n\n"
            f"Manual intervention is required."
        )
        self.pr.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
