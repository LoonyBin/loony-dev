from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, Task, _slugify
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


def issue_action(issue: Issue, repo: Repo) -> IssueTask | None:
    """Pure predicate: an implementation task for *issue* if it is ready, else None."""
    if "ready-for-development" not in issue.labels:
        return None
    if issue.has_other_assignee(repo.bot_name):
        logger.debug(
            "Issue #%d is assigned to %s — skipping (not our issue)",
            issue.number, issue.assignees,
        )
        return None
    if "in-error" in issue.labels:
        logger.debug("Issue #%d is in-error — skipping", issue.number)
        return None
    comments = issue.comments
    plan = IssueTask._find_plan(comments, repo.bot_name)
    if plan is not None:
        logger.debug("Issue #%d has an approved plan (%d chars)", issue.number, len(plan))
    else:
        logger.debug("Issue #%d has no approved plan — will implement from issue body", issue.number)
    return IssueTask(issue, plan=plan)


class IssueTask(Task):
    task_type = "implement_issue"
    priority = 40

    def __init__(self, issue: Issue, plan: str | None = None) -> None:
        self.issue = issue
        self.plan = plan
        # State flags set by CodingAgent.execute_issue() before on_complete() runs.
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
            task = issue_action(issue, repo)
            if task is not None:
                yield task

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
    def branch_name(self) -> str:
        return f"issue-{self.issue.number}/{_slugify(self.issue.title)}"

    @property
    def worktree_key(self) -> str:
        return f"issue-{self.issue.number}"

    @property
    def session_key(self) -> str:
        return f"issue:{self.issue.number}"

    def describe(self) -> str:
        """Human-readable label for logging/dashboard (not sent as a turn).

        The actual work is driven by ``execute_issue`` via the ``/implement-issue``
        slash command built from :meth:`implement_payload` (issue #166).
        """
        return f"Implement issue #{self.issue.number}: {self.issue.title}"

    def implement_payload(self) -> dict:
        """Context for ``/implement-issue`` (phase 1: write code, no git ops)."""
        payload: dict = {
            "issue_number": self.issue.number,
            "title": self.issue.title,
            "body": self.issue.body,
        }
        if self.plan is not None:
            payload["plan"] = self.plan
        return payload

    def fix_review_payload(self, review_output: str) -> dict:
        """Context for ``/fix-review`` (fix issues reported by CodeRabbit)."""
        return {
            "issue_number": self.issue.number,
            "review_output": review_output,
        }

    def fix_hook_payload(self, hook_output: str) -> dict:
        """Context for ``/fix-hook`` (fix pre-commit/pre-push hook failures)."""
        return {
            "issue_number": self.issue.number,
            "hook_output": hook_output,
        }

    def commit_message_payload(self) -> dict:
        """Context for ``/commit-message`` (conventional commit message only)."""
        return {
            "issue_number": self.issue.number,
            "title": self.issue.title,
        }

    def pr_body_payload(self, diff: str) -> dict:
        """Context for ``/pr-body`` (write a GitHub PR body)."""
        return {
            "issue_number": self.issue.number,
            "title": self.issue.title,
            "body": self.issue.body,
            "diff": diff,
        }

    def mark_commit_exhausted(self, hook_output: str | None) -> None:
        self.commit_exhausted = True
        self.hook_output = hook_output

    def on_start(self, repo: Repo) -> None:
        logger.debug("Issue #%d: removing 'ready-for-development', adding 'in-progress'", self.issue.number)
        # Reconcile a stale 'ready-for-planning' label: an approved issue can
        # still carry it from the planning phase (it is kept while awaiting
        # approval). The transition to implementation is the right point to drop
        # it — moved here from the old planning-discovery side effect (#197) so
        # pipeline discovery stays a pure read.
        if "ready-for-planning" in self.issue.labels:
            logger.debug("Issue #%d: removing stale 'ready-for-planning' label", self.issue.number)
            self.issue.remove_label("ready-for-planning")
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
