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


class CodingAgent(ClaudeQuotaMixin, Agent):
    """Invokes Claude Code CLI to implement code changes."""

    name = "coding"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type in ("implement_issue", "address_review", "resolve_conflicts")

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        cmd = ["claude", "-p", "--dangerously-skip-permissions", prompt]
        logger.debug("Running Claude CLI (cwd=%s): claude -p --dangerously-skip-permissions <prompt>", self.work_dir)
        logger.debug("Claude prompt: %s", truncate_for_log(prompt))

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

        returncode = proc.returncode
        logger.debug("Claude CLI exited with code %d", returncode)
        if stdout:
            logger.debug("Claude stdout: %s", truncate_for_log(stdout))
        if stderr:
            logger.debug("Claude stderr: %s", truncate_for_log(stderr))

        if returncode != 0:
            combined = f"{stdout}\n{stderr}"
            if self._is_quota_error(combined):
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {returncode}",
            )

        summary = self._generate_summary(stdout)
        return TaskResult(success=True, output=stdout, summary=summary)

    def _generate_summary(self, output: str) -> str:
        """Use Claude to generate a brief summary of the work done."""
        summary_prompt = f"Summarize what was done in 2-3 sentences based on this output:\n\n{output[-3000:]}"
        logger.debug("Running summary Claude call")
        logger.debug("Summary prompt: %s", truncate_for_log(summary_prompt))
        with subprocess.Popen(
            ["claude", "-p", "--dangerously-skip-permissions", summary_prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            self._active_process = proc
            try:
                stdout, _ = proc.communicate()
            finally:
                self._active_process = None
        logger.debug("Summary Claude call exited with code %d", proc.returncode)
        if stdout:
            logger.debug("Summary output: %s", truncate_for_log(stdout))
        if proc.returncode == 0 and stdout.strip():
            return stdout.strip()
        return "Changes were made successfully."

