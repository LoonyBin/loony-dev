from __future__ import annotations

from typing import TYPE_CHECKING

from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import PullRequest, TaskResult


class PRReviewTask(Task):
    task_type = "address_review"

    def __init__(self, pr: PullRequest) -> None:
        self.pr = pr

    def describe(self) -> str:
        comments_text = "\n\n".join(
            self._format_comment(c) for c in self.pr.new_comments
        )
        return (
            f"Address review comments on PR #{self.pr.number}: {self.pr.title}\n\n"
            f"You are on branch: {self.pr.branch}\n\n"
            f"New review comments to address:\n\n{comments_text}\n\n"
            f"Instructions:\n"
            f"- Read and understand each review comment\n"
            f"- Make the requested changes\n"
            f"- Commit and push your changes"
        )

    def _format_comment(self, comment) -> str:
        location = ""
        if comment.path:
            location = f" ({comment.path}"
            if comment.line:
                location += f":{comment.line}"
            location += ")"
        return f"**{comment.author}**{location}:\n{comment.body}"

    def on_start(self, github: GitHubClient) -> None:
        github.add_label(self.pr.number, "in-progress")

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.remove_label(self.pr.number, "in-progress")
        github.post_comment(
            self.pr.number,
            f"Review comments addressed.\n\n{result.summary}",
        )

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.remove_label(self.pr.number, "in-progress")
        github.post_comment(
            self.pr.number,
            f"Failed to address review comments: {error}",
        )
