"""Regression tests: CodingAgent.execute_issue is worktree-aware (issue #127)
and drives a single persistent ClaudeSession (issue #162).

The orchestrator prepares the branch via `git worktree add -B` before the agent
runs, so execute_issue must not perform any in-worktree branch checkout. It also
opens exactly one ClaudeSession per task and runs every phase as a turn in it
rather than respawning `claude` per phase.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.agents.claude_session import TurnResult
from loony_dev.agents.coding import CodingAgent
from loony_dev.commands import install_commands

# A bot turn is `/<command> <abs path>.json` (issue #166).
_TURN_RE = re.compile(r"^/[a-z-]+ /.+\.json$")


def _turn(text: str = "ok") -> TurnResult:
    return TurnResult(text=text, stop_reason="end_turn", was_interrupted=False, entries_added=1)


class TestExecuteIssueWorktreeAware(unittest.TestCase):

    def setUp(self) -> None:
        # A real worktree with the bundled commands installed so _command_turn
        # finds the command files (#166).
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name)
        install_commands(self.worktree)
        self.addCleanup(self._tmp.cleanup)

    def _make_task(self) -> MagicMock:
        task = MagicMock()
        task.issue.number = 7
        task.issue.title = "My feature"
        task.branch_name = "feature/7"
        task.worktree_key = "issue-7"
        task.implement_payload.return_value = {"issue_number": 7, "title": "My feature"}
        return task

    def _run_execute_issue(self, fake_git: MagicMock, fake_session: MagicMock | None = None):
        agent = CodingAgent()

        task = self._make_task()

        session = fake_session or MagicMock()
        session.send_turn.return_value = _turn()

        with patch("loony_dev.git.GitRepo") as GitRepoCls, \
                patch("loony_dev.coderabbit.is_available", return_value=False), \
                patch.object(agent, "_open_session", return_value=session) as open_mock, \
                patch.object(agent, "_close_session") as close_mock, \
                patch.object(agent, "_generate_commit_message", return_value="feat: x"), \
                patch.object(agent, "_save_commit_message"), \
                patch.object(agent, "_create_pr"), \
                patch.object(agent, "_generate_summary", return_value="done"):
            GitRepoCls.detect_default_branch.return_value = "main"
            GitRepoCls.return_value = fake_git
            result = agent.execute_issue(task, self.worktree)
            return result, open_mock, close_mock, session

    def test_does_not_prepare_branch(self) -> None:
        """The branch is already checked out by the orchestrator's worktree."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        result, _open, _close, _session = self._run_execute_issue(fake_git)

        self.assertTrue(result.success)
        fake_git.checkout_or_create_branch.assert_not_called()

    def test_commits_to_the_prepared_branch(self) -> None:
        """Commit/push still targets the worktree's branch."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        self._run_execute_issue(fake_git)

        fake_git.commit_and_push.assert_called_once()
        self.assertEqual(fake_git.commit_and_push.call_args.args[1], "feature/7")

    def test_opens_one_session_and_closes_it(self) -> None:
        """A single ClaudeSession is opened and closed exactly once."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        _result, open_mock, close_mock, _session = self._run_execute_issue(fake_git)

        open_mock.assert_called_once()
        close_mock.assert_called_once()

    def test_phases_run_as_turns_in_the_session(self) -> None:
        """The implement phase runs as a turn (no per-phase respawn)."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        _result, _open, _close, session = self._run_execute_issue(fake_git)

        session.send_turn.assert_called_once()
        # The implement phase runs as a /implement-issue slash-command turn.
        self.assertRegex(session.send_turn.call_args.args[0], _TURN_RE)
        self.assertTrue(session.send_turn.call_args.args[0].startswith("/implement-issue "))

    def test_empty_branch_uses_fresh_session(self) -> None:
        """An empty branch opens a fresh (session_id=None) session."""
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        agent = CodingAgent(repo="LoonyBin/repo")
        task = self._make_task()
        task.session_key = "issue:7"
        session = MagicMock()
        session.send_turn.return_value = _turn()

        with patch("loony_dev.git.GitRepo") as GitRepoCls, \
                patch("loony_dev.coderabbit.is_available", return_value=False), \
                patch.object(agent, "_open_session", return_value=session) as open_mock, \
                patch.object(agent, "_close_session"), \
                patch.object(agent, "_generate_commit_message", return_value="feat: x"), \
                patch.object(agent, "_save_commit_message"), \
                patch.object(agent, "_create_pr"), \
                patch.object(agent, "_generate_summary", return_value="done"):
            GitRepoCls.detect_default_branch.return_value = "main"
            GitRepoCls.return_value = fake_git
            agent.execute_issue(task, self.worktree)

        self.assertIsNone(open_mock.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
