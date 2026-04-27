"""Tests for idempotent PR creation in CodingAgent._create_pr (issue #112)."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.agents.coding import CodingAgent


def _mock_task(issue_number: int = 42, title: str = "My feature") -> MagicMock:
    task = MagicMock()
    task.issue.number = issue_number
    task.issue.title = title
    task.commit_exhausted = False
    task.review_exhausted = False
    return task


def _cpe(stdout: str = "", stderr: str = "") -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(1, ["gh", "pr", "create"])
    exc.stdout = stdout
    exc.stderr = stderr
    return exc


class TestCreatePrIdempotency(unittest.TestCase):

    def setUp(self) -> None:
        self.agent = CodingAgent(Path("/fake/repo"))

    def _patch_repo_name(self, name: str | None = "org/repo"):
        if name is None:
            return patch("subprocess.check_output", side_effect=Exception("no gh"))
        return patch("subprocess.check_output", return_value=name.encode())

    # ------------------------------------------------------------------
    # Existing PR — should not raise
    # ------------------------------------------------------------------

    def test_existing_pr_stderr_does_not_raise(self) -> None:
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stderr="GraphQL: A pull request already exists for org:feature/42.")):
                with patch("subprocess.check_output", return_value=b"https://github.com/org/repo/pull/7\n"):
                    self.agent._create_pr(task, "feature/42")

    def test_existing_pr_stdout_does_not_raise(self) -> None:
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stdout="a pull request already exists for this branch")):
                with patch("subprocess.check_output", return_value=b"https://github.com/org/repo/pull/7\n"):
                    self.agent._create_pr(task, "feature/42")

    def test_existing_pr_case_insensitive(self) -> None:
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stderr="A PULL REQUEST ALREADY EXISTS for feature/42")):
                with patch("subprocess.check_output", return_value=b"https://github.com/org/repo/pull/7\n"):
                    self.agent._create_pr(task, "feature/42")

    def test_existing_pr_view_cmd_failure_still_does_not_raise(self) -> None:
        """If gh pr view fails after detecting duplicate, we still return successfully."""
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stderr="GraphQL: A pull request already exists for org:feature/42.")):
                with patch("subprocess.check_output", side_effect=Exception("gh pr view failed")):
                    self.agent._create_pr(task, "feature/42")

    # ------------------------------------------------------------------
    # Real errors — must still raise
    # ------------------------------------------------------------------

    def test_auth_error_still_raises(self) -> None:
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stderr="error connecting to api.github.com: 401 Unauthorized")):
                with patch("subprocess.check_output", return_value=b"org/repo"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.agent._create_pr(task, "feature/42")

    def test_network_error_still_raises(self) -> None:
        task = _mock_task()
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", side_effect=_cpe(stderr="failed to create pull request: could not connect to GitHub")):
                with patch("subprocess.check_output", return_value=b"org/repo"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.agent._create_pr(task, "feature/42")

    # ------------------------------------------------------------------
    # Happy path still works
    # ------------------------------------------------------------------

    def test_successful_creation_logs_url(self) -> None:
        task = _mock_task()
        success_proc = MagicMock(spec=subprocess.CompletedProcess)
        success_proc.returncode = 0
        success_proc.stdout = "https://github.com/org/repo/pull/99\n"
        with self._patch_repo_name("org/repo"):
            with patch("subprocess.run", return_value=success_proc):
                self.agent._create_pr(task, "feature/42")


if __name__ == "__main__":
    unittest.main()
