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
        return f"pr:{self.pr.number}"

    @property
    def target_branch(self) -> str:
        return self.pr.branch

    def describe(self) -> str:
        return (
            f"Resolve merge conflicts on PR #{self.pr.number}: {self.pr.title}\n\n"
            f"The branch '{self.pr.branch}' has conflicts with {self.default_branch} that must be resolved before merging.\n\n"
            f"Instructions:\n"
            f"- Run: git checkout {self.pr.branch}\n"
            f"- Run: git merge {self.default_branch}\n"
            f"- If conflicts arise, read each conflicting file, understand the intent of both sides,\n"
            f"  and resolve the markers appropriately\n"
            f"- Stage resolved files and run: git merge --continue\n"
            f"- Push: git push --force-with-lease\n"
            f"- Do NOT create a new PR or commit unrelated changes"
        )

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
