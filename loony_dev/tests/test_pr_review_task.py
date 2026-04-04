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

from loony_dev.models import Comment, PullRequest
from loony_dev.tasks.base import SUCCESS_MARKER
from loony_dev.tasks.pr_review_task import PRReviewTask

BOT_NAME = "loony-bot"
REVIEWER = "alice"


def _comment(author: str, body: str, ts: str, path: str | None = None, line: int | None = None) -> Comment:
    return Comment(author=author, body=body, created_at=ts, path=path, line=line)


def _success(ts: str) -> Comment:
    return _comment(BOT_NAME, f"{SUCCESS_MARKER}\nReview addressed.", ts)


def _inline(ts: str, path: str = "foo.py", line: int = 10) -> Comment:
    return _comment(REVIEWER, "Please fix this.", ts, path=path, line=line)


def _general(ts: str) -> Comment:
    return _comment(REVIEWER, "LGTM overall.", ts)


class TestNewSinceBot(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. No prior SUCCESS_MARKER — all non-bot comments returned
    # ------------------------------------------------------------------
    def test_inline_only_no_prior_marker(self) -> None:
        comments = [
            _inline("2024-01-01T10:00:00Z"),
            _inline("2024-01-01T11:00:00Z"),
        ]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, comments)

    # ------------------------------------------------------------------
    # 2. Inline drafted BEFORE marker but submitted (via review lookup) AFTER
    #    This is the core bug scenario — with fixed timestamps, these should
    #    appear after the marker in the sorted list.
    # ------------------------------------------------------------------
    def test_inline_drafted_before_marker_submitted_after(self) -> None:
        # Timeline:
        #   T1: inline comment drafted (created_at before SUCCESS_MARKER)
        #   T2: bot posts SUCCESS_MARKER
        #   T3: review submitted — so get_pr_inline_comments() returns T3 as created_at
        marker = _success("2024-01-01T12:00:00Z")
        # After the fix, get_pr_inline_comments() remaps created_at to submitted_at (T3)
        inline_with_submitted_ts = _inline("2024-01-01T13:00:00Z")

        comments = sorted([marker, inline_with_submitted_ts], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [inline_with_submitted_ts])

    # ------------------------------------------------------------------
    # 3. Inline comment submitted BEFORE the marker — should NOT be returned
    # ------------------------------------------------------------------
    def test_inline_both_before_marker(self) -> None:
        inline_before = _inline("2024-01-01T10:00:00Z")
        marker = _success("2024-01-01T12:00:00Z")

        comments = sorted([inline_before, marker], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # 4. Mix of general and inline; only post-marker ones returned
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 5. Multiple SUCCESS_MARKERs — only the last one counts
    # ------------------------------------------------------------------
    def test_uses_last_success_marker(self) -> None:
        old_marker = _success("2024-01-01T10:00:00Z")
        middle_comment = _general("2024-01-01T11:00:00Z")
        new_marker = _success("2024-01-01T12:00:00Z")
        after_new = _inline("2024-01-01T13:00:00Z")

        comments = [old_marker, middle_comment, new_marker, after_new]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        # middle_comment is between markers — not returned
        self.assertEqual(result, [after_new])


class TestDiscoverInlineOnly(unittest.TestCase):
    """discover() should yield a task when the only new comments are inline."""

    def _make_github(self, inline_comments: list[Comment], pr_number: int = 1) -> MagicMock:
        github = MagicMock()
        github.bot_name = BOT_NAME

        # PR data: no general comments, no review bodies, no in-progress label
        pr_item = {
            "number": pr_number,
            "headRefName": "feature/branch",
            "headRefOid": "abc123",
            "title": "My PR",
            "comments": [],
            "reviews": [],
            "labels": [],
            "mergeable": "MERGEABLE",
            "updatedAt": "2024-01-01T14:00:00Z",
        }
        github.list_open_prs.return_value = [pr_item]
        github.get_pr_inline_comments.return_value = inline_comments
        return github

    def tearDown(self) -> None:
        pass

    def test_discover_triggers_on_inline_only(self) -> None:
        """discover() yields a task when there are only inline comments and no
        general comments after the bot's last SUCCESS_MARKER."""
        inline = _inline("2024-01-01T13:00:00Z")
        github = self._make_github([inline])

        with patch("loony_dev.tasks.pr_review_task.is_authorized", side_effect=lambda _gh, u: u == REVIEWER):
            tasks = list(PRReviewTask.discover(github))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)
        self.assertEqual(tasks[0].pr.new_comments, [inline])

    def test_discover_skips_when_inline_before_marker(self) -> None:
        """discover() skips a PR when all inline comments pre-date the marker."""
        # The PR has a SUCCESS_MARKER general comment and an inline comment from before it.
        inline_before = _inline("2024-01-01T10:00:00Z")
        github = self._make_github([inline_before])

        # Add a SUCCESS_MARKER comment to the PR's general comments
        github.list_open_prs.return_value = [{
            "number": 1,
            "headRefName": "feature/branch",
            "headRefOid": "abc123",
            "title": "My PR",
            "comments": [{
                "author": {"login": BOT_NAME},
                "body": f"{SUCCESS_MARKER}\nReview addressed.",
                "createdAt": "2024-01-01T12:00:00Z",
            }],
            "reviews": [],
            "labels": [],
            "mergeable": "MERGEABLE",
            "updatedAt": "2024-01-01T14:00:00Z",
        }]

        with patch("loony_dev.tasks.pr_review_task.is_authorized", side_effect=lambda _gh, u: u == REVIEWER):
            tasks = list(PRReviewTask.discover(github))

        self.assertEqual(tasks, [])


if __name__ == "__main__":
    unittest.main()
