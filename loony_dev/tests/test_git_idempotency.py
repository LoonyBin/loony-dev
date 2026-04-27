"""Tests for idempotent behaviour in GitRepo.commit_and_push (issue #112)."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from loony_dev.git import GitRepo
from loony_dev.models import GitError, HookFailureError


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


class TestCommitAndPushIdempotency(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/fake/repo"), default_branch="main")

    def _run_commit_and_push(self, commit_proc: MagicMock, push_proc: MagicMock) -> None:
        """Patch subprocess.run to return given procs and invoke commit_and_push."""
        add_proc = _proc(0)
        with patch.object(self.repo, "_run"):
            with patch("subprocess.run", side_effect=[commit_proc, push_proc]) as mock_run:
                self.repo.commit_and_push("feat: test", "feature/test")
                return mock_run

    # ------------------------------------------------------------------
    # Commit no-op cases
    # ------------------------------------------------------------------

    def test_nothing_to_commit_does_not_raise(self) -> None:
        commit = _proc(1, stdout="On branch main\nnothing to commit, working tree clean")
        push = _proc(0, stderr="Branch 'feature/test' set up to track remote branch.")
        # Should not raise
        self._run_commit_and_push(commit, push)

    def test_nothing_added_to_commit_does_not_raise(self) -> None:
        commit = _proc(1, stdout="nothing added to commit but untracked files present")
        push = _proc(0)
        self._run_commit_and_push(commit, push)

    def test_nothing_to_commit_case_insensitive(self) -> None:
        commit = _proc(1, stdout="Nothing To Commit, working tree clean")
        push = _proc(0)
        self._run_commit_and_push(commit, push)

    def test_real_commit_error_still_raises_git_error(self) -> None:
        commit = _proc(1, stderr="error: pathspec 'bogus' did not match any file(s)")
        push = _proc(0)
        with patch.object(self.repo, "_run"):
            with patch("subprocess.run", return_value=commit):
                with self.assertRaises(GitError):
                    self.repo.commit_and_push("feat: test", "feature/test")

    def test_hook_failure_on_commit_still_raises(self) -> None:
        commit = _proc(1, stdout="pre-commit hook failed (exit code 1)")
        push = _proc(0)
        with patch.object(self.repo, "_run"):
            with patch("subprocess.run", return_value=commit):
                with self.assertRaises(HookFailureError):
                    self.repo.commit_and_push("feat: test", "feature/test")

    # ------------------------------------------------------------------
    # Push no-op cases
    # ------------------------------------------------------------------

    def test_everything_up_to_date_does_not_raise(self) -> None:
        commit = _proc(0)
        push = _proc(1, stderr="Everything up-to-date")
        self._run_commit_and_push(commit, push)

    def test_already_up_to_date_does_not_raise(self) -> None:
        commit = _proc(0)
        push = _proc(1, stdout="Already up to date.")
        self._run_commit_and_push(commit, push)

    def test_already_up_to_date_case_insensitive(self) -> None:
        commit = _proc(0)
        push = _proc(1, stderr="EVERYTHING UP-TO-DATE")
        self._run_commit_and_push(commit, push)

    def test_real_push_error_still_raises_git_error(self) -> None:
        commit = _proc(0)
        push = _proc(1, stderr="error: failed to push some refs to 'origin'\nhint: Updates were rejected")
        with patch.object(self.repo, "_run"):
            with patch("subprocess.run", side_effect=[commit, push]):
                with self.assertRaises(GitError):
                    self.repo.commit_and_push("feat: test", "feature/test")

    def test_hook_failure_on_push_still_raises(self) -> None:
        commit = _proc(0)
        push = _proc(1, stderr="remote: pre-push hook failed (exit code 1)")
        with patch.object(self.repo, "_run"):
            with patch("subprocess.run", side_effect=[commit, push, _proc(0)]) as mock_run:
                with self.assertRaises(HookFailureError):
                    self.repo.commit_and_push("feat: test", "feature/test")

    # ------------------------------------------------------------------
    # Both commit and push are no-ops (the common retry scenario)
    # ------------------------------------------------------------------

    def test_both_commit_and_push_noop_does_not_raise(self) -> None:
        commit = _proc(1, stdout="nothing to commit, working tree clean")
        push = _proc(1, stderr="Everything up-to-date")
        self._run_commit_and_push(commit, push)


if __name__ == "__main__":
    unittest.main()
