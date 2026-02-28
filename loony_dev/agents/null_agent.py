from __future__ import annotations

from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task


class NullAgent(Agent):
    """A no-op agent for tasks that handle themselves without Claude.

    Used by StuckItemCleanupTask, which performs its work entirely in
    on_start / on_complete without needing an AI agent.
    """

    name = "null"

    def can_handle(self, task: Task) -> bool:
        return task.task_type == "cleanup_stuck"

    def execute(self, task: Task) -> TaskResult:
        return TaskResult(success=True, output="", summary="Cleanup task completed.")
