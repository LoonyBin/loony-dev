from __future__ import annotations

from typing import TYPE_CHECKING

from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Comment, Issue, TaskResult


class PlanningTask(Task):
    task_type = "plan_issue"

    def __init__(
        self,
        issue: Issue,
        existing_plan: str | None,
        new_comments: list[Comment],
    ) -> None:
        self.issue = issue
        self.existing_plan = existing_plan
        self.new_comments = new_comments

    def describe(self) -> str:
        if self.existing_plan is None:
            return (
                f"Create a clear implementation plan for the following GitHub issue.\n\n"
                f"Issue #{self.issue.number}: {self.issue.title}\n\n"
                f"{self.issue.body}\n\n"
                f"You may read the codebase to understand the existing structure before planning.\n"
                f"Output ONLY the plan text in well-structured markdown. "
                f"Do NOT implement anything — planning only."
            )

        feedback = "\n\n".join(
            f"**{c.author}:** {c.body}" for c in self.new_comments
        )
        return (
            f"Revise the implementation plan for GitHub issue #{self.issue.number} "
            f"based on the user feedback below.\n\n"
            f"Issue #{self.issue.number}: {self.issue.title}\n\n"
            f"{self.issue.body}\n\n"
            f"## Current Plan\n\n{self.existing_plan}\n\n"
            f"## User Feedback\n\n{feedback}\n\n"
            f"Output ONLY the updated plan text in well-structured markdown. "
            f"Do NOT implement anything — planning only."
        )

    def on_start(self, github: GitHubClient) -> None:
        pass  # Keep ready-for-planning label so state is visible; execution is serial

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.post_comment(self.issue.number, result.summary)

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.post_comment(
            self.issue.number,
            f"Planning failed: {error}",
        )
