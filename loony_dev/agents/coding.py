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

        baseline_commit = self._get_head_commit()

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
        has_changes = self._has_code_changes(baseline_commit)
        return TaskResult(success=True, output=stdout, summary=summary, post_summary=has_changes)

    def _get_head_commit(self) -> str | None:
        """Return the current HEAD commit hash, or None if git is unavailable."""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.work_dir,
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception:
            return None

    def _has_code_changes(self, baseline_commit: str | None) -> bool:
        """Return True if commits were added or files are staged/modified since baseline."""
        try:
            # Check for uncommitted changes (staged or unstaged)
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.work_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True

            # Check for new commits since baseline
            if baseline_commit:
                current = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.work_dir,
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                return current != baseline_commit

        except Exception:
            pass

        return True  # safe default: post summary if we can't determine

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

