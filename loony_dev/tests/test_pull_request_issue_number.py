"""Tests for PullRequest.issue_number resolution (issue #181)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.github import PullRequest


def _pr(*, branch: str = "", title: str = "", body: str = "") -> PullRequest:
    repo = MagicMock()
    repo.bot_name = "trixy"
    return PullRequest(number=99, branch=branch, title=title, body=body, _repo=repo)


class TestIssueNumber(unittest.TestCase):
    def test_branch_is_primary_signal(self) -> None:
        self.assertEqual(_pr(branch="issue-181/unify-keys").issue_number, 181)

    def test_body_fallback_closes(self) -> None:
        # Non-conventional branch, but the body references the issue.
        self.assertEqual(
            _pr(branch="my-fix", body="Some text\n\nCloses #42\n").issue_number, 42
        )

    def test_body_fallback_is_case_insensitive_and_matches_synonyms(self) -> None:
        self.assertEqual(_pr(branch="x", body="fixes #7").issue_number, 7)
        self.assertEqual(_pr(branch="x", body="RESOLVES #8").issue_number, 8)

    def test_title_fallback(self) -> None:
        self.assertEqual(_pr(branch="x", title="Add a thing (#7)").issue_number, 7)

    def test_branch_beats_conflicting_body_and_title(self) -> None:
        pr = _pr(branch="issue-181/slug", body="Closes #42", title="Thing (#7)")
        self.assertEqual(pr.issue_number, 181)

    def test_body_beats_title(self) -> None:
        pr = _pr(branch="external", body="Fixes #42", title="Thing (#7)")
        self.assertEqual(pr.issue_number, 42)

    def test_external_pr_returns_none(self) -> None:
        pr = _pr(branch="contributor:feature", title="Improve docs", body="No refs here")
        self.assertIsNone(pr.issue_number)

    def test_title_ref_must_be_trailing(self) -> None:
        # A bare "#7" mid-title is not the bot's "(#N)" suffix format.
        self.assertIsNone(_pr(branch="external", title="See #7 for context").issue_number)


if __name__ == "__main__":
    unittest.main()
