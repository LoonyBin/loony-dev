from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, Task, issue_or_pr_keys

if TYPE_CHECKING:
    from loony_dev.github import PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


def conflict_action(
    pr: PullRequest, bot_name: str, default_branch: str,
) -> ConflictResolutionTask | None:
    """Pure predicate: a conflict-resolution task for *pr* if it conflicts, else None."""
    if not pr.is_assigned_to(bot_name):
        return None
    if "in-progress" in pr.labels:
        return None
    if "in-error" in pr.labels:
        return None
    if pr.mergeable != "CONFLICTING":
        return None
    return ConflictResolutionTask(pr, default_branch=default_branch)


class ConflictResolutionTask(Task):
    task_type = "resolve_conflicts"
    priority = 10
    command_name = "resolve-conflicts"

    def __init__(self, pr: PullRequest, default_branch: str = "main") -> None:
        self.pr = pr
        self.default_branch = default_branch

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[ConflictResolutionTask]:
        """Yield PRs that are in a CONFLICTING state with the default branch."""
        from loony_dev.github import PullRequest

        default_branch = repo.detect_default_branch()
        for pr in PullRequest.list_open(repo=repo):
            task = conflict_action(pr, repo.bot_name, default_branch)
            if task is not None:
                yield task

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return issue_or_pr_keys(self.pr)[0]

    @property
    def target_branch(self) -> str:
        return self.pr.branch

    @property
    def worktree_key(self) -> str:
        return issue_or_pr_keys(self.pr)[1]

    def describe(self) -> str:
        """Human-readable label for logging/dashboard (not sent as a turn).

        The work is driven via the ``/resolve-conflicts`` slash command built
        from :meth:`context_payload` (issue #166).
        """
        return f"Resolve merge conflicts on PR #{self.pr.number}: {self.pr.title}"

    def context_payload(self) -> dict:
        """Context for ``/resolve-conflicts``."""
        return {
            "pr_number": self.pr.number,
            "title": self.pr.title,
            "branch": self.pr.branch,
            "default_branch": self.default_branch,
        }

    def on_start(self, repo: Repo) -> None:
        self.pr.add_label("in-progress")
        self.pr.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        self.pr.remove_label("in-progress")
        self.pr.add_comment(
            f"{SUCCESS_MARKER}\n\nMerge conflicts resolved.\n\n{result.summary}",
        )

    def on_failure(self, repo: Repo, error: Exception) -> None:
        self.pr.remove_label("in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        failure_body = (
            f"{FAILURE_MARKER}\n\nFailed to resolve merge conflicts: {error}\n\n"
            f"Manual intervention is required."
        )
        self.pr.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
