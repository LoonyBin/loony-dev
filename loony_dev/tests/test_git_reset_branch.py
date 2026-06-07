"""Tests for GitRepo.reset_branch_to_upstream (issue #145).

The orchestrator must never switch the base checkout onto a task's branch: the
base stays pinned to the default branch and tasks run only in worktrees. Before
the fix, ``reset_branch_to_upstream`` did ``git checkout -B <branch>`` in the
base checkout, parking it on the PR branch; the subsequent
``git worktree add -B <branch> <path> <branch>`` then failed with
``fatal: '<branch>' is already used by worktree``.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from loony_dev.git import GitRepo


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


class TestResetBranchToUpstreamCommands(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/repo"), default_branch="main")

    def test_fetches_then_force_moves_ref_without_checkout(self) -> None:
        with patch.object(self.repo, "_run") as mock_run:
            self.repo.reset_branch_to_upstream("feature/x")

        self.assertEqual(
            mock_run.call_args_list,
            [
                call("fetch", "origin", "feature/x"),
                call("branch", "-f", "feature/x", "origin/feature/x"),
            ],
        )

    def test_does_not_checkout_or_clean_the_base(self) -> None:
        with patch.object(self.repo, "_run") as mock_run:
            self.repo.reset_branch_to_upstream("feature/x")

        verbs = [c.args[0] for c in mock_run.call_args_list]
        self.assertNotIn("checkout", verbs)
        self.assertNotIn("clean", verbs)

    def test_empty_branch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.reset_branch_to_upstream("   ")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


class TestResetBranchToUpstreamRealRepo(unittest.TestCase):
    """End-to-end-ish test against a real temp repo with a remote."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)

        # A bare remote.
        self.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       check=True, capture_output=True)

        # The base checkout, cloned from the remote.
        self.base = root / "base"
        subprocess.run(["git", "clone", str(self.remote), str(self.base)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            _git(self.base, "config", k, v)

        # Seed main and a PR branch on the remote.
        (self.base / "README.md").write_text("hello\n")
        _git(self.base, "add", "-A")
        _git(self.base, "commit", "-m", "init")
        _git(self.base, "push", "origin", "main")

        _git(self.base, "checkout", "-b", "feature/x")
        (self.base / "feat.txt").write_text("feature\n")
        _git(self.base, "add", "-A")
        _git(self.base, "commit", "-m", "feat")
        _git(self.base, "push", "origin", "feature/x")

        # Return the base checkout to the default branch, as the orchestrator
        # keeps it pinned there. Drop the local feature branch so reset must
        # recreate it from origin.
        _git(self.base, "checkout", "main")
        _git(self.base, "branch", "-D", "feature/x")

        self.git = GitRepo(self.base, default_branch="main")

    def test_base_stays_on_default_branch(self) -> None:
        self.git.reset_branch_to_upstream("feature/x")
        self.assertEqual(_git(self.base, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_local_ref_matches_origin_after_reset(self) -> None:
        self.git.reset_branch_to_upstream("feature/x")
        local = _git(self.base, "rev-parse", "feature/x")
        remote = _git(self.base, "rev-parse", "origin/feature/x")
        self.assertEqual(local, remote)

    def test_worktree_add_succeeds_after_reset(self) -> None:
        """The bug: worktree add failed because the base was parked on the branch."""
        self.git.reset_branch_to_upstream("feature/x")

        wt_path = Path(self._tmpdir.name) / "wt-feature-x"
        # Mirrors Orchestrator.dispatch: create_worktree(branch=target, base=None).
        result = self.git.create_worktree(branch="feature/x", path=wt_path, base=None)

        self.assertTrue(result.exists())
        # The worktree is on the PR branch and carries its history.
        self.assertEqual(
            _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD"), "feature/x"
        )
        self.assertTrue((wt_path / "feat.txt").exists())
        # The base checkout never moved off main.
        self.assertEqual(_git(self.base, "rev-parse", "--abbrev-ref", "HEAD"), "main")


if __name__ == "__main__":
    unittest.main()
