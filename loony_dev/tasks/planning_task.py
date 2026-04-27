from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import FAILURE_MARKER, Task, decode_last_seen, encode_marker

if TYPE_CHECKING:
    from loony_dev.github import Comment, Issue, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)

PLAN_MARKER_PREFIX = "<!-- loony-plan"
PLAN_MARKER = "<!-- loony-plan -->"  # legacy fixed string; kept for backward compatibility


class PlanningTask(Task):
    task_type = "plan_issue"
    priority = 30

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
    def discover(repo: Repo) -> Iterator[PlanningTask]:
        """Yield planning tasks for issues that need a new or revised plan."""
        from loony_dev.github import Issue

        for issue in Issue.list(label="ready-for-planning", repo=repo):
            logger.debug("Examining issue #%d: %s (labels=%s)", issue.number, issue.title, issue.labels)
            if issue.has_other_assignee(repo.bot_name):
                logger.debug(
                    "Issue #%d is assigned to %s — skipping (not our issue)",
                    issue.number, issue.assignees,
                )
                continue
            if "in-error" in issue.labels:
                logger.debug("Issue #%d is in-error — skipping", issue.number)
                continue
            if "ready-for-development" in issue.labels:
                logger.debug(
                    "Issue #%d has 'ready-for-development' — plan approved, removing 'ready-for-planning'",
                    issue.number,
                )
                issue.remove_label("ready-for-planning")
                continue
            comments = issue.comments
            existing_plan, new_comments = PlanningTask._analyze_planning_comments(
                comments, repo.bot_name
            )
            if existing_plan is not None:
                logger.debug(
                    "Issue #%d: existing plan found (%d chars), %d new comment(s) since last plan",
                    issue.number, len(existing_plan), len(new_comments),
                )
            else:
                logger.debug("Issue #%d: no existing plan — will create initial plan", issue.number)

            if existing_plan is None:
                yield PlanningTask(issue, existing_plan, new_comments)
            elif new_comments:
                authorized_new = [
                    c for c in new_comments
                    if repo.is_authorized(c.author)
                ]
                if authorized_new:
                    yield PlanningTask(issue, existing_plan, authorized_new)
                else:
                    logger.debug(
                        "Issue #%d: %d new comment(s) but none from authorized users — skipping",
                        issue.number, len(new_comments),
                    )
            else:
                logger.debug("Issue #%d: plan exists and no new feedback — skipping", issue.number)

    @staticmethod
    def _analyze_planning_comments(
        comments: list[Comment], bot_name: str
    ) -> tuple[str | None, list[Comment]]:
        """Return (existing_plan, new_user_comments_since_last_plan)."""
        bot_last_plan_idx = -1
        bot_last_plan: str | None = None

        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(PLAN_MARKER_PREFIX):
                bot_last_plan_idx = i
                end = c.body.find("-->")
                bot_last_plan = c.body[end + 3:].strip() if end >= 0 else c.body[len(PLAN_MARKER):].strip()

        if bot_last_plan_idx == -1:
            new_comments = [c for c in comments if c.author != bot_name]
        else:
            last_seen = decode_last_seen(comments[bot_last_plan_idx].body)
            if last_seen is not None:
                new_comments = [c for c in comments if c.author != bot_name and c.created_at > last_seen]
            else:
                new_comments = [
                    c for c in comments[bot_last_plan_idx + 1:] if c.author != bot_name
                ]

        return bot_last_plan, new_comments

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return f"issue:{self.issue.number}"

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

    def on_start(self, repo: Repo) -> None:
        logger.debug("Issue #%d: starting planning (keeping 'ready-for-planning' label)", self.issue.number)
        self.issue.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug(
            "Issue #%d: posting plan (%d chars): %s",
            self.issue.number, len(result.summary), truncate_for_log(result.summary),
        )
        last_seen_ts = max((c.created_at for c in self.new_comments), default="")
        marker = encode_marker(PLAN_MARKER_PREFIX, last_seen_ts) if last_seen_ts else PLAN_MARKER
        self.issue.add_comment(f"{marker}\n\n{result.summary}")

    def on_failure(self, repo: Repo, error: Exception) -> None:
        logger.debug("Issue #%d: planning failed (%s)", self.issue.number, error)
        if isinstance(error, RateLimitedError):
            logger.info(
                "Issue #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.issue.number,
            )
            return
        failure_body = f"{FAILURE_MARKER}\n\nPlanning failed: {error}"
        self.issue.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
