from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)


class PlanningAgent(Agent):
    """Uses Claude to generate or update an implementation plan for an issue."""

    name = "planning"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def can_handle(self, task: Task) -> bool:
        return task.task_type == "plan_issue"

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        logger.debug("Running planning Claude CLI (cwd=%s)", self.work_dir)
        logger.debug("Planning prompt: %s", truncate_for_log(prompt))

        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", prompt],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
        )

        logger.debug("Planning Claude CLI exited with code %d", result.returncode)
        if result.stdout:
            logger.debug("Planning output (%d chars): %s", len(result.stdout), truncate_for_log(result.stdout))
        if result.stderr:
            logger.debug("Planning stderr: %s", truncate_for_log(result.stderr))

        success = result.returncode == 0
        output = result.stdout if success else f"{result.stdout}\n{result.stderr}"

        # The raw output IS the plan; use it directly as the summary so
        # PlanningTask.on_complete can post it as a GitHub comment.
        summary = output.strip() if success else f"Agent exited with code {result.returncode}"

        return TaskResult(success=success, output=output, summary=summary)
