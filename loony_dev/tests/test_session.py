"""Tests for deterministic session ID generation and CLI session logic."""
from __future__ import annotations

import uuid
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.session import session_id_for


class TestSessionIdFor(unittest.TestCase):
    """session_id_for must be deterministic and produce valid UUIDs."""

    def test_deterministic(self) -> None:
        a = session_id_for("LoonyBin/repo", "issue:42")
        b = session_id_for("LoonyBin/repo", "issue:42")
        self.assertEqual(a, b)

    def test_valid_uuid(self) -> None:
        result = session_id_for("LoonyBin/repo", "issue:1")
        parsed = uuid.UUID(result)
        self.assertEqual(parsed.version, 5)

    def test_different_repos_differ(self) -> None:
        a = session_id_for("LoonyBin/repo-a", "issue:1")
        b = session_id_for("LoonyBin/repo-b", "issue:1")
        self.assertNotEqual(a, b)

    def test_different_keys_differ(self) -> None:
        a = session_id_for("LoonyBin/repo", "issue:1")
        b = session_id_for("LoonyBin/repo", "issue:2")
        self.assertNotEqual(a, b)

    def test_issue_vs_pr_differ(self) -> None:
        a = session_id_for("LoonyBin/repo", "issue:1")
        b = session_id_for("LoonyBin/repo", "pr:1")
        self.assertNotEqual(a, b)

    def test_planning_and_implementation_share_id(self) -> None:
        """PlanningTask and IssueTask for the same issue must produce the same session ID."""
        planning = session_id_for("LoonyBin/repo", "issue:42")
        implementation = session_id_for("LoonyBin/repo", "issue:42")
        self.assertEqual(planning, implementation)


class TestSessionKeyOnTasks(unittest.TestCase):
    """Task subclasses must expose the correct session_key."""

    def test_planning_task_key(self) -> None:
        from loony_dev.models import Issue
        from loony_dev.tasks.planning_task import PlanningTask
        task = PlanningTask(Issue(number=7, title="t", body="b"), None, [])
        self.assertEqual(task.session_key, "issue:7")

    def test_issue_task_key(self) -> None:
        from loony_dev.models import Issue
        from loony_dev.tasks.issue_task import IssueTask
        task = IssueTask(Issue(number=7, title="t", body="b"))
        self.assertEqual(task.session_key, "issue:7")

    def test_pr_review_task_key(self) -> None:
        from loony_dev.models import PullRequest
        from loony_dev.tasks.pr_review_task import PRReviewTask
        task = PRReviewTask(PullRequest(number=10, branch="b", title="t"))
        self.assertEqual(task.session_key, "pr:10")

    def test_ci_failure_task_key(self) -> None:
        from loony_dev.models import PullRequest
        from loony_dev.tasks.ci_failure_task import CIFailureTask
        task = CIFailureTask(PullRequest(number=5, branch="b", title="t"), [])
        self.assertEqual(task.session_key, "pr:5")

    def test_conflict_task_key(self) -> None:
        from loony_dev.models import PullRequest
        from loony_dev.tasks.conflict_task import ConflictResolutionTask
        task = ConflictResolutionTask(PullRequest(number=3, branch="b", title="t"))
        self.assertEqual(task.session_key, "pr:3")

    def test_stuck_item_task_has_no_session_key(self) -> None:
        from loony_dev.models import Issue
        from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask
        task = StuckItemCleanupTask(Issue(number=1, title="t", body="b"), 12)
        self.assertIsNone(task.session_key)


class TestSessionIdForOnAgent(unittest.TestCase):
    """ClaudeQuotaMixin._session_id_for must use repo + task.session_key."""

    def test_returns_id_when_repo_and_key_present(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")
        task = MagicMock()
        task.session_key = "issue:42"
        sid = agent._session_id_for(task)
        self.assertEqual(sid, session_id_for("LoonyBin/repo", "issue:42"))

    def test_returns_none_when_no_repo(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"))  # repo defaults to ""
        task = MagicMock()
        task.session_key = "issue:42"
        self.assertIsNone(agent._session_id_for(task))

    def test_returns_none_when_no_session_key(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")
        task = MagicMock()
        task.session_key = None
        self.assertIsNone(agent._session_id_for(task))


class TestRunClaudeCli(unittest.TestCase):
    """ClaudeQuotaMixin._run_claude_cli resume/fallback logic."""

    def _make_popen_mock(self, stdout: str, stderr: str, returncode: int) -> MagicMock:
        mock_proc = MagicMock()
        mock_proc.__enter__ = MagicMock(return_value=mock_proc)
        mock_proc.__exit__ = MagicMock(return_value=False)
        mock_proc.communicate.return_value = (stdout, stderr)
        mock_proc.returncode = returncode
        return mock_proc

    def test_no_session_id_runs_plain(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")
        mock_proc = self._make_popen_mock("output", "", 0)

        with patch("loony_dev.agents.claude_quota.subprocess.Popen", return_value=mock_proc) as mock_popen:
            stdout, stderr, rc = agent._run_claude_cli("hello", cwd=Path("/tmp"))

        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "output")
        # Should NOT include --resume or --session-id
        cmd = mock_popen.call_args[0][0]
        self.assertNotIn("--resume", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_session_id_tries_resume_first(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")
        mock_proc = self._make_popen_mock("resumed output", "", 0)

        with patch("loony_dev.agents.claude_quota.subprocess.Popen", return_value=mock_proc) as mock_popen:
            stdout, stderr, rc = agent._run_claude_cli(
                "hello", cwd=Path("/tmp"), session_id="test-uuid",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "resumed output")
        # Should have used --resume
        cmd = mock_popen.call_args[0][0]
        self.assertIn("--resume", cmd)
        # Should only be called once (resume succeeded)
        self.assertEqual(mock_popen.call_count, 1)

    def test_session_not_found_falls_back_to_session_id(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")

        resume_proc = self._make_popen_mock("", "No session found for id", 1)
        create_proc = self._make_popen_mock("fresh output", "", 0)

        with patch("loony_dev.agents.claude_quota.subprocess.Popen", side_effect=[resume_proc, create_proc]) as mock_popen:
            stdout, stderr, rc = agent._run_claude_cli(
                "hello", cwd=Path("/tmp"), session_id="test-uuid",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "fresh output")
        self.assertEqual(mock_popen.call_count, 2)
        # First call: --resume
        first_cmd = mock_popen.call_args_list[0][0][0]
        self.assertIn("--resume", first_cmd)
        # Second call: --session-id
        second_cmd = mock_popen.call_args_list[1][0][0]
        self.assertIn("--session-id", second_cmd)

    def test_real_error_does_not_fallback(self) -> None:
        from loony_dev.agents.coding import CodingAgent
        agent = CodingAgent(work_dir=Path("/tmp"), repo="LoonyBin/repo")
        # Error that is NOT session-not-found
        mock_proc = self._make_popen_mock("", "quota exceeded", 1)

        with patch("loony_dev.agents.claude_quota.subprocess.Popen", return_value=mock_proc) as mock_popen:
            stdout, stderr, rc = agent._run_claude_cli(
                "hello", cwd=Path("/tmp"), session_id="test-uuid",
            )

        self.assertEqual(rc, 1)
        # Should only be called once (no fallback for real errors)
        self.assertEqual(mock_popen.call_count, 1)


class TestIsSessionNotFound(unittest.TestCase):
    def test_various_patterns(self) -> None:
        from loony_dev.agents.claude_quota import ClaudeQuotaMixin
        self.assertTrue(ClaudeQuotaMixin._is_session_not_found("Error: No session found"))
        self.assertTrue(ClaudeQuotaMixin._is_session_not_found("Session not found for id abc"))
        self.assertTrue(ClaudeQuotaMixin._is_session_not_found("Could not find session"))
        self.assertTrue(ClaudeQuotaMixin._is_session_not_found("Invalid session id"))
        self.assertTrue(ClaudeQuotaMixin._is_session_not_found("Session does not exist"))

    def test_normal_error_not_matched(self) -> None:
        from loony_dev.agents.claude_quota import ClaudeQuotaMixin
        self.assertFalse(ClaudeQuotaMixin._is_session_not_found("rate limit exceeded"))
        self.assertFalse(ClaudeQuotaMixin._is_session_not_found("network error"))


if __name__ == "__main__":
    unittest.main()
