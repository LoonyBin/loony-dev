from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import (
    FAILURE_MARKER,
    Task,
    _slugify,
    decode_last_seen,
    encode_marker,
)

if TYPE_CHECKING:
    from loony_dev.github import Comment, Issue, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)

PLAN_MARKER_PREFIX = "<!-- loony-plan"
PLAN_MARKER = "<!-- loony-plan -->"  # legacy fixed string; kept for backward compatibility
REVISION_NOTE_DELIMITER = "<!-- loony-revision-note -->"

# Matches a trailing `**Revision note:**` heading at the start of a line whose
# content runs to end-of-string. Greedy `.*` ensures we anchor on the LAST such
# heading, so an embedded `**Revision note:**` earlier in the plan body doesn't
# truncate valid content.
_REVISION_NOTE_FALLBACK_RE = re.compile(
    r"(?s)\A(.*)(?:\n|\A)\*\*Revision note:\*\*\s*(.+?)\s*\Z"
)


def _split_revision_note(summary: str) -> tuple[str, str]:
    """Split agent output into (plan, revision_note) on the revision-note delimiter.

    Falls back to splitting on a trailing `**Revision note:**` heading if the explicit
    delimiter is absent (older outputs). Returns ``(summary, "")`` if neither marker
    is present.
    """
    if REVISION_NOTE_DELIMITER in summary:
        plan, _, note = summary.partition(REVISION_NOTE_DELIMITER)
    else:
        match = _REVISION_NOTE_FALLBACK_RE.match(summary.strip())
        if match is None:
            return summary.strip(), ""
        plan, note = match.group(1), match.group(2)
    plan = plan.rstrip()
    while plan.endswith("---"):
        plan = plan[:-3].rstrip()
    return plan.strip(), note.strip()


class PlanningTask(Task):
    task_type = "plan_issue"
    priority = 30

    def __init__(
        self,
        issue: Issue,
        existing_plan: str | None,
        new_comments: list[Comment],
        existing_plan_comment_id: int | None = None,
    ) -> None:
        self.issue = issue
        self.existing_plan = existing_plan
        self.new_comments = new_comments
        self.existing_plan_comment_id = existing_plan_comment_id

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
            existing_plan, existing_plan_comment_id, new_comments = PlanningTask._analyze_planning_comments(
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
                    yield PlanningTask(issue, existing_plan, authorized_new, existing_plan_comment_id)
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
    ) -> tuple[str | None, int | None, list[Comment]]:
        """Return (existing_plan, existing_plan_comment_id, new_user_comments_since_last_plan)."""
        bot_last_plan_idx = -1
        bot_last_plan: str | None = None
        bot_last_plan_comment_id: int | None = None

        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(PLAN_MARKER_PREFIX):
                bot_last_plan_idx = i
                bot_last_plan_comment_id = c.id
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

        return bot_last_plan, bot_last_plan_comment_id, new_comments

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def branch_name(self) -> str:
        """The issue's feature branch — identical to ``IssueTask.branch_name``.

        Planning runs in the ``issue-N`` worktree on this branch (#181) so that
        planning -> implementation is one cwd / one session with no cross-worktree
        reuse. The branch is created from the default branch at planning time
        (it does not exist yet) and is already present when implementation runs.

        Slug stability: the slug derives from the *current* issue title. If the
        title is edited between planning and implementation the branch keeps its
        original name (we never rename a live branch); a stale slug only affects
        the human-readable suffix, not the ``issue-N`` worktree identity.
        """
        return f"issue-{self.issue.number}/{_slugify(self.issue.title)}"

    @property
    def session_key(self) -> str:
        return f"issue:{self.issue.number}"

    @property
    def worktree_key(self) -> str:
        return f"issue-{self.issue.number}"

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
            f"Output the updated plan in well-structured markdown. Then on a new line emit "
            f"the literal delimiter `{REVISION_NOTE_DELIMITER}` (exactly, on its own line), "
            f"followed by a short (2-4 sentence) revision note summarising what changed in this "
            f"revision and any questions or pushback you have about the feedback. The plan must "
            f"come before the delimiter and the revision note after it; do not include the "
            f"delimiter anywhere else.\n\n"
            f"Do NOT implement anything — planning only."
        )

    def on_start(self, repo: Repo) -> None:
        logger.debug("Issue #%d: starting planning (keeping 'ready-for-planning' label)", self.issue.number)
        self.issue.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug(
            "Issue #%d: %s plan (%d chars): %s",
            self.issue.number,
            "updating" if self.existing_plan_comment_id else "posting",
            len(result.summary),
            truncate_for_log(result.summary),
        )
        last_seen_ts = max((c.created_at for c in self.new_comments), default="")
        marker = encode_marker(PLAN_MARKER_PREFIX, last_seen_ts) if last_seen_ts else PLAN_MARKER
        plan_text, revision_note = _split_revision_note(result.summary)
        if not plan_text.strip():
            logger.warning(
                "Issue #%d: parsed plan text is empty; preserving raw summary to avoid data loss",
                self.issue.number,
            )
            plan_text = result.summary.strip()
        body = f"{marker}\n\n{plan_text}"
        if self.existing_plan_comment_id is not None:
            self.issue.edit_comment(self.existing_plan_comment_id, body)
            if revision_note:
                self.issue.add_comment(f"**Revision note:**\n\n{revision_note}")
        else:
            self.issue.add_comment(body)

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
