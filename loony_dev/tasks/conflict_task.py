from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, Task

if TYPE_CHECKING:
    from loony_dev.github import PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


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
            if not pr.is_assigned_to(repo.bot_name):
                continue
            if "in-progress" in pr.labels:
                continue
            if "in-error" in pr.labels:
                continue
            if pr.mergeable != "CONFLICTING":
                continue

            yield ConflictResolutionTask(pr, default_branch=default_branch)

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        # Must stay 1:1 with ``worktree_key``: the Claude transcript JSONL path
        # is derived from (cwd, session_id), so reusing a session id across two
        # worktrees makes the readiness wait time out. Distinct from the PR
        # review / CI session keys for the same PR for the same reason.
        return f"pr:{self.pr.number}:conflicts"

    @property
    def target_branch(self) -> str:
        return self.pr.branch

    @property
    def worktree_key(self) -> str:
        return f"pr-{self.pr.number}-conflicts"

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
