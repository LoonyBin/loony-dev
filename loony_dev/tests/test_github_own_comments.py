"""Tests that the bot's own comments get Content(safe=True).

Regression tests for issue #56: plan comments (containing <!-- loony-plan -->)
were being flagged as prompt injection. With the Content class, the bot's own
comments are marked as safe at the model layer.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.github.pull_request import PullRequest

BOT_NAME = "loony-bot"
PLAN_MARKER = "<!-- loony-plan -->"
FAILURE_MARKER = "<!-- loony-failure -->"


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT_NAME
    return repo


# ---------------------------------------------------------------------------
# Comment._from_api
# ---------------------------------------------------------------------------


class TestCommentFromApi(unittest.TestCase):
    """Comment._from_api should mark bot comments as safe."""

    def test_bot_comment_is_safe(self) -> None:
        repo = _make_repo()
        data = {"author": {"login": BOT_NAME}, "body": f"{PLAN_MARKER}\n\nThe plan.", "createdAt": "2024-01-01T00:00:00Z"}
        comment = Comment._from_api(data, repo)
        self.assertTrue(comment.body.is_safe)

    def test_bot_plan_marker_preserved(self) -> None:
        repo = _make_repo()
        data = {"author": {"login": BOT_NAME}, "body": f"{PLAN_MARKER}\n\nThe plan.", "createdAt": "2024-01-01T00:00:00Z"}
        comment = Comment._from_api(data, repo)
        self.assertTrue(str(comment.body).startswith(PLAN_MARKER))

    def test_bot_failure_marker_preserved(self) -> None:
        repo = _make_repo()
        data = {"author": {"login": BOT_NAME}, "body": f"{FAILURE_MARKER}\n\nOops.", "createdAt": "2024-01-01T00:00:00Z"}
        comment = Comment._from_api(data, repo)
        self.assertTrue(str(comment.body).startswith(FAILURE_MARKER))

    def test_user_comment_is_not_safe(self) -> None:
        repo = _make_repo()
        data = {"author": {"login": "attacker"}, "body": "Hello <!-- inject --> world", "createdAt": "2024-01-01T00:00:00Z"}
        comment = Comment._from_api(data, repo)
        self.assertFalse(comment.body.is_safe)


# ---------------------------------------------------------------------------
# PullRequest._from_api
# ---------------------------------------------------------------------------


class TestPullRequestFromApi(unittest.TestCase):
    """PullRequest._from_api should mark bot comments and reviews as safe."""

    def _pr_data(self, comment_author: str, review_author: str) -> dict:
        return {
            "number": 10,
            "headRefName": "feat/x",
            "headRefOid": "abc",
            "title": "My PR",
            "labels": [],
            "mergeable": "MERGEABLE",
            "updatedAt": "2024-01-01T00:00:00Z",
            "comments": [{"author": {"login": comment_author}, "body": f"{PLAN_MARKER} comment", "createdAt": "2024-01-01T00:00:00Z"}],
            "reviews": [{"author": {"login": review_author}, "body": f"{PLAN_MARKER} review", "submittedAt": "2024-01-01T00:00:00Z"}],
            "assignees": [],
        }

    def test_bot_pr_comment_is_safe(self) -> None:
        repo = _make_repo()
        pr = PullRequest._from_api(self._pr_data(BOT_NAME, BOT_NAME), repo)
        self.assertTrue(pr.comments[0].body.is_safe)

    def test_bot_pr_review_is_safe(self) -> None:
        repo = _make_repo()
        pr = PullRequest._from_api(self._pr_data(BOT_NAME, BOT_NAME), repo)
        self.assertTrue(pr.reviews[0].body.is_safe)

    def test_bot_pr_comment_marker_preserved(self) -> None:
        repo = _make_repo()
        pr = PullRequest._from_api(self._pr_data(BOT_NAME, BOT_NAME), repo)
        self.assertIn(PLAN_MARKER, str(pr.comments[0].body))

    def test_user_pr_comment_not_safe(self) -> None:
        repo = _make_repo()
        pr = PullRequest._from_api(self._pr_data("user", BOT_NAME), repo)
        self.assertFalse(pr.comments[0].body.is_safe)


# ---------------------------------------------------------------------------
# Content safety basics
# ---------------------------------------------------------------------------


class TestContentSafety(unittest.TestCase):
    """Content class safety tracking."""

    def test_default_not_safe(self) -> None:
        c = Content("hello")
        self.assertFalse(c.is_safe)

    def test_explicit_safe(self) -> None:
        c = Content("hello", safe=True)
        self.assertTrue(c.is_safe)

    def test_sanitize_returns_safe(self) -> None:
        c = Content("hello")
        self.assertTrue(c.sanitize().is_safe)

    def test_already_safe_skips_sanitize(self) -> None:
        c = Content("hello", safe=True)
        self.assertIs(c.sanitize(), c)


if __name__ == "__main__":
    unittest.main()
