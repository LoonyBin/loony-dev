from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import Issue, PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


def stuck_params() -> tuple[int, datetime]:
    """Return ``(threshold_hours, cutoff)`` for the stuck-item check this tick."""
    from loony_dev import config

    threshold_hours = int(config.settings.get("stuck_threshold_hours", 12))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)
    return threshold_hours, cutoff


def stuck_issue_action(
    issue: Issue, threshold_hours: int, cutoff: datetime,
) -> StuckItemCleanupTask | None:
    """Pure predicate: a cleanup task for *issue* if it is stuck in-progress, else None."""
    if "in-progress" not in issue.labels:
        return None
    if "in-error" in issue.labels:
        return None
    if issue.updated_at is not None and issue.updated_at < cutoff:
        logger.debug(
            "Issue #%d has been in-progress since %s (threshold: %dh) — marking stuck",
            issue.number, issue.updated_at, threshold_hours,
        )
        return StuckItemCleanupTask(issue, threshold_hours)
    return None


def stuck_pr_action(
    pr: PullRequest, threshold_hours: int, cutoff: datetime, bot_name: str,
) -> StuckItemCleanupTask | None:
    """Pure predicate: a cleanup task for *pr* if it is stuck in-progress, else None."""
    if not pr.is_assigned_to(bot_name):
        return None
    if "in-progress" not in pr.labels:
        return None
    if "in-error" in pr.labels:
        return None
    if pr.updated_at is not None and pr.updated_at < cutoff:
        logger.debug(
            "PR #%d has been in-progress since %s (threshold: %dh) — marking stuck",
            pr.number, pr.updated_at, threshold_hours,
        )
        return StuckItemCleanupTask(pr, threshold_hours)
    return None


class StuckItemCleanupTask(Task):
    """Resets issues and PRs that have been stuck in-progress for too long."""

    task_type = "cleanup_stuck"
    priority = 5

    def __init__(self, item: Issue | PullRequest, threshold_hours: int) -> None:
        self.item = item
        self.threshold_hours = threshold_hours

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[StuckItemCleanupTask]:
        """Yield cleanup tasks for issues and PRs stuck in-progress past the threshold."""
        from loony_dev.github import Issue, PullRequest

        threshold_hours, cutoff = stuck_params()

        for issue in Issue.list(label="in-progress", repo=repo):
            task = stuck_issue_action(issue, threshold_hours, cutoff)
            if task is not None:
                yield task

        for pr in PullRequest.list_open(repo=repo):
            task = stuck_pr_action(pr, threshold_hours, cutoff, repo.bot_name)
            if task is not None:
                yield task

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    def describe(self) -> str:
        from loony_dev.github import Issue

        kind = "Issue" if isinstance(self.item, Issue) else "PR"
        return (
            f"Clean up stuck {kind} #{self.item.number}: {self.item.title}\n\n"
            f"This item has been labeled in-progress for over {self.threshold_hours} hours "
            f"with no activity, indicating the worker stopped unexpectedly. "
            f"Resetting to allow retry."
        )

    @property
    def session_key(self) -> str | None:
        return None

    def on_start(self, repo: Repo) -> None:
        from loony_dev.github import Issue

        kind = "issue" if isinstance(self.item, Issue) else "PR"
        logger.info(
            "Resetting stuck %s #%d (%s), in-progress since %s",
            kind, self.item.number, self.item.title, self.item.updated_at,
        )
        self.item.add_comment(
            f"This item has been `in-progress` for over {self.threshold_hours} hours with no "
            f"activity. The worker likely stopped unexpectedly. Resetting to allow retry.",
        )

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        from loony_dev.github import Issue

        self.item.remove_label("in-progress")
        if isinstance(self.item, Issue):
            self.item.add_label("ready-for-development")
            logger.info(
                "Issue #%d reset: removed in-progress, restored ready-for-development",
                self.item.number,
            )
        else:
            logger.info("PR #%d reset: removed in-progress", self.item.number)

    def on_failure(self, repo: Repo, error: Exception) -> None:
        logger.error(
            "Failed to clean up stuck item #%d: %s", self.item.number, error
        )
