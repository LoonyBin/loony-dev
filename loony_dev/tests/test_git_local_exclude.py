"""Tests for GitRepo.add_local_exclude — keeping generated slash commands out of
git status / git add -A in worker worktrees (issue #166)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from loony_dev.commands import install_commands
from loony_dev.git import GitRepo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


class TestAddLocalExclude(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("hi\n")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-q", "-m", "init")

    def test_installed_commands_are_excluded_from_status(self) -> None:
        install_commands(self.repo)
        # Before excluding, the generated commands show as untracked.
        self.assertIn(".claude/", _git(self.repo, "status", "--porcelain"))

        GitRepo.add_local_exclude(self.repo, ".claude/commands/")

        # Now git ignores them entirely.
        self.assertEqual(_git(self.repo, "status", "--porcelain").strip(), "")

    def test_add_all_does_not_stage_excluded_commands(self) -> None:
        install_commands(self.repo)
        GitRepo.add_local_exclude(self.repo, ".claude/commands/")
        _git(self.repo, "add", "-A")
        staged = _git(self.repo, "diff", "--cached", "--name-only")
        self.assertNotIn(".claude/commands", staged)

    def test_is_idempotent(self) -> None:
        GitRepo.add_local_exclude(self.repo, ".claude/commands/")
        GitRepo.add_local_exclude(self.repo, ".claude/commands/")
        exclude = (self.repo / ".git" / "info" / "exclude").read_text()
        self.assertEqual(exclude.count(".claude/commands/"), 1)

    def test_applies_inside_linked_worktree(self) -> None:
        # The exclude lives in the common git dir, so it covers worktrees too.
        GitRepo.add_local_exclude(self.repo, ".claude/commands/")
        wt = Path(self._tmp.name) / "wt"
        _git(self.repo, "worktree", "add", "-q", str(wt), "HEAD")
        install_commands(wt)
        self.assertEqual(_git(wt, "status", "--porcelain").strip(), "")


if __name__ == "__main__":
    unittest.main()
