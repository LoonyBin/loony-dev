from __future__ import annotations

from loony_dev.plugins.base import TaskPlugin
from loony_dev.tasks.base import Task
from loony_dev.tasks.ci_failure_task import CIFailureTask
from loony_dev.tasks.conflict_task import ConflictResolutionTask
from loony_dev.tasks.issue_task import IssueTask
from loony_dev.tasks.planning_task import PlanningTask
from loony_dev.tasks.pr_review_task import PRReviewTask
from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask


class GithubTaskPlugin(TaskPlugin):
    """Built-in task plugin that registers all GitHub-backed task types."""

    @property
    def name(self) -> str:
        return "github"

    @property
    def task_classes(self) -> list[type[Task]]:
        return [
            StuckItemCleanupTask,
            ConflictResolutionTask,
            CIFailureTask,
            PRReviewTask,
            PlanningTask,
            IssueTask,
        ]
