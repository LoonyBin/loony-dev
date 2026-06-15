from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.agents.claude_quota import ClaudeQuotaMixin
from loony_dev.agents.context_file import CommandNotInstalledError, cleanup_context_dir
from loony_dev.models import TaskResult, truncate_for_log

if TYPE_CHECKING:
    from pathlib import Path

    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)


class PlanningAgent(ClaudeQuotaMixin, Agent):
    """Uses Claude to generate or update an implementation plan for an issue."""

    name = "planning"

    def __init__(self, repo: str = "") -> None:
        self.repo = repo

    def _can_handle_task(self, task: Task) -> bool:
        return task.task_type == "plan_issue"

    def execute(self, task: Task, work_dir: Path) -> TaskResult:
        session_id = self._session_id_for(task)
        try:
            prompt = self._command_turn(
                work_dir, task.command_name, task.context_payload(),
                task_key=task.worktree_key,
            )
        except CommandNotInstalledError as exc:
            logger.error("Cannot dispatch planning task: %s", exc)
            return TaskResult(success=False, output=str(exc), summary=str(exc))

        logger.debug("Running planning Claude CLI (cwd=%s, session=%s)", work_dir, session_id)
        logger.debug("Planning turn: %s", prompt)

        try:
            stdout, stderr, returncode = self._run_claude_cli(
                prompt, cwd=work_dir, session_id=session_id,
            )
        finally:
            cleanup_context_dir(task.worktree_key)

        logger.debug("Planning Claude CLI exited with code %d", returncode)
        if stdout:
            logger.debug("Planning output (%d chars): %s", len(stdout), truncate_for_log(stdout))
        if stderr:
            logger.debug("Planning stderr: %s", truncate_for_log(stderr))

        if returncode != 0:
            combined = f"{stdout}\n{stderr}"
            if self._is_quota_error(combined):
                self._handle_quota_error(combined)
            return TaskResult(
                success=False,
                output=combined,
                summary=f"Agent exited with code {returncode}",
            )

        # The raw output IS the plan; use it directly as the summary so
        # PlanningTask.on_complete can post it as a GitHub comment.
        return TaskResult(
            success=True,
            output=stdout,
            summary=stdout.strip(),
        )
