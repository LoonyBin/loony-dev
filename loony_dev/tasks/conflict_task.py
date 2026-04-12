from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import PullRequest, RateLimitedError
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
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
    def discover(github: GitHubClient) -> Iterator[ConflictResolutionTask]:
        """Yield PRs that are in a CONFLICTING state with the default branch."""
        default_branch = github.detect_default_branch()
        for item in github.list_open_prs():
            if not github.is_assigned_to_bot(item):
                continue

            labels = [l["name"] for l in item.get("labels", [])]
            if "in-progress" in labels:
                continue

            if item.get("mergeable") != "CONFLICTING":
                continue

            yield ConflictResolutionTask(
                PullRequest(
                    number=item["number"],
                    branch=item["headRefName"],
                    title=item["title"],
                    mergeable=item.get("mergeable"),
                ),
                default_branch=default_branch,
            )

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

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

    def on_start(self, github: GitHubClient) -> None:
        github.add_label(self.pr.number, "in-progress")
        github.assign_self(self.pr.number)

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        github.remove_label(self.pr.number, "in-progress")
        github.post_comment(
            self.pr.number,
            f"{SUCCESS_MARKER}\n\nMerge conflicts resolved.\n\n{result.summary}",
        )

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        github.remove_label(self.pr.number, "in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        github.post_comment(
            self.pr.number,
            f"{FAILURE_MARKER}\n\nFailed to resolve merge conflicts: {error}\n\nManual intervention is required.",
        )
