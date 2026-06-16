"""Tests for CodingAgent driving a persistent ClaudeSession (issue #162).

Covers the simple ``execute`` path, quota translation from
``QuotaExceededError`` into a rate-limited TaskResult, and the session
registry that lets ``terminate`` tear sessions down on shutdown.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.agents.claude_session import (
    ClaudeSessionError,
    QuotaExceededError,
    TurnResult,
)
from loony_dev.agents.coding import CodingAgent, _CliSession
from loony_dev.commands import install_commands

# A bot turn is a short slash-command invocation: `/<command> <abs path>.json`.
_TURN_RE = re.compile(r"^/(?P<command>[a-z-]+) (?P<path>/.+\.json)$")


def _turn(text: str = "did the work") -> TurnResult:
    return TurnResult(text=text, stop_reason="end_turn", was_interrupted=False, entries_added=2)


def _assert_turn(testcase: unittest.TestCase, turn: str, command: str) -> dict:
    """Assert *turn* is `/<command> <path>` and return the parsed JSON payload."""
    m = _TURN_RE.match(turn)
    testcase.assertIsNotNone(m, f"turn is not a slash-command invocation: {turn!r}")
    testcase.assertEqual(m.group("command"), command)
    return json.loads(Path(m.group("path")).read_text(encoding="utf-8"))


class TestCliSession(unittest.TestCase):
    """``_CliSession`` adapts ``claude -p`` output into the ClaudeSession surface."""

    def _session(self, stdout: str, stderr: str, rc: int) -> tuple[CodingAgent, _CliSession]:
        agent = CodingAgent(repo="LoonyBin/repo")
        agent._run_claude_cli = MagicMock(return_value=(stdout, stderr, rc))  # type: ignore[method-assign]
        return agent, _CliSession(agent, Path("/wt"), "sid-1")

    def test_success_returns_turn_result(self) -> None:
        agent, sess = self._session("the answer", "", 0)
        result = sess.send_turn("do it", timeout=123.0)
        self.assertEqual(result.text, "the answer")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertFalse(result.was_interrupted)
        # session id + timeout are threaded through to the CLI runner.
        _, kwargs = agent._run_claude_cli.call_args
        self.assertEqual(kwargs["session_id"], "sid-1")
        self.assertEqual(kwargs["timeout"], 123.0)
        self.assertEqual(kwargs["cwd"], Path("/wt"))

    def test_quota_output_raises_quota_error(self) -> None:
        _, sess = self._session("You've hit your limit · resets 7:30pm", "", 0)
        with self.assertRaises(QuotaExceededError):
            sess.send_turn("do it", timeout=1.0)

    def test_nonzero_exit_raises_session_error(self) -> None:
        _, sess = self._session("", "boom: something failed", 1)
        with self.assertRaises(ClaudeSessionError) as ctx:
            sess.send_turn("do it", timeout=1.0)
        self.assertIn("boom: something failed", str(ctx.exception))

    def test_timeout_rc_raises_session_error(self) -> None:
        _, sess = self._session("", "claude -p timed out after 5s", 124)
        with self.assertRaises(ClaudeSessionError) as ctx:
            sess.send_turn("do it", timeout=5.0)
        self.assertIn("timed out", str(ctx.exception))


class TestOpenSessionIds(unittest.TestCase):
    """``_open_session`` preserves a given id and invents one for a fresh branch."""

    def test_preserves_given_session_id(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        sess = agent._open_session(Path("/wt"), "deterministic-id")
        self.assertEqual(sess.session_id, "deterministic-id")

    def test_invents_id_when_none(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        sess = agent._open_session(Path("/wt"), None)
        self.assertTrue(sess.session_id)  # a fresh uuid so phases stay resumable
        # distinct sessions get distinct invented ids
        other = agent._open_session(Path("/wt"), None)
        self.assertNotEqual(sess.session_id, other.session_id)


class TestExecuteUsesSession(unittest.TestCase):
    """The simple execute() path opens one session and runs a single slash-command turn."""

    def setUp(self) -> None:
        # A real worktree with the bundled commands installed, so _command_turn
        # finds the command file and writes a real scratch context file.
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name)
        install_commands(self.worktree)
        self.addCleanup(self._tmp.cleanup)

    def _agent_and_task(self) -> tuple[CodingAgent, MagicMock]:
        agent = CodingAgent(repo="LoonyBin/repo")
        task = MagicMock()
        task.command_name = "resolve-conflicts"
        task.context_payload.return_value = {"pr_number": 5, "branch": "feature/5"}
        task.worktree_key = "pr-5-conflicts"
        task.session_key = "pr:5"
        return agent, task

    def test_success(self) -> None:
        agent, task = self._agent_and_task()
        session = MagicMock()
        session.send_turn.return_value = _turn("done")

        # Keep the scratch context file around so we can inspect its payload
        # (the finally block would otherwise clean it up before we read it).
        with patch.object(agent, "_open_session", return_value=session) as open_mock, \
                patch.object(agent, "_close_session") as close_mock, \
                patch("loony_dev.agents.coding.cleanup_context_dir"), \
                patch.object(agent, "_get_head_commit", return_value="abc"), \
                patch.object(agent, "_has_code_changes", return_value=True), \
                patch.object(agent, "_generate_summary", return_value="summary"):
            result = agent.execute(task, self.worktree)

        self.assertTrue(result.success)
        self.assertEqual(result.output, "done")
        open_mock.assert_called_once()
        close_mock.assert_called_once()
        session.send_turn.assert_called_once()
        # The turn is a slash-command invocation, and the scratch file carries
        # the task's context payload.
        turn = session.send_turn.call_args.args[0]
        payload = _assert_turn(self, turn, "resolve-conflicts")
        self.assertEqual(payload, {"pr_number": 5, "branch": "feature/5"})

    def test_missing_command_is_loud_failure(self) -> None:
        agent, task = self._agent_and_task()
        # An empty worktree with no installed commands → config drift.
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))

        with patch.object(agent, "_open_session") as open_mock, \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, empty)

        self.assertFalse(result.success)
        self.assertIn("not installed", result.summary)
        # We never opened a session or sent an inline turn.
        open_mock.assert_not_called()

    def test_quota_error_pauses_agent(self) -> None:
        agent, task = self._agent_and_task()
        session = MagicMock()
        session.send_turn.side_effect = QuotaExceededError(
            "usage limit reached. Your limit will reset at 2pm (America/New_York)",
        )

        with patch.object(agent, "_open_session", return_value=session), \
                patch.object(agent, "_close_session") as close_mock, \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, self.worktree)

        self.assertFalse(result.success)
        self.assertTrue(result.rate_limited)
        self.assertTrue(agent.is_disabled())
        # The session is still closed even when the turn raises.
        close_mock.assert_called_once()

    def test_session_error_returns_failure(self) -> None:
        agent, task = self._agent_and_task()
        session = MagicMock()
        session.send_turn.side_effect = ClaudeSessionError("session process exited mid-turn")

        with patch.object(agent, "_open_session", return_value=session), \
                patch.object(agent, "_close_session"), \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, self.worktree)

        self.assertFalse(result.success)
        self.assertFalse(result.rate_limited)

    def test_open_failure_returns_failure(self) -> None:
        agent, task = self._agent_and_task()

        with patch.object(agent, "_open_session", side_effect=ClaudeSessionError("boom")), \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, self.worktree)

        self.assertFalse(result.success)
        self.assertIn("Failed to start Claude session", result.summary)


class TestExecuteIssueQuota(unittest.TestCase):
    """A quota error during an execute_issue phase pauses the agent."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name)
        install_commands(self.worktree)
        self.addCleanup(self._tmp.cleanup)

    def test_implement_phase_quota_error_pauses_agent(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        task = MagicMock()
        task.issue.number = 7
        task.issue.title = "My feature"
        task.branch_name = "feature/7"
        task.session_key = "issue:7"
        task.worktree_key = "issue-7"
        task.implement_payload.return_value = {"issue_number": 7, "title": "My feature"}

        session = MagicMock()
        session.send_turn.side_effect = QuotaExceededError(
            "usage limit reached. Your limit will reset at 2pm (America/New_York)",
        )
        fake_git = MagicMock()
        fake_git.count_commits_ahead.return_value = 0

        with patch("loony_dev.git.GitRepo") as GitRepoCls, \
                patch("loony_dev.coderabbit.is_available", return_value=False), \
                patch("loony_dev.agents.coding.cleanup_context_dir"), \
                patch.object(agent, "_open_session", return_value=session), \
                patch.object(agent, "_close_session") as close_mock:
            GitRepoCls.detect_default_branch.return_value = "main"
            GitRepoCls.return_value = fake_git
            result = agent.execute_issue(task, self.worktree)

        self.assertFalse(result.success)
        self.assertTrue(result.rate_limited)
        self.assertTrue(agent.is_disabled())
        # The single session is still closed even when the implement turn raises.
        close_mock.assert_called_once()
        session.send_turn.assert_called_once()
        # The implement turn is a /implement-issue slash command with the payload.
        turn = session.send_turn.call_args.args[0]
        payload = _assert_turn(self, turn, "implement-issue")
        self.assertEqual(payload["issue_number"], 7)


class TestSessionRegistry(unittest.TestCase):
    """_register_session / terminate close live sessions on shutdown."""

    def test_terminate_closes_registered_sessions(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        session = MagicMock()
        agent._register_session(session)

        agent.terminate()

        session.close.assert_called_once()

    def test_unregister_prevents_close(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        session = MagicMock()
        agent._register_session(session)
        agent._unregister_session(session)

        agent.terminate()

        session.close.assert_not_called()

    def test_terminate_survives_close_error(self) -> None:
        agent = CodingAgent(repo="LoonyBin/repo")
        bad = MagicMock()
        bad.close.side_effect = RuntimeError("already gone")
        agent._register_session(bad)

        # Must not raise.
        agent.terminate()
        bad.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
