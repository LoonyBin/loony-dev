from __future__ import annotations

from typing import TYPE_CHECKING

from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Issue, TaskResult


class IssueTask(Task):
    task_type = "implement_issue"

    def __init__(self, issue: Issue, plan: str | None = None) -> None:
        self.issue = issue
        self.plan = plan

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
