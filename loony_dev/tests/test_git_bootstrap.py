"""Unit tests for empty-repo bootstrap behaviour (issue #60).

Uses local bare repos — no network required.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from loony_dev.git import GitRepo


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_bare_repo(tmp: Path) -> Path:
    """Create a local bare repository."""
    bare = tmp / "bare.git"
    bare.mkdir()
    _run("git", "init", "--bare", str(bare), cwd=tmp)
    return bare


def _clone(bare: Path, tmp: Path, name: str = "clone") -> Path:
    """Clone bare repo into a local directory."""
    dest = tmp / name
    _run("git", "clone", str(bare), str(dest), cwd=tmp)
    return dest


def _make_commit(repo: Path, branch: str = "main") -> None:
    """Create an initial commit on the given branch."""
    _run("git", "checkout", "-b", branch, cwd=repo)
    (repo / "README.md").write_text("hello")
    _run("git", "add", "README.md", cwd=repo)
    _run("git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "-m", "init", cwd=repo)
    _run("git", "push", "-u", "origin", branch, cwd=repo)


class TestHasCommits(unittest.TestCase):
    def test_has_commits_false_on_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bare = _make_bare_repo(tmp_path)
            clone = _clone(bare, tmp_path)
            repo = GitRepo(clone)
            self.assertFalse(repo.has_commits())

    def test_has_commits_true_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bare = _make_bare_repo(tmp_path)
            clone = _clone(bare, tmp_path)
            _make_commit(clone)
            repo = GitRepo(clone)
            self.assertTrue(repo.has_commits())


class TestGetDefaultBranch(unittest.TestCase):
    def test_get_default_branch_returns_configured_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bare = _make_bare_repo(tmp_path)
            clone = _clone(bare, tmp_path)
            _make_commit(clone, branch="trunk")
            # After push, set remote HEAD so symbolic-ref resolves
            _run("git", "remote", "set-head", "origin", "trunk", cwd=clone)
            repo = GitRepo(clone)
            self.assertEqual(repo.get_default_branch(), "trunk")

    def test_get_default_branch_falls_back_to_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bare = _make_bare_repo(tmp_path)
            clone = _clone(bare, tmp_path)
            # Do not set refs/remotes/origin/HEAD — symbolic-ref will fail
            repo = GitRepo(clone)
            self.assertEqual(repo.get_default_branch(), "main")


class TestEnsureMainUpToDate(unittest.TestCase):
    def test_ensure_main_up_to_date_skips_on_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bare = _make_bare_repo(tmp_path)
            clone = _clone(bare, tmp_path)
            repo = GitRepo(clone)
            # Must not raise; must not create any commits
            repo.ensure_main_up_to_date()
            self.assertFalse(repo.has_commits())


if __name__ == "__main__":
    unittest.main()
