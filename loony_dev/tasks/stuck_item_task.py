from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loony_dev.github import _parse_datetime
from loony_dev.models import Issue, PullRequest
from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
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
    def discover(github: GitHubClient, threshold_hours: int = 12) -> Iterator[StuckItemCleanupTask]:
        """Yield cleanup tasks for issues and PRs stuck in-progress past the threshold."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)

        for issue, _ in github.list_issues(label="in-progress"):
            if issue.updated_at is not None and issue.updated_at < cutoff:
                logger.debug(
                    "Issue #%d has been in-progress since %s (threshold: %dh) — marking stuck",
                    issue.number, issue.updated_at, threshold_hours,
                )
                yield StuckItemCleanupTask(issue, threshold_hours)

        for item in github.list_open_prs():
            labels = [label["name"] for label in item.get("labels", [])]
            if "in-progress" not in labels:
                continue
            updated_at = _parse_datetime(item.get("updatedAt"))
            if updated_at is not None and updated_at < cutoff:
                pr = PullRequest(
                    number=item["number"],
                    branch=item["headRefName"],
                    title=item["title"],
                    mergeable=item.get("mergeable"),
                    updated_at=updated_at,
                )
                logger.debug(
                    "PR #%d has been in-progress since %s (threshold: %dh) — marking stuck",
                    pr.number, updated_at, threshold_hours,
                )
                yield StuckItemCleanupTask(pr, threshold_hours)

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    def describe(self) -> str:
        kind = "Issue" if isinstance(self.item, Issue) else "PR"
        return (
            f"Clean up stuck {kind} #{self.item.number}: {self.item.title}\n\n"
            f"This item has been labeled in-progress for over {self.threshold_hours} hours "
            f"with no activity, indicating the worker stopped unexpectedly. "
            f"Resetting to allow retry."
        )

    def on_start(self, github: GitHubClient) -> None:
        kind = "issue" if isinstance(self.item, Issue) else "PR"
        logger.info(
            "Resetting stuck %s #%d (%s), in-progress since %s",
            kind, self.item.number, self.item.title, self.item.updated_at,
        )
        github.post_comment(
            self.item.number,
            f"This item has been `in-progress` for over {self.threshold_hours} hours with no "
            f"activity. The worker likely stopped unexpectedly. Resetting to allow retry.",
        )

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.remove_label(self.item.number, "in-progress")
        if isinstance(self.item, Issue):
            github.add_label(self.item.number, "ready-for-development")
            logger.info(
                "Issue #%d reset: removed in-progress, restored ready-for-development",
                self.item.number,
            )
        else:
            logger.info("PR #%d reset: removed in-progress", self.item.number)

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        logger.error(
            "Failed to clean up stuck item #%d: %s", self.item.number, error
        )
