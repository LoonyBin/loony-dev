"""Lifecycle-state reads used by the worktree reclaimer (issue #198).

``PullRequest.terminal_state`` / ``Issue.is_closed`` must raise on a malformed
or unexpected ``gh`` payload rather than defaulting to "open"/not-closed — a
silent default would hide a read failure and leave a completed pipeline
unreclaimed (repo convention: prefer raising over silent defaults).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from loony_dev.github.issue import Issue
from loony_dev.github.pull_request import PullRequest


def _make_repo(payload) -> MagicMock:
    repo = MagicMock()
    repo.client.gh_json = MagicMock(return_value=payload)
    return repo


class TestPullRequestTerminalState(unittest.TestCase):
    def test_merged(self) -> None:
        repo = _make_repo({"state": "MERGED", "mergedAt": "2026-06-16T00:00:00Z"})
        self.assertEqual(PullRequest.terminal_state(1, repo=repo), "merged")

    def test_closed_without_merge(self) -> None:
        repo = _make_repo({"state": "CLOSED", "mergedAt": None})
        self.assertEqual(PullRequest.terminal_state(1, repo=repo), "closed")

    def test_open(self) -> None:
        repo = _make_repo({"state": "OPEN", "mergedAt": None})
        self.assertEqual(PullRequest.terminal_state(1, repo=repo), "open")

    def test_non_dict_payload_raises(self) -> None:
        repo = _make_repo([])
        with self.assertRaises(ValueError):
            PullRequest.terminal_state(1, repo=repo)

    def test_unknown_state_raises(self) -> None:
        repo = _make_repo({"state": "LOCKED", "mergedAt": None})
        with self.assertRaises(ValueError):
            PullRequest.terminal_state(1, repo=repo)


class TestIssueIsClosed(unittest.TestCase):
    def test_closed(self) -> None:
        repo = _make_repo({"state": "CLOSED"})
        self.assertTrue(Issue.is_closed(1, repo=repo))

    def test_open(self) -> None:
        repo = _make_repo({"state": "OPEN"})
        self.assertFalse(Issue.is_closed(1, repo=repo))

    def test_missing_state_raises(self) -> None:
        repo = _make_repo({})
        with self.assertRaises(ValueError):
            Issue.is_closed(1, repo=repo)

    def test_unknown_state_raises(self) -> None:
        repo = _make_repo({"state": "DELETED"})
        with self.assertRaises(ValueError):
            Issue.is_closed(1, repo=repo)


if __name__ == "__main__":
    unittest.main()
