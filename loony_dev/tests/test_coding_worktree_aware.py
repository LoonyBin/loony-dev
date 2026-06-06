"""Regression tests: CodingAgent.execute_issue is worktree-aware (issue #127).

The orchestrator now prepares the branch via `git worktree add -B` before the
agent runs, so execute_issue must not perform any in-worktree branch checkout.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.agents.coding import CodingAgent


class TestExecuteIssueWorktreeAware(unittest.TestCase):

    def _run_execute_issue(self, fake_git: MagicMock):
        agent = CodingAgent()

        task = MagicMock()
        task.issue.number = 7
        task.issue.title = "My feature"
        task.branch_name = "feature/7"
        task.implement_prompt.return_value = "implement it"

        with patch("loony_dev.git.GitRepo") as GitRepoCls, \
                patch("loony_dev.coderabbit.is_available", return_value=False), \
                patch.object(agent, "_run_claude_cli", return_value=("ok", "", 0)), \
                patch.object(agent, "_generate_commit_message", return_value="feat: x"), \
                patch.object(agent, "_save_commit_message"), \
                patch.object(agent, "_create_pr"), \
                patch.object(agent, "_generate_summary", return_value="done"):
            GitRepoCls.detect_default_branch.return_value = "main"
            GitRepoCls.return_value = fake_git
            return agent.execute_issue(task, Path("/fake/worktree"))

    def test_does_not_prepare_branch(self) -> None:
        """The branch is already checked out by the orchestrator's worktree."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        result = self._run_execute_issue(fake_git)

        self.assertTrue(result.success)
        fake_git.checkout_or_create_branch.assert_not_called()

    def test_commits_to_the_prepared_branch(self) -> None:
        """Commit/push still targets the worktree's branch."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        self._run_execute_issue(fake_git)

        fake_git.commit_and_push.assert_called_once()
        self.assertEqual(fake_git.commit_and_push.call_args.args[1], "feature/7")


if __name__ == "__main__":
    unittest.main()
