from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.tasks.base import Task
from loony_dev.tasks.planning_task import PLAN_MARKER

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Comment, Issue, TaskResult


class IssueTask(Task):
    task_type = "implement_issue"
    priority = 3

    def __init__(self, issue: Issue, plan: str | None = None) -> None:
        self.issue = issue
        self.plan = plan

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(github: GitHubClient) -> Iterator[IssueTask]:
        """Yield implementation tasks for issues labeled ready-for-development."""
        for issue in github.get_ready_issues():
            comments = github.get_issue_comments(issue.number)
            plan = IssueTask._find_plan(comments, github.bot_name)
            yield IssueTask(issue, plan=plan)

    @staticmethod
    def _find_plan(comments: list[Comment], bot_name: str) -> str | None:
        """Return the text of the most recent approved plan comment, or None."""
        plan: str | None = None
        for c in comments:
            if c.author == bot_name and c.body.startswith(PLAN_MARKER):
                plan = c.body[len(PLAN_MARKER):].strip()
        return plan

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    def describe(self) -> str:
        if self.plan is not None:
            content = f"## Approved Implementation Plan\n\n{self.plan}"
        else:
            content = f"Issue #{self.issue.number}: {self.issue.title}\n\n{self.issue.body}"
        return (
            f"Implement the following GitHub issue.\n\n"
            f"{content}\n\n"
            f"Instructions:\n"
            f"- Create a new branch for this work\n"
            f"- Implement the changes described in the issue\n"
            f"- Commit your changes with a descriptive message referencing #{self.issue.number}\n"
            f"- Push the branch and create a pull request\n"
            f"- The PR title should reference the issue number"
        )

    def on_start(self, github: GitHubClient) -> None:
        github.remove_label(self.issue.number, "ready-for-development")
        github.add_label(self.issue.number, "in-progress")

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.remove_label(self.issue.number, "in-progress")
        github.post_comment(
            self.issue.number,
            f"Implementation complete.\n\n{result.summary}",
        )

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.remove_label(self.issue.number, "in-progress")
        github.add_label(self.issue.number, "ready-for-development")
        github.post_comment(
            self.issue.number,
            f"Implementation failed: {error}",
        )
