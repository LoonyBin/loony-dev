"""Tests for Orchestrator worktree lifecycle (issue #126)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from loony_dev.git import WorktreeInfo
from loony_dev.models import TaskResult
from loony_dev.orchestrator import Orchestrator
from loony_dev.tasks.issue_task import IssueTask


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.name = "owner/repo"
    repo.owner = "owner"
    return repo


def _make_git(tmp_path: Path, *, worktrees: list[WorktreeInfo] | None = None) -> MagicMock:
    git = MagicMock()
    git.work_dir = tmp_path
    git.default_branch = "main"
    git.list_worktrees.return_value = worktrees if worktrees is not None else []
    return git


def _make_orchestrator(tmp_path: Path, git: MagicMock, agents: list) -> Orchestrator:
    return Orchestrator(repo=_make_repo(), git=git, agents=agents, interval=60)


class TestWorktreeLifecycle(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        self.orch = _make_orchestrator(self.tmp, self.git, [self.agent])
        # Discard any calls from the startup sweep so assertions target dispatch.
        self.git.reset_mock()

    def _make_task(self, *, worktree_key: str | None, target_branch: str | None = None) -> MagicMock:
        task = MagicMock()
        task.worktree_key = worktree_key
        task.target_branch = target_branch
        task.describe.return_value = "do work"
        return task

    def test_successful_task_creates_then_removes_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-1", target_branch="feature/x")
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="done")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "pr-1"
        self.git.create_worktree.assert_called_once_with(
            branch="feature/x", path=expected_path, base=None,
        )
        self.git.remove_worktree.assert_called_once_with(expected_path)
        # Agent runs inside the worktree, not the base checkout.
        self.assertEqual(self.agent.execute.call_args.kwargs["work_dir"], expected_path)
        task.on_complete.assert_called_once()
        task.on_failure.assert_not_called()

    def test_failing_agent_still_removes_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-2", target_branch="feature/y")
        self.agent.execute.side_effect = RuntimeError("boom")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "pr-2"
        self.git.create_worktree.assert_called_once()
        # finally clause removes the worktree even though the agent raised.
        self.git.remove_worktree.assert_called_once_with(expected_path)
        task.on_failure.assert_called_once()

    def test_unsuccessful_result_still_removes_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-3", target_branch="feature/z")
        self.agent.execute.return_value = TaskResult(
            success=False, output="", summary="nope",
        )

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "pr-3"
        self.git.remove_worktree.assert_called_once_with(expected_path)
        task.on_failure.assert_called_once()
        task.on_complete.assert_not_called()

    def test_null_task_creates_no_worktree(self) -> None:
        task = self._make_task(worktree_key=None)
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="ok")

        self.orch.dispatch(self.agent, task)

        self.git.create_worktree.assert_not_called()
        self.git.remove_worktree.assert_not_called()
        # Runs against the base checkout.
        self.assertEqual(self.agent.execute.call_args.kwargs["work_dir"], self.tmp)
        task.on_complete.assert_called_once()

    def test_issue_task_uses_default_branch_as_base(self) -> None:
        issue = MagicMock()
        issue.number = 7
        issue.title = "Add feature"
        task = IssueTask(issue)
        # IssueTask.on_start touches GitHub — stub it out for this unit test.
        task.on_start = MagicMock()
        task.on_complete = MagicMock()
        task.on_failure = MagicMock()

        coding_agent = MagicMock()
        # Not a CodingAgent instance, so dispatch routes through execute(); that is
        # fine for verifying the worktree branch/base computation.
        coding_agent.execute.return_value = TaskResult(success=True, output="", summary="ok")

        self.orch.dispatch(coding_agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "issue-7"
        self.git.create_worktree.assert_called_once_with(
            branch=task.branch_name, path=expected_path, base="main",
        )
        self.git.remove_worktree.assert_called_once_with(expected_path)

    def test_planning_task_forks_throwaway_branch_from_default(self) -> None:
        # A task with a worktree_key but no target_branch (e.g. PlanningTask)
        # must NOT reuse the default branch — the base checkout already holds it.
        task = self._make_task(worktree_key="issue-5-plan", target_branch=None)
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="plan")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "issue-5-plan"
        self.git.create_worktree.assert_called_once_with(
            branch="issue-5-plan", path=expected_path, base="main",
        )


class TestStartupSweep(unittest.TestCase):

    def test_removes_preexisting_worktrees_under_root(self) -> None:
        tmp = Path("/repo")
        root = tmp / ".worktrees" / "owner" / "repo"
        stale = WorktreeInfo(path=root / "pr-9", branch="feature/old", head="abc")
        outside = WorktreeInfo(path=tmp / "other-wt", branch="x", head="def")
        base = WorktreeInfo(path=tmp, branch="main", head="ghi")
        git = _make_git(tmp, worktrees=[base, outside, stale])

        Orchestrator(repo=_make_repo(), git=git, agents=[], interval=60)

        git._run.assert_any_call("worktree", "prune")
        # Only the worktree under worktree_root is pruned.
        git.remove_worktree.assert_called_once_with(stale.path)

    def test_skips_bare_worktrees(self) -> None:
        tmp = Path("/repo")
        root = tmp / ".worktrees" / "owner" / "repo"
        bare = WorktreeInfo(path=root / "bare", branch=None, head=None, bare=True)
        git = _make_git(tmp, worktrees=[bare])

        Orchestrator(repo=_make_repo(), git=git, agents=[], interval=60)

        git.remove_worktree.assert_not_called()


if __name__ == "__main__":
    unittest.main()
