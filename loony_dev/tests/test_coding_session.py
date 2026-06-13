"""Tests for CodingAgent driving a persistent ClaudeSession (issue #162).

Covers the simple ``execute`` path, quota translation from
``QuotaExceededError`` into a rate-limited TaskResult, and the session
registry that lets ``terminate`` tear sessions down on shutdown.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.agents.claude_session import (
    ClaudeSessionError,
    QuotaExceededError,
    TurnResult,
)
from loony_dev.agents.coding import CodingAgent


def _turn(text: str = "did the work") -> TurnResult:
    return TurnResult(text=text, stop_reason="end_turn", was_interrupted=False, entries_added=2)


class TestExecuteUsesSession(unittest.TestCase):
    """The simple execute() path opens one session and runs a single turn."""

    def _agent_and_task(self) -> tuple[CodingAgent, MagicMock]:
        agent = CodingAgent(repo="LoonyBin/repo")
        task = MagicMock()
        task.describe.return_value = "fix the conflict"
        task.session_key = "pr:5"
        return agent, task

    def test_success(self) -> None:
        agent, task = self._agent_and_task()
        session = MagicMock()
        session.send_turn.return_value = _turn("done")

        with patch.object(agent, "_open_session", return_value=session) as open_mock, \
                patch.object(agent, "_close_session") as close_mock, \
                patch.object(agent, "_get_head_commit", return_value="abc"), \
                patch.object(agent, "_has_code_changes", return_value=True), \
                patch.object(agent, "_generate_summary", return_value="summary"):
            result = agent.execute(task, Path("/fake/worktree"))

        self.assertTrue(result.success)
        self.assertEqual(result.output, "done")
        open_mock.assert_called_once()
        close_mock.assert_called_once()
        session.send_turn.assert_called_once()
        self.assertEqual(session.send_turn.call_args.args[0], "fix the conflict")

    def test_quota_error_pauses_agent(self) -> None:
        agent, task = self._agent_and_task()
        session = MagicMock()
        session.send_turn.side_effect = QuotaExceededError(
            "usage limit reached. Your limit will reset at 2pm (America/New_York)",
        )

        with patch.object(agent, "_open_session", return_value=session), \
                patch.object(agent, "_close_session") as close_mock, \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, Path("/fake/worktree"))

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
            result = agent.execute(task, Path("/fake/worktree"))

        self.assertFalse(result.success)
        self.assertFalse(result.rate_limited)

    def test_open_failure_returns_failure(self) -> None:
        agent, task = self._agent_and_task()

        with patch.object(agent, "_open_session", side_effect=ClaudeSessionError("boom")), \
                patch.object(agent, "_get_head_commit", return_value="abc"):
            result = agent.execute(task, Path("/fake/worktree"))

        self.assertFalse(result.success)
        self.assertIn("Failed to start Claude session", result.summary)


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
