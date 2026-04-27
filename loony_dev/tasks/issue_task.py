from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, Task
from loony_dev.tasks.planning_task import PLAN_MARKER, PLAN_MARKER_PREFIX

_MAX_HOOK_OUTPUT_CHARS = 500


def _sanitize_hook_output(output: str) -> str:
    """Return a safe truncated summary of hook output for public issue comments."""
    if len(output) <= _MAX_HOOK_OUTPUT_CHARS:
        return output
    return output[:_MAX_HOOK_OUTPUT_CHARS] + "\n[truncated]"

if TYPE_CHECKING:
    from loony_dev.github import Comment, Issue, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


class IssueTask(Task):
    task_type = "implement_issue"
    priority = 40

    def __init__(self, issue: Issue, plan: str | None = None) -> None:
        self.issue = issue
        self.plan = plan
        # State flags set by CodingAgent.execute_issue() before on_complete() runs.
        self.review_exhausted: bool = False
        self.commit_exhausted: bool = False
        self.hook_output: str | None = None

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[IssueTask]:
        """Yield implementation tasks for issues labeled ready-for-development."""
        from loony_dev.github import Issue

        for issue in Issue.list(label="ready-for-development", repo=repo):
            logger.debug("Examining issue #%d: %s", issue.number, issue.title)
            if issue.has_other_assignee(repo.bot_name):
                logger.debug(
                    "Issue #%d is assigned to %s — skipping (not our issue)",
                    issue.number, issue.assignees,
                )
                continue
            if "in-error" in issue.labels:
                logger.debug("Issue #%d is in-error — skipping", issue.number)
                continue
            comments = issue.comments
            plan = IssueTask._find_plan(comments, repo.bot_name)
            if plan is not None:
                logger.debug("Issue #%d has an approved plan (%d chars)", issue.number, len(plan))
            else:
                logger.debug("Issue #%d has no approved plan — will implement from issue body", issue.number)
            yield IssueTask(issue, plan=plan)

    @staticmethod
    def _find_plan(comments: list[Comment], bot_name: str) -> str | None:
        """Return the text of the most recent approved plan comment, or None."""
        plan: str | None = None
        for c in comments:
            if c.author == bot_name and c.body.startswith(PLAN_MARKER_PREFIX):
                end = c.body.find("-->")
                plan = c.body[end + 3:].strip() if end >= 0 else c.body[len(PLAN_MARKER):].strip()
        return plan

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return f"issue:{self.issue.number}"

    def describe(self) -> str:
        return self.implement_prompt()

    def implement_prompt(self) -> str:
        """Prompt for phase 1: write code only, no git operations."""
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
            f"- Do NOT commit, push, or create a pull request — stop after making code changes"
        )

    def fix_review_prompt(self, review_output: str) -> str:
        """Prompt for fixing issues reported by Coderabbit."""
        return (
            f"A Coderabbit code review found issues with your changes for "
            f"issue #{self.issue.number}. Please fix them. "
            f"Do NOT commit or push — only fix the code.\n\n"
            f"Review output:\n{review_output}"
        )

    def fix_hook_prompt(self, hook_output: str) -> str:
        """Prompt for fixing pre-commit/pre-push hook failures."""
        return (
            f"A git hook rejected the commit for issue #{self.issue.number}. "
            f"Please fix the code to satisfy the hook. "
            f"Do NOT commit or push — only fix the code.\n\n"
            f"Hook output:\n{hook_output}"
        )

    def commit_message_prompt(self) -> str:
        """Prompt asking Claude to output only a commit message."""
        return (
            f"Generate a conventional commit message for the changes made to implement "
            f"issue #{self.issue.number}: {self.issue.title}.\n\n"
            f"Output ONLY the commit message — no explanation, no preamble, no markdown fences. "
            f"The message must reference #{self.issue.number}."
        )

    def mark_review_exhausted(self) -> None:
        self.review_exhausted = True

    def mark_commit_exhausted(self, hook_output: str | None) -> None:
        self.commit_exhausted = True
        self.hook_output = hook_output

    def on_start(self, repo: Repo) -> None:
        logger.debug("Issue #%d: removing 'ready-for-development', adding 'in-progress'", self.issue.number)
        self.issue.remove_label("ready-for-development")
        self.issue.add_label("in-progress")
        self.issue.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug("Issue #%d: removing 'in-progress', posting completion comment", self.issue.number)
        logger.debug("Completion comment body: %s", truncate_for_log(result.summary))
        self.issue.remove_label("in-progress")

        status_notes = ""
        if self.commit_exhausted:
            status_notes += (
                "\n\n⚠️ Pre-commit/pre-push hooks failed after exhausting all retries. "
                "Committed as [WIP]."
            )
            if self.hook_output:
                safe_output = _sanitize_hook_output(self.hook_output)
                logger.debug("Full hook output: %s", truncate_for_log(self.hook_output))
                status_notes += (
                    f"\n\n<details><summary>Hook output</summary>\n\n"
                    f"```\n{safe_output}\n```\n</details>"
                )
        elif self.review_exhausted:
            status_notes += (
                "\n\n⚠️ Coderabbit review retries exhausted — some issues may remain."
            )

        self.issue.add_comment(
            f"{SUCCESS_MARKER}\n\nImplementation complete.{status_notes}\n\n{result.summary}",
        )

        author = self.issue.author
        if not author or author == repo.bot_name:
            return

        pr = self.issue.find_pr()
        if pr is None:
            logger.warning(
                "Could not find PR for issue #%d; skipping reviewer assignment",
                self.issue.number,
            )
            return

        try:
            pr.add_reviewer(author)
            logger.info("Assigned %s as reviewer on PR #%d", author, pr.number)
        except Exception as e:
            logger.warning(
                "Failed to assign reviewer %s on PR #%d: %s", author, pr.number, e
            )

    def on_failure(self, repo: Repo, error: Exception) -> None:
        logger.debug(
            "Issue #%d: task failed (%s), removing 'in-progress'",
            self.issue.number, error,
        )
        self.issue.remove_label("in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "Issue #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.issue.number,
            )
            self.issue.add_label("ready-for-development")
            return
        failure_body = f"{FAILURE_MARKER}\n\nImplementation failed: {error}"
        in_error = self.issue.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
        if not in_error:
            self.issue.add_label("ready-for-development")
