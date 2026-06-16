"""Tests for git worktree lifecycle helpers (issue #125)."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.git import GitRepo, WorktreeInfo
from loony_dev.models import GitError


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock(spec=subprocess.CompletedProcess)
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


PORCELAIN_TWO_WORKTREES = (
    "worktree /repo\n"
    "HEAD aaa111\n"
    "branch refs/heads/main\n"
    "\n"
    "worktree /repo/wt/issue-9\n"
    "HEAD bbb222\n"
    "branch refs/heads/issue-9/fix\n"
    "\n"
)

PORCELAIN_DETACHED_AND_BARE = (
    "worktree /repo\n"
    "bare\n"
    "\n"
    "worktree /repo/wt/detached\n"
    "HEAD ccc333\n"
    "detached\n"
    "\n"
)


class TestListWorktrees(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/repo"), default_branch="main")

    def test_parses_branches_and_strips_refs_heads(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_proc(0, stdout=PORCELAIN_TWO_WORKTREES),
        ) as mock_run:
            worktrees = self.repo.list_worktrees()

        mock_run.assert_called_once_with(
            ["git", "worktree", "list", "--porcelain"],
            cwd=Path("/repo"),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(len(worktrees), 2)
        self.assertEqual(
            worktrees[0],
            WorktreeInfo(
                path=Path("/repo"),
                branch="main",
                head="aaa111",
            ),
        )
        self.assertEqual(
            worktrees[1],
            WorktreeInfo(
                path=Path("/repo/wt/issue-9"),
                branch="issue-9/fix",
                head="bbb222",
            ),
        )

    def test_handles_detached_and_bare_records(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_proc(0, stdout=PORCELAIN_DETACHED_AND_BARE),
        ):
            worktrees = self.repo.list_worktrees()

        self.assertEqual(len(worktrees), 2)
        bare = worktrees[0]
        self.assertTrue(bare.bare)
        self.assertIsNone(bare.branch)
        self.assertIsNone(bare.head)

        detached = worktrees[1]
        self.assertTrue(detached.detached)
        self.assertIsNone(detached.branch)
        self.assertEqual(detached.head, "ccc333")
        self.assertEqual(detached.path, Path("/repo/wt/detached"))

    def test_empty_output_returns_empty_list(self) -> None:
        with patch("subprocess.run", return_value=_proc(0, stdout="")):
            self.assertEqual(self.repo.list_worktrees(), [])

    def test_trailing_record_without_blank_line(self) -> None:
        porcelain = (
            "worktree /repo\n"
            "HEAD aaa111\n"
            "branch refs/heads/main\n"
        )
        with patch("subprocess.run", return_value=_proc(0, stdout=porcelain)):
            worktrees = self.repo.list_worktrees()
        self.assertEqual(len(worktrees), 1)
        self.assertEqual(worktrees[0].branch, "main")


class TestCreateWorktree(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/repo"), default_branch="main")

    def test_invokes_worktree_add_with_default_base(self) -> None:
        with patch.object(self.repo, "list_worktrees", return_value=[]):
            with patch(
                "subprocess.run",
                side_effect=[_proc(1), _proc(0)],
            ) as mock_run:
                result = self.repo.create_worktree(
                    "issue-1/foo", Path("/repo/wt/issue-1")
                )

        self.assertEqual(result, Path("/repo/wt/issue-1"))
        self.assertEqual(mock_run.call_count, 2)
        show_ref_call, add_call = mock_run.call_args_list
        self.assertEqual(
            show_ref_call.args[0],
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/issue-1/foo"],
        )
        self.assertEqual(
            add_call.args[0],
            ["git", "worktree", "add", "-B", "issue-1/foo", "/repo/wt/issue-1", "main"],
        )
        self.assertEqual(add_call.kwargs["cwd"], Path("/repo"))
        self.assertTrue(add_call.kwargs["check"])

    def test_uses_existing_branch_as_start_ref_when_present(self) -> None:
        with patch.object(self.repo, "list_worktrees", return_value=[]):
            with patch(
                "subprocess.run",
                side_effect=[_proc(0), _proc(0)],
            ) as mock_run:
                self.repo.create_worktree(
                    "issue-1/foo", Path("/repo/wt/issue-1")
                )

        self.assertEqual(mock_run.call_count, 2)
        add_call = mock_run.call_args_list[1]
        self.assertEqual(
            add_call.args[0],
            ["git", "worktree", "add", "-B", "issue-1/foo", "/repo/wt/issue-1", "issue-1/foo"],
        )

    def test_explicit_base_is_passed_through(self) -> None:
        with patch.object(self.repo, "list_worktrees", return_value=[]):
            with patch("subprocess.run", return_value=_proc(0)) as mock_run:
                self.repo.create_worktree(
                    "feature/x",
                    Path("/repo/wt/feature-x"),
                    base="origin/main",
                )

        mock_run.assert_called_once_with(
            [
                "git", "worktree", "add", "-B",
                "feature/x", "/repo/wt/feature-x", "origin/main",
            ],
            cwd=Path("/repo"),
            capture_output=True,
            text=True,
            check=True,
        )

    def test_idempotent_when_already_on_right_branch(self) -> None:
        existing = WorktreeInfo(
            path=Path("/repo/wt/issue-1"),
            branch="issue-1/foo",
            head="abc123",
        )
        with patch.object(self.repo, "list_worktrees", return_value=[existing]):
            with patch("subprocess.run") as mock_run:
                result = self.repo.create_worktree(
                    "issue-1/foo", Path("/repo/wt/issue-1")
                )

        self.assertEqual(result, Path("/repo/wt/issue-1"))
        mock_run.assert_not_called()

    def test_runs_add_when_existing_worktree_is_on_different_branch(self) -> None:
        existing = WorktreeInfo(
            path=Path("/repo/wt/issue-1"),
            branch="some-other-branch",
            head="abc123",
        )
        with patch.object(self.repo, "list_worktrees", return_value=[existing]):
            with patch(
                "subprocess.run",
                side_effect=[_proc(1), _proc(0)],
            ) as mock_run:
                self.repo.create_worktree(
                    "issue-1/foo", Path("/repo/wt/issue-1")
                )
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args.args[0][:4], ["git", "worktree", "add", "-B"])

    def test_empty_branch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.create_worktree("", Path("/repo/wt/x"))

    def test_git_failure_propagates(self) -> None:
        with patch.object(self.repo, "list_worktrees", return_value=[]):
            with patch(
                "subprocess.run",
                side_effect=[
                    _proc(1),
                    subprocess.CalledProcessError(
                        returncode=128,
                        cmd=["git", "worktree", "add"],
                        stderr="fatal: target path already exists",
                    ),
                ],
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.repo.create_worktree(
                        "issue-1/foo", Path("/repo/wt/issue-1")
                    )


class TestRemoveWorktree(unittest.TestCase):

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/repo"), default_branch="main")

    def test_runs_remove_then_prune(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=[_proc(0), _proc(0)],
        ) as mock_run:
            self.repo.remove_worktree(Path("/repo/wt/issue-1"))

        self.assertEqual(mock_run.call_count, 2)
        remove_call, prune_call = mock_run.call_args_list
        self.assertEqual(
            remove_call.args[0],
            ["git", "worktree", "remove", "--force", "/repo/wt/issue-1"],
        )
        self.assertEqual(remove_call.kwargs["cwd"], Path("/repo"))
        self.assertEqual(prune_call.args[0], ["git", "worktree", "prune"])
        self.assertEqual(prune_call.kwargs["cwd"], Path("/repo"))

    def test_tolerates_missing_worktree_and_still_prunes(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=[
                _proc(1, stderr="fatal: '/repo/wt/gone' is not a working tree"),
                _proc(0),
            ],
        ) as mock_run:
            self.repo.remove_worktree(Path("/repo/wt/gone"))

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[1].args[0], ["git", "worktree", "prune"])

    def test_raises_on_unknown_failure_but_still_prunes(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=[_proc(1, stderr="some unexpected error"), _proc(0)],
        ) as mock_run:
            with self.assertRaises(GitError):
                self.repo.remove_worktree(Path("/repo/wt/x"))

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[1].args[0], ["git", "worktree", "prune"])


class TestSyncWorktreeToUpstream(unittest.TestCase):
    """Reuse-path upstream sync runs inside the worktree (issue #198)."""

    def setUp(self) -> None:
        self.repo = GitRepo(Path("/repo"), default_branch="main")

    def test_fetches_then_hard_resets_inside_worktree(self) -> None:
        wt = Path("/repo/.worktrees/owner/repo/issue-7")
        with patch(
            "subprocess.run",
            side_effect=[_proc(0, stdout="issue-7/slug\n"), _proc(0), _proc(0)],
        ) as mock_run:
            self.repo.sync_worktree_to_upstream(wt, "issue-7/slug")

        self.assertEqual(mock_run.call_count, 3)
        head_call, fetch_call, reset_call = mock_run.call_args_list
        # HEAD is read from inside the worktree to confirm the branch first.
        self.assertEqual(head_call.args[0], ["git", "rev-parse", "--abbrev-ref", "HEAD"])
        self.assertEqual(head_call.kwargs["cwd"], wt)
        self.assertEqual(fetch_call.args[0], ["git", "fetch", "origin", "issue-7/slug"])
        self.assertEqual(fetch_call.kwargs["cwd"], wt)
        self.assertTrue(fetch_call.kwargs["check"])
        self.assertEqual(reset_call.args[0], ["git", "reset", "--hard", "origin/issue-7/slug"])
        self.assertEqual(reset_call.kwargs["cwd"], wt)
        self.assertTrue(reset_call.kwargs["check"])

    def test_refuses_when_worktree_on_other_branch(self) -> None:
        # A worktree on a different branch must never have its ref reset onto
        # origin/<branch> — sync raises instead of fetching/resetting.
        wt = Path("/repo/.worktrees/owner/repo/issue-7")
        with patch(
            "subprocess.run", side_effect=[_proc(0, stdout="some-other-branch\n")],
        ) as mock_run:
            with self.assertRaises(GitError):
                self.repo.sync_worktree_to_upstream(wt, "issue-7/slug")
        # Only the HEAD probe ran; no fetch/reset was attempted.
        self.assertEqual(mock_run.call_count, 1)

    def test_empty_branch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.sync_worktree_to_upstream(Path("/repo/wt/x"), "")


if __name__ == "__main__":
    unittest.main()
