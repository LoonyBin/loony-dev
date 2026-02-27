from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import Comment, PullRequest
from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import TaskResult


class PRReviewTask(Task):
    task_type = "address_review"
    priority = 1

    def __init__(self, pr: PullRequest) -> None:
        self.pr = pr

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(github: GitHubClient) -> Iterator[PRReviewTask]:
        """Yield PRs that have new review comments since the bot last responded."""
        for item in github.list_open_prs():
            labels = [l["name"] for l in item.get("labels", [])]
            if "in-progress" in labels:
                continue

            all_comments = PRReviewTask._assemble_comments(item, github)
            new_comments = PRReviewTask._new_since_bot(all_comments, github.bot_name)

            if new_comments:
                yield PRReviewTask(PullRequest(
                    number=item["number"],
                    branch=item["headRefName"],
                    title=item["title"],
                    new_comments=new_comments,
                ))

    @staticmethod
    def _assemble_comments(pr_data: dict, github: GitHubClient) -> list[Comment]:
        """Combine general comments, review bodies, and inline review comments."""
        comments = [
            Comment(
                author=c.get("author", {}).get("login", ""),
                body=c.get("body", ""),
                created_at=c.get("createdAt", ""),
            )
            for c in pr_data.get("comments", [])
        ]
        comments += [
            Comment(
                author=review.get("author", {}).get("login", ""),
                body=review.get("body", ""),
                created_at=review.get("submittedAt", ""),
            )
            for review in pr_data.get("reviews", [])
            if review.get("body")
        ]
        comments.extend(github.get_pr_inline_comments(pr_data["number"]))
        comments.sort(key=lambda c: c.created_at)
        return comments

    @staticmethod
    def _new_since_bot(comments: list[Comment], bot_name: str) -> list[Comment]:
        """Return non-bot comments that appear after the bot's last comment."""
        bot_last_idx = -1
        for i, c in enumerate(comments):
            if c.author == bot_name:
                bot_last_idx = i

        if bot_last_idx == -1:
            return [c for c in comments if c.author != bot_name]
        return [c for c in comments[bot_last_idx + 1:] if c.author != bot_name]

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

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

    def _format_comment(self, comment: Comment) -> str:
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
