from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin
from loony_dev.models import TaskResult, truncate_for_log

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)


class PlanningAgent(ClaudeQuotaMixin, Agent):
    """Uses Claude to generate or update an implementation plan for an issue."""

    name = "planning"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type == "plan_issue"

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        cmd = ["claude", "-p", "--dangerously-skip-permissions", prompt]
        logger.debug("Running planning Claude CLI (cwd=%s)", self.work_dir)
        logger.debug("Planning prompt: %s", truncate_for_log(prompt))

        with subprocess.Popen(
            cmd,
            cwd=self.work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            self._active_process = proc
            try:
                stdout, stderr = proc.communicate()
            finally:
                self._active_process = None

        logger.debug("Planning Claude CLI exited with code %d", proc.returncode)
        if stdout:
            logger.debug("Planning output (%d chars): %s", len(stdout), truncate_for_log(stdout))
        if stderr:
            logger.debug("Planning stderr: %s", truncate_for_log(stderr))

        if proc.returncode != 0:
            combined = f"{stdout}\n{stderr}"
            if self._is_quota_error(combined):
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {proc.returncode}",
            )

        # The raw output IS the plan; use it directly as the summary so
        # PlanningTask.on_complete can post it as a GitHub comment.
        return TaskResult(
            success=True,
            output=stdout,
            summary=stdout.strip(),
        )

