from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult
from loony_dev.tasks.planning_task import PLAN_MARKER, PlanningTask

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Comment
    from loony_dev.tasks.base import Task


class PlanningAgent(Agent):
    """Uses Claude to generate or update an implementation plan for an issue."""

    name = "planning"

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def discover_tasks(self, github: GitHubClient) -> list[Task]:
        """Find ready-for-planning issues that need a new or revised plan."""
        tasks: list[Task] = []
        for issue, labels in github.list_issues("ready-for-planning"):
            if "ready-for-development" in labels:
                # User approved the plan; hand off to coding agent.
                github.remove_label(issue.number, "ready-for-planning")
                continue
            comments = github.get_issue_comments(issue.number)
            existing_plan, new_comments = self._analyze_planning_comments(
                comments, github.bot_name
            )
            if existing_plan is None or new_comments:
                tasks.append(PlanningTask(issue, existing_plan, new_comments))
        return tasks

    @staticmethod
    def _analyze_planning_comments(
        comments: list[Comment], bot_name: str
    ) -> tuple[str | None, list[Comment]]:
        """Return (existing_plan, new_user_comments_since_last_plan).

        Only a bot comment that starts with PLAN_MARKER counts as a plan.
        Other bot comments (e.g. failure notices) are ignored.
        """
        bot_last_plan_idx = -1
        bot_last_plan: str | None = None

        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(PLAN_MARKER):
                bot_last_plan_idx = i
                bot_last_plan = c.body[len(PLAN_MARKER):].strip()

        if bot_last_plan_idx == -1:
            new_comments = [c for c in comments if c.author != bot_name]
        else:
            new_comments = [
                c for c in comments[bot_last_plan_idx + 1:] if c.author != bot_name
            ]

        return bot_last_plan, new_comments

    def can_handle(self, task: Task) -> bool:
        return task.task_type == "plan_issue"

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

        # The raw output IS the plan; use it directly as the summary so
        # PlanningTask.on_complete can post it as a GitHub comment.
        summary = output.strip() if success else f"Agent exited with code {result.returncode}"

        return TaskResult(success=success, output=output, summary=summary)
