"""Tests asserting that PR task discover() methods skip PRs not assigned to the bot.

Issue #80: PR tasks (ConflictResolutionTask, CIFailureTask, StuckItemCleanupTask,
PRReviewTask) must only process PRs where the bot is listed as an assignee, to
avoid interfering with PRs owned by other developers.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github.pull_request import PullRequest
from loony_dev.github.issue import Issue
from loony_dev.tasks.ci_failure_task import CIFailureTask
from loony_dev.tasks.conflict_task import ConflictResolutionTask
from loony_dev.tasks.pr_review_task import PRReviewTask
from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask

BOT_NAME = "loony-bot"
OTHER_USER = "alice"
REVIEWER = "alice"


def _make_repo(pr_data: list[dict], issue_data: list[dict] | None = None) -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT_NAME
    repo._tick_cache = {}
    repo._check_runs_cache = {}
    repo.skip_ci_checks = set()
    repo.is_authorized = MagicMock(return_value=True)

    prs = [PullRequest._from_api(d, repo) for d in pr_data]
    repo._tick_cache["open_prs"] = prs

    if issue_data:
        issues = [Issue(
            number=d["number"], title=d.get("title", ""), body=d.get("body", ""),
            updated_at=d.get("updated_at"), labels=d.get("labels", []), _repo=repo,
        ) for d in issue_data]
    else:
        issues = []
    # For Issue.list() — we'll patch it
    return repo, issues


def _pr_data(
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
        repo, _ = _make_repo([_pr_data(assigned_to_bot=True, mergeable="CONFLICTING")])
        tasks = list(ConflictResolutionTask.discover(repo))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_skips_unassigned_conflicting_pr(self) -> None:
        repo, _ = _make_repo([_pr_data(assigned_to_bot=False, mergeable="CONFLICTING")])
        tasks = list(ConflictResolutionTask.discover(repo))
        self.assertEqual(tasks, [])

    def test_skips_assigned_non_conflicting_pr(self) -> None:
        repo, _ = _make_repo([_pr_data(assigned_to_bot=True, mergeable="MERGEABLE")])
        tasks = list(ConflictResolutionTask.discover(repo))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# CIFailureTask
# ---------------------------------------------------------------------------

class TestCIFailureTaskFilter(unittest.TestCase):

    def _make_ci_repo(self, assigned_to_bot: bool, has_failures: bool = True) -> MagicMock:
        from loony_dev.github.check_run import CheckRun

        repo, _ = _make_repo([_pr_data(assigned_to_bot=assigned_to_bot, mergeable="MERGEABLE")])
        if has_failures:
            failing = [CheckRun(name="test", status="completed", conclusion="failure", details_url="https://example.com")]
        else:
            failing = []

        # Mock the check_runs API response
        repo.client.gh_api.return_value = {
            "check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure", "details_url": "https://example.com"}
            ] if has_failures else []
        }
        return repo

    def test_yields_assigned_pr_with_failures(self) -> None:
        repo = self._make_ci_repo(assigned_to_bot=True)
        tasks = list(CIFailureTask.discover(repo))
        self.assertEqual(len(tasks), 1)

    def test_skips_unassigned_pr_with_failures(self) -> None:
        repo = self._make_ci_repo(assigned_to_bot=False)
        tasks = list(CIFailureTask.discover(repo))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# StuckItemCleanupTask
# ---------------------------------------------------------------------------

class TestStuckItemCleanupTaskFilter(unittest.TestCase):

    def _make_stuck_repo(self, assigned_to_bot: bool) -> MagicMock:
        repo, _ = _make_repo(
            [_pr_data(assigned_to_bot=assigned_to_bot, labels=["in-progress"], mergeable="MERGEABLE")],
        )
        return repo

    def test_yields_assigned_stuck_pr(self) -> None:
        repo = self._make_stuck_repo(assigned_to_bot=True)
        with patch("loony_dev.github.issue.Issue.list", return_value=[]):
            with patch("loony_dev.config.settings") as mock_settings:
                mock_settings.get.return_value = 0  # threshold = 0 hours, always stuck
                tasks = list(StuckItemCleanupTask.discover(repo))
        self.assertEqual(len(tasks), 1)

    def test_skips_unassigned_stuck_pr(self) -> None:
        repo = self._make_stuck_repo(assigned_to_bot=False)
        with patch("loony_dev.github.issue.Issue.list", return_value=[]):
            with patch("loony_dev.config.settings") as mock_settings:
                mock_settings.get.return_value = 0
                tasks = list(StuckItemCleanupTask.discover(repo))
        self.assertEqual(tasks, [])


# ---------------------------------------------------------------------------
# PRReviewTask
# ---------------------------------------------------------------------------

class TestPRReviewTaskFilter(unittest.TestCase):

    def _make_review_repo(self, assigned_to_bot: bool) -> MagicMock:
        repo, _ = _make_repo([_pr_data(assigned_to_bot=assigned_to_bot, mergeable="MERGEABLE")])
        # Return inline comments from an authorized reviewer
        repo.client.gh_api.return_value = [
            {
                "user": {"login": REVIEWER},
                "body": "Fix this.",
                "created_at": "2024-01-01T01:00:00Z",
                "path": "foo.py",
                "line": 10,
                "pull_request_review_id": None,
            }
        ]
        return repo

    def test_yields_assigned_pr_with_new_comments(self) -> None:
        repo = self._make_review_repo(assigned_to_bot=True)
        tasks = list(PRReviewTask.discover(repo))
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_skips_unassigned_pr_with_new_comments(self) -> None:
        repo = self._make_review_repo(assigned_to_bot=False)
        tasks = list(PRReviewTask.discover(repo))
        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
