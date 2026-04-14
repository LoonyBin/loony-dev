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
        from loony_dev import config
        from loony_dev.github import Issue, PullRequest

        threshold_hours = int(config.settings.get("stuck_threshold_hours", 12))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)

        for issue in Issue.list(label="in-progress", repo=repo):
            if issue.updated_at is not None and issue.updated_at < cutoff:
                logger.debug(
                    "Issue #%d has been in-progress since %s (threshold: %dh) — marking stuck",
                    issue.number, issue.updated_at, threshold_hours,
                )
                yield StuckItemCleanupTask(issue, threshold_hours)

        for pr in PullRequest.list_open(repo=repo):
            if not pr.is_assigned_to(repo.bot_name):
                continue
            if "in-progress" not in pr.labels:
                continue
            if pr.updated_at is not None and pr.updated_at < cutoff:
                logger.debug(
                    "PR #%d has been in-progress since %s (threshold: %dh) — marking stuck",
                    pr.number, pr.updated_at, threshold_hours,
                )
                yield StuckItemCleanupTask(pr, threshold_hours)

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
