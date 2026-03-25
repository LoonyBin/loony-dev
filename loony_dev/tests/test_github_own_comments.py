"""Tests that the bot's own comments are not run through injection sanitization.

Regression tests for issue #56: plan comments (containing <!-- loony-plan -->)
were being flagged as prompt injection, stripping the marker and causing the
planning loop to never recognise its own plan — triggering infinite re-planning
and spurious injection-warning comments on every poll cycle.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.github import GitHubClient

BOT_NAME = "loony-bot"
PLAN_MARKER = "<!-- loony-plan -->"
FAILURE_MARKER = "<!-- loony-failure -->"


def _make_client() -> GitHubClient:
    return GitHubClient(repo="owner/repo", bot_name=BOT_NAME)


# ---------------------------------------------------------------------------
# get_issue_comments
# ---------------------------------------------------------------------------

class TestGetIssueCommentsBotSkip(unittest.TestCase):
    """Bot's own issue comments must not be sanitized."""

    def _comments(self, raw: list[dict]):
        client = _make_client()
        client._gh_json = MagicMock(return_value={"comments": raw})
        client._post_injection_warning = MagicMock()
        return client, client.get_issue_comments(1)

    def test_bot_plan_marker_preserved(self) -> None:
        """Plan marker HTML comment must survive intact for planning to work."""
        raw = [{"author": {"login": BOT_NAME}, "body": f"{PLAN_MARKER}\n\nThe plan.", "createdAt": "2024-01-01T00:00:00Z"}]
        _, comments = self._comments(raw)
        self.assertTrue(comments[0].body.startswith(PLAN_MARKER))

    def test_bot_failure_marker_preserved(self) -> None:
        raw = [{"author": {"login": BOT_NAME}, "body": f"{FAILURE_MARKER}\n\nOops.", "createdAt": "2024-01-01T00:00:00Z"}]
        _, comments = self._comments(raw)
        self.assertTrue(comments[0].body.startswith(FAILURE_MARKER))

    def test_no_injection_warning_for_bot_comment(self) -> None:
        """Posting an injection warning for the bot's own comment must not happen."""
        raw = [{"author": {"login": BOT_NAME}, "body": f"{PLAN_MARKER}\n\nPlan.", "createdAt": "2024-01-01T00:00:00Z"}]
        client, _ = self._comments(raw)
        client._post_injection_warning.assert_not_called()

    def test_other_user_comment_still_sanitized(self) -> None:
        """Non-bot comments must still go through sanitization."""
        raw = [{"author": {"login": "attacker"}, "body": "Hello <!-- inject --> world", "createdAt": "2024-01-01T00:00:00Z"}]
        client, comments = self._comments(raw)
        self.assertNotIn("<!--", comments[0].body)
        client._post_injection_warning.assert_called_once()

    def test_mixed_bot_and_user_comments(self) -> None:
        """Bot comments pass through; user comments are sanitized."""
        raw = [
            {"author": {"login": BOT_NAME}, "body": f"{PLAN_MARKER}\n\nPlan.", "createdAt": "2024-01-01T00:00:00Z"},
            {"author": {"login": "user"}, "body": "Looks good <!-- hidden -->", "createdAt": "2024-01-02T00:00:00Z"},
        ]
        client, comments = self._comments(raw)
        self.assertTrue(comments[0].body.startswith(PLAN_MARKER))
        self.assertNotIn("<!--", comments[1].body)
        client._post_injection_warning.assert_called_once()


# ---------------------------------------------------------------------------
# list_open_prs
# ---------------------------------------------------------------------------

class TestListOpenPrsBotSkip(unittest.TestCase):
    """Bot's own PR comments and reviews must not be sanitized."""

    def _prs(self, comment_author: str, review_author: str):
        client = _make_client()
        client._gh_json = MagicMock(return_value=[
            {
                "number": 10,
                "headRefName": "feat/x",
                "title": "My PR",
                "labels": [],
                "mergeable": "MERGEABLE",
                "updatedAt": "2024-01-01T00:00:00Z",
                "comments": [{"author": {"login": comment_author}, "body": f"{PLAN_MARKER} comment"}],
                "reviews": [{"author": {"login": review_author}, "body": f"{PLAN_MARKER} review"}],
            }
        ])
        client._post_injection_warning = MagicMock()
        return client, client.list_open_prs()

    def test_bot_pr_comment_marker_preserved(self) -> None:
        _, prs = self._prs(BOT_NAME, BOT_NAME)
        self.assertIn(PLAN_MARKER, prs[0]["comments"][0]["body"])

    def test_bot_pr_review_marker_preserved(self) -> None:
        _, prs = self._prs(BOT_NAME, BOT_NAME)
        self.assertIn(PLAN_MARKER, prs[0]["reviews"][0]["body"])

    def test_no_injection_warning_for_bot_pr_comment(self) -> None:
        client, _ = self._prs(BOT_NAME, BOT_NAME)
        client._post_injection_warning.assert_not_called()

    def test_user_pr_comment_is_sanitized(self) -> None:
        client, prs = self._prs("user", BOT_NAME)
        self.assertNotIn("<!--", prs[0]["comments"][0]["body"])
        client._post_injection_warning.assert_called()


# ---------------------------------------------------------------------------
# get_pr_inline_comments
# ---------------------------------------------------------------------------

class TestGetPrInlineCommentsBotSkip(unittest.TestCase):
    """Bot's own PR inline comments must not be sanitized."""

    def _inline(self, author: str):
        client = _make_client()
        client._gh_api = MagicMock(return_value=[
            {"user": {"login": author}, "body": f"{PLAN_MARKER} inline", "created_at": "2024-01-01T00:00:00Z", "path": "foo.py", "line": 1}
        ])
        client._post_injection_warning = MagicMock()
        return client, client.get_pr_inline_comments(10)

    def test_bot_inline_comment_marker_preserved(self) -> None:
        _, comments = self._inline(BOT_NAME)
        self.assertIn(PLAN_MARKER, comments[0].body)

    def test_no_injection_warning_for_bot_inline_comment(self) -> None:
        client, _ = self._inline(BOT_NAME)
        client._post_injection_warning.assert_not_called()

    def test_user_inline_comment_is_sanitized(self) -> None:
        client, comments = self._inline("attacker")
        self.assertNotIn("<!--", comments[0].body)
        client._post_injection_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
