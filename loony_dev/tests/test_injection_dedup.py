"""Tests for injection-warning deduplication via comment lookup (issue #68).

The bot polls GitHub on every cycle. Without deduplication, a prompt injection
detected on the read path would post a new warning comment on every poll —
spamming the issue indefinitely. The fix checks whether a warning for the same
field already exists in the comments before posting.

``_injection_warning_exists`` uses a raw ``_gh_json`` call instead of
``get_issue_comments`` to avoid infinite recursion (get_issue_comments
sanitizes bodies → _sanitize_field → _post_injection_warning → loop).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call

from loony_dev.github import GitHubClient, INJECTION_WARNING_SENTINEL
from loony_dev.models import Comment

BOT_NAME = "loony-bot"


def _make_client() -> GitHubClient:
    return GitHubClient(repo="owner/repo", bot_name=BOT_NAME)


def _sentinel(field: str) -> str:
    return f'{INJECTION_WARNING_SENTINEL}"{field}" -->'


def _raw_comments_response(*comments: dict) -> dict:
    """Build the dict that ``_gh_json("issue", "view", ...)`` returns."""
    return {"comments": list(comments)}


def _raw_warning_comment(field: str) -> dict:
    """A raw comment dict (as returned by gh) containing the sentinel."""
    return {"author": {"login": BOT_NAME}, "body": f"{_sentinel(field)}\n> [!WARNING]\n> ..."}


class TestInjectionWarningDedup(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. No prior warning — comment should be posted
    # ------------------------------------------------------------------
    def test_no_prior_warning_posts_comment(self) -> None:
        client = _make_client()
        client._gh_json = MagicMock(return_value=_raw_comments_response())
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 2. Prior warning present for same field — no new comment
    # ------------------------------------------------------------------
    def test_prior_warning_suppresses_comment(self) -> None:
        client = _make_client()
        client._gh_json = MagicMock(return_value=_raw_comments_response(_raw_warning_comment("body")))
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # 3. Prior warning for a different field — new comment IS posted
    # ------------------------------------------------------------------
    def test_different_field_posts_comment(self) -> None:
        client = _make_client()
        # Warning exists for "title", but we're checking "body"
        client._gh_json = MagicMock(return_value=_raw_comments_response(_raw_warning_comment("title")))
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 4. Prior warning on a different issue — new comment IS posted
    # ------------------------------------------------------------------
    def test_different_item_posts_comment(self) -> None:
        client = _make_client()

        def _gh_json_side_effect(*args: str) -> dict:
            # Issue #1 has a warning; issue #2 does not
            if "1" in args:
                return _raw_comments_response(_raw_warning_comment("body"))
            return _raw_comments_response()

        client._gh_json = MagicMock(side_effect=_gh_json_side_effect)
        client.post_comment = MagicMock()

        client._post_injection_warning(2, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 5. Restart survival — fresh instance still reads prior warning
    # ------------------------------------------------------------------
    def test_restart_survival_no_repost(self) -> None:
        """A freshly-constructed client with no in-memory state must not
        re-post if the warning comment is already present in GitHub."""
        fresh_client = GitHubClient(repo="owner/repo", bot_name=BOT_NAME)
        fresh_client._gh_json = MagicMock(return_value=_raw_comments_response(_raw_warning_comment("body")))
        fresh_client.post_comment = MagicMock()

        fresh_client._post_injection_warning(1, "body", [])

        fresh_client.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # 6. Warning comment body contains the sentinel so future checks work
    # ------------------------------------------------------------------
    def test_posted_comment_body_contains_sentinel(self) -> None:
        client = _make_client()
        client._gh_json = MagicMock(return_value=_raw_comments_response())
        posted_bodies: list[str] = []
        client.post_comment = MagicMock(side_effect=lambda n, b: posted_bodies.append(b))

        client._post_injection_warning(1, "body", [])

        self.assertEqual(len(posted_bodies), 1)
        self.assertIn(_sentinel("body"), posted_bodies[0])

    # ------------------------------------------------------------------
    # 7. Regression: _injection_warning_exists must NOT call
    #    get_issue_comments (which sanitizes → infinite recursion)
    # ------------------------------------------------------------------
    def test_injection_warning_exists_does_not_use_get_issue_comments(self) -> None:
        """Ensure the recursion bug cannot regress."""
        client = _make_client()
        client._gh_json = MagicMock(return_value=_raw_comments_response())
        client.get_issue_comments = MagicMock(
            side_effect=AssertionError("must not call get_issue_comments from _injection_warning_exists"),
        )

        # Should complete without hitting get_issue_comments
        result = client._injection_warning_exists(1, "body")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
