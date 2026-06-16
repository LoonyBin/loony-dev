"""Tests for PipelineSession — the per-pipeline session manager (issue #198)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from loony_dev.pipeline_session import PipelineSession
from loony_dev.session import session_id_for
from loony_dev.tasks.issue_task import IssueTask
from loony_dev.tasks.planning_task import PlanningTask

ROOT = Path("/repo/.worktrees/owner/repo")
REPO = "owner/repo"


def _pr_task(*, worktree_key: str, target_branch: str, session_key: str | None) -> MagicMock:
    task = MagicMock()
    task.worktree_key = worktree_key
    task.target_branch = target_branch
    task.session_key = session_key
    return task


class TestForTask(unittest.TestCase):

    def test_pr_task_forks_from_target_branch(self) -> None:
        task = _pr_task(
            worktree_key="issue-7", target_branch="issue-7/slug", session_key="issue:7",
        )
        ps = PipelineSession.for_task(
            task, worktree_root=ROOT, repo_name=REPO, default_branch="main",
        )
        self.assertEqual(ps.pipeline_key, "issue-7")
        self.assertEqual(ps.branch, "issue-7/slug")
        self.assertIsNone(ps.base)  # forks from the existing target branch ref
        self.assertEqual(ps.worktree_path, ROOT / "issue-7")
        self.assertEqual(ps.session_id, session_id_for(REPO, "issue:7"))
        self.assertFalse(ps.live)

    def test_issue_task_uses_feature_branch_and_default_base(self) -> None:
        issue = MagicMock()
        issue.number = 7
        issue.title = "Add feature"
        task = IssueTask(issue)
        ps = PipelineSession.for_task(
            task, worktree_root=ROOT, repo_name=REPO, default_branch="main",
        )
        self.assertEqual(ps.branch, task.branch_name)
        self.assertEqual(ps.base, "main")
        self.assertEqual(ps.session_id, session_id_for(REPO, "issue:7"))

    def test_planning_task_matches_issue_branch(self) -> None:
        issue = MagicMock()
        issue.number = 5
        issue.title = "Add feature"
        task = PlanningTask(issue, None, [])
        ps = PipelineSession.for_task(
            task, worktree_root=ROOT, repo_name=REPO, default_branch="main",
        )
        self.assertEqual(ps.branch, "issue-5/add-feature")
        self.assertEqual(ps.base, "main")

    def test_throwaway_task_forks_branch_named_after_key(self) -> None:
        task = MagicMock(spec=["worktree_key", "target_branch", "session_key"])
        task.worktree_key = "aux-5"
        task.target_branch = None
        task.session_key = None
        ps = PipelineSession.for_task(
            task, worktree_root=ROOT, repo_name=REPO, default_branch="main",
        )
        self.assertEqual(ps.branch, "aux-5")
        self.assertEqual(ps.base, "main")
        self.assertIsNone(ps.session_id)

    def test_no_worktree_key_rejected(self) -> None:
        task = MagicMock()
        task.worktree_key = None
        with self.assertRaises(ValueError):
            PipelineSession.for_task(
                task, worktree_root=ROOT, repo_name=REPO, default_branch="main",
            )


class TestIdleState(unittest.TestCase):

    def _ps(self) -> PipelineSession:
        return PipelineSession(
            pipeline_key="issue-7", branch="issue-7/slug", base=None,
            worktree_path=ROOT / "issue-7",
        )

    def test_mark_active_sets_stamp(self) -> None:
        ps = self._ps()
        ps.mark_active(now=123.0)
        self.assertEqual(ps.last_active, 123.0)

    def test_is_idle_requires_live(self) -> None:
        ps = self._ps()
        ps.mark_active(now=0.0)
        # Not live yet → never idle, even past the grace window.
        self.assertFalse(ps.is_idle(now=1000.0, grace=300.0))

    def test_is_idle_requires_grace_elapsed(self) -> None:
        ps = self._ps()
        ps.live = True
        ps.mark_active(now=0.0)
        self.assertFalse(ps.is_idle(now=299.0, grace=300.0))
        self.assertTrue(ps.is_idle(now=300.0, grace=300.0))


if __name__ == "__main__":
    unittest.main()
