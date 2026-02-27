from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task


class CodingAgent(Agent):
    """Invokes Claude Code CLI to implement code changes."""

    name = "coding"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def can_handle(self, task: Task) -> bool:
        return task.task_type in ("implement_issue", "address_review")

    def execute(self, task: Task) -> TaskResult:
        prompt = task.describe()
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", prompt],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
        )

        success = result.returncode == 0
        output = result.stdout if success else f"{result.stdout}\n{result.stderr}"

        # Ask Claude to summarize what it did
        if success:
            summary = self._generate_summary(output)
        else:
            summary = f"Agent exited with code {result.returncode}"

        return TaskResult(success=success, output=output, summary=summary)

    def _generate_summary(self, output: str) -> str:
        """Use Claude to generate a brief summary of the work done."""
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                f"Summarize what was done in 2-3 sentences based on this output:\n\n{output[-3000:]}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "Changes were made successfully."
