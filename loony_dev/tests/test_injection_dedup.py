"""Tests for injection-warning deduplication via comment lookup (issue #68).

The bot polls GitHub on every cycle. Without deduplication, a prompt injection
detected on the read path would post a new warning comment on every poll —
spamming the issue indefinitely. The fix checks whether a warning for the same
field already exists in the comments before posting.
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


def _warning_comment(field: str) -> Comment:
    return Comment(author=BOT_NAME, body=f"{_sentinel(field)}\n> [!WARNING]\n> ...", created_at="2024-01-01T00:00:00Z")


class TestInjectionWarningDedup(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. No prior warning — comment should be posted
    # ------------------------------------------------------------------
    def test_no_prior_warning_posts_comment(self) -> None:
        client = _make_client()
        client.get_issue_comments = MagicMock(return_value=[])
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 2. Prior warning present for same field — no new comment
    # ------------------------------------------------------------------
    def test_prior_warning_suppresses_comment(self) -> None:
        client = _make_client()
        client.get_issue_comments = MagicMock(return_value=[_warning_comment("body")])
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # 3. Prior warning for a different field — new comment IS posted
    # ------------------------------------------------------------------
    def test_different_field_posts_comment(self) -> None:
        client = _make_client()
        # Warning exists for "title", but we're checking "body"
        client.get_issue_comments = MagicMock(return_value=[_warning_comment("title")])
        client.post_comment = MagicMock()

        client._post_injection_warning(1, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 4. Prior warning on a different issue — new comment IS posted
    # ------------------------------------------------------------------
    def test_different_item_posts_comment(self) -> None:
        client = _make_client()

        def _comments(number: int) -> list[Comment]:
            # Issue #1 has a warning; issue #2 does not
            if number == 1:
                return [_warning_comment("body")]
            return []

        client.get_issue_comments = MagicMock(side_effect=_comments)
        client.post_comment = MagicMock()

        client._post_injection_warning(2, "body", [])

        client.post_comment.assert_called_once()

    # ------------------------------------------------------------------
    # 5. Restart survival — fresh instance still reads prior warning
    # ------------------------------------------------------------------
    def test_restart_survival_no_repost(self) -> None:
        """A freshly-constructed client with no in-memory state must not
        re-post if the warning comment is already present in GitHub."""
        # Simulate a fresh instance — no shared state with any prior instance
        fresh_client = GitHubClient(repo="owner/repo", bot_name=BOT_NAME)
        fresh_client.get_issue_comments = MagicMock(return_value=[_warning_comment("body")])
        fresh_client.post_comment = MagicMock()

        fresh_client._post_injection_warning(1, "body", [])

        fresh_client.post_comment.assert_not_called()

    # ------------------------------------------------------------------
    # 6. Warning comment body contains the sentinel so future checks work
    # ------------------------------------------------------------------
    def test_posted_comment_body_contains_sentinel(self) -> None:
        client = _make_client()
        client.get_issue_comments = MagicMock(return_value=[])
        posted_bodies: list[str] = []
        client.post_comment = MagicMock(side_effect=lambda n, b: posted_bodies.append(b))

        client._post_injection_warning(1, "body", [])

        self.assertEqual(len(posted_bodies), 1)
        self.assertIn(_sentinel("body"), posted_bodies[0])


if __name__ == "__main__":
    unittest.main()
