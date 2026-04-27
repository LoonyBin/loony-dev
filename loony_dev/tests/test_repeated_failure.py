"""Tests for the repeated-failure detection logic (issue #111).

Covers:
- _normalize_failure_body: marker stripping for stable comparisons
- GitHubItem._recent_bot_failure_comments: filtering and ordering
- GitHubItem.check_and_post_failure: all decision branches
- discover() in-error guard for IssueTask, PlanningTask, CIFailureTask,
  ConflictResolutionTask, PRReviewTask, and StuckItemCleanupTask
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.github.issue import GitHubItem, Issue, _normalize_failure_body
from loony_dev.github.pull_request import PullRequest
from loony_dev.tasks.base import (
    CI_FAILURE_MARKER,
    FAILURE_MARKER,
    FAILURE_MARKER_PREFIX,
    IN_ERROR_MARKER,
    encode_marker,
)

BOT = "loony-bot"
OWNER = "loony-org"
REPO_NAME = f"{OWNER}/loony-dev"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comment(author: str, body: str, ts: str = "2024-01-01T10:00:00Z") -> Comment:
    return Comment(author=author, body=Content(body, safe=(author == BOT)), created_at=ts)


def _failure(body_suffix: str = "Implementation failed: oops", ts: str = "2024-01-01T10:00:00Z") -> Comment:
    return _comment(BOT, f"{FAILURE_MARKER}\n\n{body_suffix}", ts)


def _ci_failure(body_suffix: str = "CI failed", ts: str = "2024-01-01T10:00:00Z") -> Comment:
    return _comment(BOT, f"{CI_FAILURE_MARKER}\n\n{body_suffix}", ts)


def _mock_repo(threshold: int = 2) -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT
    repo.owner = OWNER
    repo.repeated_failure_threshold = threshold
    repo.name = REPO_NAME
    return repo


def _mock_item(comments: list[Comment], author: str = "alice") -> GitHubItem:
    """Return a minimal GitHubItem whose get_comments() is patched."""
    repo = _mock_repo()
    item = Issue(number=1, author=author, _repo=repo)
    item.get_comments = MagicMock(return_value=comments)  # type: ignore[method-assign]
    item.add_comment = MagicMock()  # type: ignore[method-assign]
    item.add_label = MagicMock()  # type: ignore[method-assign]
    return item


# ---------------------------------------------------------------------------
# _normalize_failure_body
# ---------------------------------------------------------------------------


class TestNormalizeFailureBody(unittest.TestCase):

    def test_strips_plain_marker(self) -> None:
        body = f"{FAILURE_MARKER}\n\nImplementation failed: boom"
        self.assertEqual(_normalize_failure_body(body), "Implementation failed: boom")

    def test_strips_ci_failure_marker(self) -> None:
        body = f"{CI_FAILURE_MARKER}\n\nCI broke"
        self.assertEqual(_normalize_failure_body(body), "CI broke")

    def test_strips_encoded_marker_with_last_seen(self) -> None:
        marker = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-01T09:00:00Z")
        body = f"{marker}\n\nFailed to address review comments: err"
        self.assertEqual(
            _normalize_failure_body(body),
            "Failed to address review comments: err",
        )

    def test_strips_leading_whitespace_after_marker(self) -> None:
        body = f"{FAILURE_MARKER}\n\n\n  Real content"
        self.assertEqual(_normalize_failure_body(body), "Real content")

    def test_body_without_marker_returned_as_is(self) -> None:
        body = "Just some text"
        self.assertEqual(_normalize_failure_body(body), "Just some text")

    def test_empty_body(self) -> None:
        self.assertEqual(_normalize_failure_body(""), "")

    def test_only_marker_line(self) -> None:
        self.assertEqual(_normalize_failure_body(FAILURE_MARKER), "")

    def test_two_encoded_markers_different_timestamps_normalize_same(self) -> None:
        marker_a = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-01T09:00:00Z")
        marker_b = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-02T10:00:00Z")
        body_a = f"{marker_a}\n\nFailed: same error"
        body_b = f"{marker_b}\n\nFailed: same error"
        self.assertEqual(
            _normalize_failure_body(body_a),
            _normalize_failure_body(body_b),
        )


# ---------------------------------------------------------------------------
# _recent_bot_failure_comments
# ---------------------------------------------------------------------------


class TestRecentBotFailureComments(unittest.TestCase):

    def test_returns_only_bot_failure_comments(self) -> None:
        comments = [
            _comment("alice", "LGTM"),
            _failure(ts="2024-01-01T10:00:00Z"),
            _comment("alice", "Fix this", ts="2024-01-01T11:00:00Z"),
        ]
        item = _mock_item(comments)
        result = item._recent_bot_failure_comments(BOT, 2)
        self.assertEqual(len(result), 1)
        self.assertIn(FAILURE_MARKER, str(result[0].body))

    def test_returns_last_n_when_more_exist(self) -> None:
        comments = [
            _failure("err1", ts="2024-01-01T08:00:00Z"),
            _failure("err2", ts="2024-01-01T09:00:00Z"),
            _failure("err3", ts="2024-01-01T10:00:00Z"),
        ]
        item = _mock_item(comments)
        result = item._recent_bot_failure_comments(BOT, 2)
        self.assertEqual(len(result), 2)
        self.assertIn("err2", str(result[0].body))
        self.assertIn("err3", str(result[1].body))

    def test_ci_failure_marker_is_included(self) -> None:
        comments = [
            _ci_failure(ts="2024-01-01T10:00:00Z"),
            _ci_failure(ts="2024-01-01T11:00:00Z"),
        ]
        item = _mock_item(comments)
        result = item._recent_bot_failure_comments(BOT, 2)
        self.assertEqual(len(result), 2)

    def test_excludes_success_and_plan_comments(self) -> None:
        from loony_dev.tasks.base import SUCCESS_MARKER
        comments = [
            _comment(BOT, f"{SUCCESS_MARKER}\n\nDone."),
            _comment(BOT, "<!-- loony-plan -->\n\nPlan text."),
            _failure(ts="2024-01-01T10:00:00Z"),
        ]
        item = _mock_item(comments)
        result = item._recent_bot_failure_comments(BOT, 2)
        self.assertEqual(len(result), 1)

    def test_returns_fewer_than_n_when_not_enough_exist(self) -> None:
        comments = [_failure(ts="2024-01-01T10:00:00Z")]
        item = _mock_item(comments)
        result = item._recent_bot_failure_comments(BOT, 3)
        self.assertEqual(len(result), 1)

    def test_returns_empty_when_no_failure_comments(self) -> None:
        item = _mock_item([_comment("alice", "Hello")])
        result = item._recent_bot_failure_comments(BOT, 2)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# check_and_post_failure
# ---------------------------------------------------------------------------


class TestCheckAndPostFailure(unittest.TestCase):

    def _make_failure_body(self, msg: str = "Implementation failed: oops") -> str:
        return f"{FAILURE_MARKER}\n\n{msg}"

    def test_not_enough_history_posts_regular_comment(self) -> None:
        """Fewer than N prior failures → post regular failure comment."""
        item = _mock_item([_failure(ts="2024-01-01T10:00:00Z")])
        body = self._make_failure_body()
        result = item.check_and_post_failure(body, BOT, 2, OWNER)

        self.assertFalse(result)
        item.add_comment.assert_called_once_with(body)
        item.add_label.assert_not_called()

    def test_non_identical_failures_posts_regular_comment(self) -> None:
        """N prior failures exist but body differs → post regular failure comment."""
        different = _failure("Different error entirely", ts="2024-01-01T09:00:00Z")
        also_different = _failure("Another different error", ts="2024-01-01T10:00:00Z")
        item = _mock_item([different, also_different])
        body = self._make_failure_body("Implementation failed: oops")
        result = item.check_and_post_failure(body, BOT, 2, OWNER)

        self.assertFalse(result)
        item.add_comment.assert_called_once_with(body)
        item.add_label.assert_not_called()

    def test_identical_failures_triggers_in_error(self) -> None:
        """N identical prior failures → apply in-error label, post sleeping notice."""
        body = self._make_failure_body()
        prior_a = _failure(ts="2024-01-01T09:00:00Z")
        prior_b = _failure(ts="2024-01-01T10:00:00Z")
        item = _mock_item([prior_a, prior_b], author="alice")

        result = item.check_and_post_failure(body, BOT, 2, OWNER)

        self.assertTrue(result)
        item.add_label.assert_called_once_with("in-error")
        posted_body: str = item.add_comment.call_args[0][0]
        self.assertTrue(posted_body.startswith(IN_ERROR_MARKER))
        self.assertIn("@alice", posted_body)
        self.assertIn(body, posted_body)

    def test_identical_failures_with_different_markers_triggers_in_error(self) -> None:
        """Encoded markers with different timestamps normalize the same."""
        msg = "Failed to address review comments: timeout"
        marker_a = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-01T09:00:00Z")
        marker_b = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-01T10:00:00Z")
        prior_a = _comment(BOT, f"{marker_a}\n\n{msg}", ts="2024-01-01T09:05:00Z")
        prior_b = _comment(BOT, f"{marker_b}\n\n{msg}", ts="2024-01-01T10:05:00Z")
        item = _mock_item([prior_a, prior_b], author="alice")

        marker_current = encode_marker(FAILURE_MARKER_PREFIX, "2024-01-01T11:00:00Z")
        current_body = f"{marker_current}\n\n{msg}"
        result = item.check_and_post_failure(current_body, BOT, 2, OWNER)

        self.assertTrue(result)
        item.add_label.assert_called_once_with("in-error")

    def test_author_fallback_to_repo_owner(self) -> None:
        """When item.author is empty, mention falls back to fallback_owner."""
        body = self._make_failure_body()
        prior_a = _failure(ts="2024-01-01T09:00:00Z")
        prior_b = _failure(ts="2024-01-01T10:00:00Z")
        item = _mock_item([prior_a, prior_b], author="")

        item.check_and_post_failure(body, BOT, 2, OWNER)

        posted_body: str = item.add_comment.call_args[0][0]
        self.assertIn(f"@{OWNER}", posted_body)

    def test_threshold_of_three_requires_three_identical(self) -> None:
        """With threshold=3, two identical failures are not enough."""
        body = self._make_failure_body()
        prior_a = _failure(ts="2024-01-01T09:00:00Z")
        prior_b = _failure(ts="2024-01-01T10:00:00Z")
        item = _mock_item([prior_a, prior_b])

        result = item.check_and_post_failure(body, BOT, 3, OWNER)

        self.assertFalse(result)
        item.add_comment.assert_called_once_with(body)
        item.add_label.assert_not_called()

    def test_threshold_of_one_triggers_on_single_prior(self) -> None:
        """With threshold=1, a single identical prior failure triggers in-error."""
        body = self._make_failure_body()
        prior = _failure(ts="2024-01-01T09:00:00Z")
        item = _mock_item([prior], author="alice")

        result = item.check_and_post_failure(body, BOT, 1, OWNER)

        self.assertTrue(result)
        item.add_label.assert_called_once_with("in-error")

    def test_one_identical_one_different_does_not_trigger(self) -> None:
        """If only some of the last N are identical, do not trigger."""
        body = self._make_failure_body("Implementation failed: oops")
        prior_different = _failure("Implementation failed: something else", ts="2024-01-01T09:00:00Z")
        prior_same = _failure(ts="2024-01-01T10:00:00Z")
        item = _mock_item([prior_different, prior_same])

        result = item.check_and_post_failure(body, BOT, 2, OWNER)

        self.assertFalse(result)
        item.add_label.assert_not_called()


# ---------------------------------------------------------------------------
# PullRequest.get_comments() override
# ---------------------------------------------------------------------------


class TestPullRequestGetComments(unittest.TestCase):

    def test_returns_copy_of_stored_comments(self) -> None:
        repo = _mock_repo()
        c1 = _comment("alice", "Hello")
        c2 = _comment(BOT, f"{FAILURE_MARKER}\n\nFailed")
        pr = PullRequest(number=1, comments=[c1, c2], _repo=repo)
        result = pr.get_comments()
        self.assertEqual(result, [c1, c2])
        self.assertIsNot(result, pr.comments)  # returns a copy


# ---------------------------------------------------------------------------
# discover() in-error guard — one representative test per task
# ---------------------------------------------------------------------------


def _pr_data(labels: list[str] = (), assignees: list[str] = (BOT,)) -> dict:
    return {
        "number": 1,
        "headRefName": "feature/x",
        "headRefOid": "abc123",
        "title": "My PR",
        "author": {"login": "alice"},
        "comments": [],
        "reviews": [],
        "labels": [{"name": lbl} for lbl in labels],
        "mergeable": "CONFLICTING",
        "updatedAt": "2020-01-01T00:00:00Z",
        "assignees": [{"login": a} for a in assignees],
    }


def _issue_data(labels: list[str] = (), assignees: list[str] = ()) -> dict:
    return {
        "number": 1,
        "title": "My issue",
        "body": "Implement this",
        "author": {"login": "alice"},
        "labels": [{"name": lbl} for lbl in labels],
        "updatedAt": "2020-01-01T00:00:00Z",
        "assignees": [{"login": a} for a in assignees],
    }


class TestDiscoverSkipsInError(unittest.TestCase):

    def _make_pr_repo(self, labels: list[str]) -> MagicMock:
        repo = _mock_repo()
        repo._tick_cache = {}
        repo._check_runs_cache = {}
        pr = PullRequest._from_api(_pr_data(labels=labels), repo)
        repo._tick_cache["open_prs"] = [pr]
        return repo

    def _make_issue_repo(self, label_filter: str, labels: list[str]) -> MagicMock:
        repo = _mock_repo()
        issue = Issue._from_api(_issue_data(labels=labels), repo)
        repo.client.gh_json.return_value = [_issue_data(labels=labels)]
        with patch("loony_dev.github.issue.Issue._from_api", return_value=issue):
            pass
        return repo, issue

    # CIFailureTask

    def test_ci_failure_skips_in_error_pr(self) -> None:
        from loony_dev.tasks.ci_failure_task import CIFailureTask
        repo = self._make_pr_repo(["in-error"])
        tasks = list(CIFailureTask.discover(repo))
        self.assertEqual(tasks, [])

    def test_ci_failure_does_not_skip_normal_pr(self) -> None:
        from loony_dev.tasks.ci_failure_task import CIFailureTask
        repo = self._make_pr_repo([])
        # PR has no failing checks, so discover yields nothing regardless —
        # just confirm in-error guard does not interfere with normal path.
        tasks = list(CIFailureTask.discover(repo))
        self.assertEqual(tasks, [])  # No checks = no task, but no crash either

    # ConflictResolutionTask

    def test_conflict_skips_in_error_pr(self) -> None:
        from loony_dev.tasks.conflict_task import ConflictResolutionTask
        repo = self._make_pr_repo(["in-error"])
        with patch.object(repo, "detect_default_branch", return_value="main"):
            tasks = list(ConflictResolutionTask.discover(repo))
        self.assertEqual(tasks, [])

    def test_conflict_yields_conflicting_pr_without_in_error(self) -> None:
        from loony_dev.tasks.conflict_task import ConflictResolutionTask
        repo = self._make_pr_repo([])  # mergeable=CONFLICTING, no in-error
        with patch.object(repo, "detect_default_branch", return_value="main"):
            tasks = list(ConflictResolutionTask.discover(repo))
        self.assertEqual(len(tasks), 1)

    # PRReviewTask

    def test_pr_review_skips_in_error_pr(self) -> None:
        from loony_dev.tasks.pr_review_task import PRReviewTask
        repo = self._make_pr_repo(["in-error"])
        repo.client.gh_api.return_value = []
        tasks = list(PRReviewTask.discover(repo))
        self.assertEqual(tasks, [])

    # IssueTask

    def test_issue_task_skips_in_error_issue(self) -> None:
        from loony_dev.tasks.issue_task import IssueTask
        repo = _mock_repo()
        in_error_issue = Issue._from_api(_issue_data(labels=["ready-for-development", "in-error"]), repo)
        repo.client.gh_json.return_value = [
            _issue_data(labels=["ready-for-development", "in-error"])
        ]
        with patch("loony_dev.github.issue.Issue.list", return_value=[in_error_issue]):
            tasks = list(IssueTask.discover(repo))
        self.assertEqual(tasks, [])

    def test_issue_task_yields_normal_issue(self) -> None:
        from loony_dev.tasks.issue_task import IssueTask
        repo = _mock_repo()
        normal_issue = Issue._from_api(_issue_data(labels=["ready-for-development"]), repo)
        with patch("loony_dev.github.issue.Issue.list", return_value=[normal_issue]):
            with patch("loony_dev.github.issue.Issue.comments", new_callable=lambda: property(lambda self: [])):
                tasks = list(IssueTask.discover(repo))
        self.assertEqual(len(tasks), 1)

    # PlanningTask

    def test_planning_task_skips_in_error_issue(self) -> None:
        from loony_dev.tasks.planning_task import PlanningTask
        repo = _mock_repo()
        in_error_issue = Issue._from_api(_issue_data(labels=["ready-for-planning", "in-error"]), repo)
        with patch("loony_dev.github.issue.Issue.list", return_value=[in_error_issue]):
            tasks = list(PlanningTask.discover(repo))
        self.assertEqual(tasks, [])

    # StuckItemCleanupTask

    def test_stuck_skips_in_error_issue(self) -> None:
        from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask
        from datetime import datetime, timezone
        repo = _mock_repo()
        stuck_issue = Issue._from_api(
            _issue_data(labels=["in-progress", "in-error"]), repo
        )
        stuck_issue.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        # Patch at the class level so the local imports inside discover() see the mock
        with patch("loony_dev.github.issue.Issue.list", return_value=[stuck_issue]):
            with patch("loony_dev.github.pull_request.PullRequest.list_open", return_value=[]):
                tasks = list(StuckItemCleanupTask.discover(repo))
        self.assertEqual(tasks, [])

    def test_stuck_skips_in_error_pr(self) -> None:
        from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask
        from datetime import datetime, timezone
        repo = _mock_repo()
        pr = PullRequest._from_api(_pr_data(labels=["in-progress", "in-error"]), repo)
        pr.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        with patch("loony_dev.github.issue.Issue.list", return_value=[]):
            with patch("loony_dev.github.pull_request.PullRequest.list_open", return_value=[pr]):
                tasks = list(StuckItemCleanupTask.discover(repo))
        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
