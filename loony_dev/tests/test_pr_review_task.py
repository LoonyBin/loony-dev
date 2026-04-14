"""Tests for PRReviewTask._new_since_bot() and discover() — inline comment timing.

Issue #78: Inline review comments were silently dropped from the polling loop
because their ``created_at`` timestamp reflects when the reviewer *drafted* the
comment, not when the review was *submitted*. If drafting happened before the
bot's last SUCCESS_MARKER, the comments sorted before the marker and were
excluded from "new" comments even though the review was submitted afterwards.

The fix is to use the review's ``submitted_at`` as the effective timestamp for
all inline comments that belong to it.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.github.pull_request import PullRequest
from loony_dev.tasks.base import SUCCESS_MARKER, SUCCESS_MARKER_PREFIX, encode_marker
from loony_dev.tasks.pr_review_task import PRReviewTask

BOT_NAME = "loony-bot"
REVIEWER = "alice"


def _mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT_NAME
    return repo


def _comment(author: str, body: str, ts: str, path: str | None = None, line: int | None = None) -> Comment:
    return Comment(author=author, body=body, created_at=ts, path=path, line=line)


def _success(ts: str, last_seen: str | None = None) -> Comment:
    if last_seen is not None:
        marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen)
    else:
        marker = SUCCESS_MARKER
    return _comment(BOT_NAME, f"{marker}\nReview addressed.", ts)


def _inline(ts: str, path: str = "foo.py", line: int = 10) -> Comment:
    return _comment(REVIEWER, "Please fix this.", ts, path=path, line=line)


def _general(ts: str) -> Comment:
    return _comment(REVIEWER, "LGTM overall.", ts)


class TestNewSinceBot(unittest.TestCase):

    def test_inline_only_no_prior_marker(self) -> None:
        comments = [
            _inline("2024-01-01T10:00:00Z"),
            _inline("2024-01-01T11:00:00Z"),
        ]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, comments)

    def test_inline_drafted_before_marker_submitted_after(self) -> None:
        marker = _success("2024-01-01T12:00:00Z")
        inline_with_submitted_ts = _inline("2024-01-01T13:00:00Z")

        comments = sorted([marker, inline_with_submitted_ts], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [inline_with_submitted_ts])

    def test_inline_both_before_marker(self) -> None:
        inline_before = _inline("2024-01-01T10:00:00Z")
        marker = _success("2024-01-01T12:00:00Z")

        comments = sorted([inline_before, marker], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [])

    def test_general_and_inline_mixed(self) -> None:
        old_general = _general("2024-01-01T09:00:00Z")
        marker = _success("2024-01-01T12:00:00Z")
        new_inline = _inline("2024-01-01T13:00:00Z")
        new_general = _general("2024-01-01T14:00:00Z")

        comments = sorted(
            [old_general, marker, new_inline, new_general],
            key=lambda c: c.created_at,
        )
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [new_inline, new_general])

    def test_uses_last_success_marker(self) -> None:
        old_marker = _success("2024-01-01T10:00:00Z")
        middle_comment = _general("2024-01-01T11:00:00Z")
        new_marker = _success("2024-01-01T12:00:00Z")
        after_new = _inline("2024-01-01T13:00:00Z")

        comments = [old_marker, middle_comment, new_marker, after_new]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [after_new])

    def test_timestamp_filter_picks_up_midrun_comment(self) -> None:
        t1_comment = _general("2024-01-01T10:00:00Z")
        t2_midrun = _general("2024-01-01T11:00:00Z")
        t3_marker = _success("2024-01-01T12:00:00Z", last_seen="2024-01-01T10:00:00Z")

        comments = sorted([t1_comment, t2_midrun, t3_marker], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t2_midrun])

    def test_timestamp_filter_excludes_already_seen_comments(self) -> None:
        t1 = _general("2024-01-01T09:00:00Z")
        t2 = _general("2024-01-01T10:00:00Z")
        marker = _success("2024-01-01T11:00:00Z", last_seen="2024-01-01T10:00:00Z")
        t3 = _general("2024-01-01T12:00:00Z")

        comments = sorted([t1, t2, marker, t3], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t3])

    def test_old_marker_backward_compat_still_uses_position(self) -> None:
        t1 = _general("2024-01-01T09:00:00Z")
        marker = _success("2024-01-01T10:00:00Z")
        t2 = _general("2024-01-01T11:00:00Z")

        comments = [t1, marker, t2]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t2])


class TestDiscoverInlineOnly(unittest.TestCase):
    """discover() should yield a task when the only new comments are inline."""

    def _make_repo_with_prs(self, pr_data: list[dict], inline_comments: list[Comment]) -> MagicMock:
        repo = _mock_repo()
        repo._tick_cache = {}
        repo.skip_ci_checks = set()
        repo._check_runs_cache = {}

        prs = [PullRequest._from_api(d, repo) for d in pr_data]
        # Patch list_open to return our PRs
        repo._tick_cache["open_prs"] = prs

        # Patch inline_comments via the client
        repo.client.gh_api.return_value = [
            {
                "user": {"login": c.author},
                "body": str(c.body),
                "created_at": c.created_at,
                "path": c.path,
                "line": c.line,
                "pull_request_review_id": None,
            }
            for c in inline_comments
        ]
        repo.is_authorized = MagicMock(return_value=True)
        return repo

    def _pr_data(self, comments: list[dict] | None = None) -> dict:
        return {
            "number": 1,
            "headRefName": "feature/branch",
            "headRefOid": "abc123",
            "title": "My PR",
            "comments": comments or [],
            "reviews": [],
            "labels": [],
            "mergeable": "MERGEABLE",
            "updatedAt": "2024-01-01T14:00:00Z",
            "assignees": [{"login": BOT_NAME}],
        }

    def test_discover_triggers_on_inline_only(self) -> None:
        inline = _inline("2024-01-01T13:00:00Z")
        repo = self._make_repo_with_prs([self._pr_data()], [inline])

        tasks = list(PRReviewTask.discover(repo))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_discover_skips_when_inline_before_marker(self) -> None:
        inline_before = _inline("2024-01-01T10:00:00Z")
        pr_data = self._pr_data(comments=[{
            "author": {"login": BOT_NAME},
            "body": f"{SUCCESS_MARKER}\nReview addressed.",
            "createdAt": "2024-01-01T12:00:00Z",
        }])
        repo = self._make_repo_with_prs([pr_data], [inline_before])

        tasks = list(PRReviewTask.discover(repo))

        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
