"""Tests asserting that PR task discover() methods skip PRs not assigned to the bot.

Issue #80: PR tasks (ConflictResolutionTask, CIFailureTask, StuckItemCleanupTask,
PRReviewTask) must only process PRs where the bot is listed as an assignee, to
avoid interfering with PRs owned by other developers.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from loony_dev.tasks.ci_failure_task import CIFailureTask
from loony_dev.tasks.conflict_task import ConflictResolutionTask
from loony_dev.tasks.pr_review_task import PRReviewTask
from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask

BOT_NAME = "loony-bot"
OTHER_USER = "alice"
REVIEWER = "alice"


def _make_github(prs: list[dict], bot_name: str = BOT_NAME) -> MagicMock:
    github = MagicMock()
    github.bot_name = bot_name
    github.list_open_prs.return_value = prs
    github.is_assigned_to_bot.side_effect = lambda pr: any(
        a.get("login", "") == bot_name for a in pr.get("assignees", [])
    )
    return github


def _pr(
    number: int = 1,
    assigned_to_bot: bool = True,
    mergeable: str = "CONFLICTING",
    labels: list[str] | None = None,
    head_sha: str = "abc123",
) -> dict:
    assignees = [{"login": BOT_NAME}] if assigned_to_bot else [{"login": OTHER_USER}]
    return {
        "number": number,
        "headRefName": f"feature/branch-{number}",
        "headRefOid": head_sha,
        "title": f"PR #{number}",
        "comments": [],
        "reviews": [],
        "labels": [{"name": l} for l in (labels or [])],
        "mergeable": mergeable,
        "updatedAt": "2024-01-01T00:00:00Z",
        "assignees": assignees,
    }


# ---------------------------------------------------------------------------
# ConflictResolutionTask
# ---------------------------------------------------------------------------

class TestConflictResolutionTaskFilter(unittest.TestCase):

    def test_yields_assigned_conflicting_pr(self) -> None:
        github = _make_github([_pr(assigned_to_bot=True, mergeable="CONFLICTING")])
        tasks = list(ConflictResolutionTask.discover(github))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_skips_unassigned_conflicting_pr(self) -> None:
        github = _make_github([_pr(assigned_to_bot=False, mergeable="CONFLICTING")])
        tasks = list(ConflictResolutionTask.discover(github))
        self.assertEqual(tasks, [])

    def test_skips_assigned_non_conflicting_pr(self) -> None:
        github = _make_github([_pr(assigned_to_bot=True, mergeable="MERGEABLE")])
        tasks = list(ConflictResolutionTask.discover(github))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# CIFailureTask
# ---------------------------------------------------------------------------

class TestCIFailureTaskFilter(unittest.TestCase):

    def _make_ci_github(self, assigned_to_bot: bool, has_failures: bool = True) -> MagicMock:
        from loony_dev.models import CheckRun
        github = _make_github([_pr(assigned_to_bot=assigned_to_bot, mergeable="MERGEABLE")])
        if has_failures:
            github.get_pr_check_runs.return_value = [
                CheckRun(name="test", status="completed", conclusion="failure", details_url="https://example.com")
            ]
        else:
            github.get_pr_check_runs.return_value = []
        return github

    def test_yields_assigned_pr_with_failures(self) -> None:
        github = self._make_ci_github(assigned_to_bot=True)
        tasks = list(CIFailureTask.discover(github))
        self.assertEqual(len(tasks), 1)

    def test_skips_unassigned_pr_with_failures(self) -> None:
        github = self._make_ci_github(assigned_to_bot=False)
        tasks = list(CIFailureTask.discover(github))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# StuckItemCleanupTask
# ---------------------------------------------------------------------------

class TestStuckItemCleanupTaskFilter(unittest.TestCase):

    def _make_stuck_github(self, assigned_to_bot: bool) -> MagicMock:
        pr = _pr(
            assigned_to_bot=assigned_to_bot,
            labels=["in-progress"],
            mergeable="MERGEABLE",
        )
        # Make updatedAt old enough to trigger stuck detection (13 hours ago)
        pr["updatedAt"] = "2024-01-01T00:00:00Z"
        github = _make_github([pr])
        github.list_issues.return_value = []
        return github

    def test_yields_assigned_stuck_pr(self) -> None:
        github = self._make_stuck_github(assigned_to_bot=True)
        with patch("loony_dev.config.settings") as mock_settings:
            mock_settings.get.return_value = 0  # threshold = 0 hours, always stuck
            tasks = list(StuckItemCleanupTask.discover(github))
        self.assertEqual(len(tasks), 1)

    def test_skips_unassigned_stuck_pr(self) -> None:
        github = self._make_stuck_github(assigned_to_bot=False)
        with patch("loony_dev.config.settings") as mock_settings:
            mock_settings.get.return_value = 0
            tasks = list(StuckItemCleanupTask.discover(github))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# PRReviewTask
# ---------------------------------------------------------------------------

class TestPRReviewTaskFilter(unittest.TestCase):

    def _make_review_github(self, assigned_to_bot: bool) -> MagicMock:
        from loony_dev.models import Comment
        pr = _pr(assigned_to_bot=assigned_to_bot, mergeable="MERGEABLE")
        github = _make_github([pr])
        # Provide an inline comment from an authorized reviewer
        github.get_pr_inline_comments.return_value = [
            Comment(author=REVIEWER, body="Fix this.", created_at="2024-01-01T01:00:00Z")
        ]
        return github

    def test_yields_assigned_pr_with_new_comments(self) -> None:
        github = self._make_review_github(assigned_to_bot=True)
        with patch("loony_dev.tasks.pr_review_task.is_authorized", return_value=True):
            tasks = list(PRReviewTask.discover(github))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_skips_unassigned_pr_with_new_comments(self) -> None:
        github = self._make_review_github(assigned_to_bot=False)
        with patch("loony_dev.tasks.pr_review_task.is_authorized", return_value=True):
            tasks = list(PRReviewTask.discover(github))
        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
