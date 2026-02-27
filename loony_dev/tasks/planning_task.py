from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import Comment
from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Issue, TaskResult

PLAN_MARKER = "<!-- loony-plan -->"


class PlanningTask(Task):
    task_type = "plan_issue"
    priority = 2

    def __init__(
        self,
        issue: Issue,
        existing_plan: str | None,
        new_comments: list[Comment],
    ) -> None:
        self.issue = issue
        self.existing_plan = existing_plan
        self.new_comments = new_comments

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(github: GitHubClient) -> Iterator[PlanningTask]:
        """Yield planning tasks for issues that need a new or revised plan."""
        for issue, labels in github.list_issues("ready-for-planning"):
            if "ready-for-development" in labels:
                # User approved the plan; hand off to coding agent.
                github.remove_label(issue.number, "ready-for-planning")
                continue
            comments = github.get_issue_comments(issue.number)
            existing_plan, new_comments = PlanningTask._analyze_planning_comments(
                comments, github.bot_name
            )
            if existing_plan is None or new_comments:
                yield PlanningTask(issue, existing_plan, new_comments)

    @staticmethod
    def _analyze_planning_comments(
        comments: list[Comment], bot_name: str
    ) -> tuple[str | None, list[Comment]]:
        """Return (existing_plan, new_user_comments_since_last_plan).

        Only a bot comment starting with PLAN_MARKER counts as a plan.
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

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

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
        github.post_comment(self.issue.number, f"{PLAN_MARKER}\n\n{result.summary}")

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.post_comment(
            self.issue.number,
            f"Planning failed: {error}",
        )
