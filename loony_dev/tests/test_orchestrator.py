"""Tests for Orchestrator worktree lifecycle (issue #126)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_successful_task_creates_and_retains_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-1", target_branch="feature/x")
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="done")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "pr-1"
        self.git.create_worktree.assert_called_once_with(
            branch="feature/x", path=expected_path, base=None,
        )
        # The worktree is retained for the pipeline's next phase (issue #198);
        # removal happens only on terminal-state reclamation, never per task.
        self.git.remove_worktree.assert_not_called()
        # Agent runs inside the worktree, not the base checkout.
        self.assertEqual(self.agent.execute.call_args.kwargs["work_dir"], expected_path)
        task.on_complete.assert_called_once()
        task.on_failure.assert_not_called()

    def test_failing_agent_retains_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-2", target_branch="feature/y")
        self.agent.execute.side_effect = RuntimeError("boom")

        self.orch.dispatch(self.agent, task)

        self.git.create_worktree.assert_called_once()
        # The worktree is retained even though the agent raised (issue #198).
        self.git.remove_worktree.assert_not_called()
        task.on_failure.assert_called_once()

    def test_unsuccessful_result_retains_worktree(self) -> None:
        task = self._make_task(worktree_key="pr-3", target_branch="feature/z")
        self.agent.execute.return_value = TaskResult(
            success=False, output="", summary="nope",
        )

        self.orch.dispatch(self.agent, task)

        self.git.remove_worktree.assert_not_called()
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
        # Worktree retained for reuse across the issue's lifecycle (issue #198).
        self.git.remove_worktree.assert_not_called()

    def test_worktree_only_task_forks_throwaway_branch_from_default(self) -> None:
        # A worktree task with neither a target_branch nor a feature branch must
        # NOT reuse the default branch directly — the base checkout already holds
        # it — so it forks a throwaway branch named after the worktree key.
        task = self._make_task(worktree_key="aux-5", target_branch=None)
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="aux")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "aux-5"
        self.git.create_worktree.assert_called_once_with(
            branch="aux-5", path=expected_path, base="main",
        )

    def test_planning_task_uses_feature_branch_from_default(self) -> None:
        # PlanningTask (#181) owns the issue's feature branch and creates it from
        # the default branch in the shared issue-N worktree — same as IssueTask.
        from loony_dev.tasks.planning_task import PlanningTask

        issue = MagicMock()
        issue.number = 5
        issue.title = "Add feature"
        task = PlanningTask(issue, None, [])
        task.on_start = MagicMock()
        task.on_complete = MagicMock()
        task.on_failure = MagicMock()
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="plan")

        self.orch.dispatch(self.agent, task)

        expected_path = self.tmp / ".worktrees" / "owner" / "repo" / "issue-5"
        self.git.create_worktree.assert_called_once_with(
            branch="issue-5/add-feature", path=expected_path, base="main",
        )


class TestPipelineReuseAndReclamation(unittest.TestCase):
    """Pipeline-scoped worktree reuse + terminal-state reclamation (issue #198)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        # base_dir under tmp so the drive-lease guard reads the same tree.
        self.orch = Orchestrator(
            repo=_make_repo(), git=self.git, agents=[self.agent],
            interval=60, base_dir=self.tmp,
        )
        self.git.reset_mock()
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="ok")

    def _make_task(self, *, worktree_key: str, target_branch: str | None = None) -> MagicMock:
        task = MagicMock()
        task.worktree_key = worktree_key
        task.target_branch = target_branch
        task.session_key = None
        task.describe.return_value = "do work"
        return task

    def _expected(self, key: str) -> Path:
        return self.tmp / ".worktrees" / "owner" / "repo" / key

    def test_consecutive_tasks_reuse_one_worktree(self) -> None:
        # Two phases on the same pipeline: the worktree is created once and synced
        # (not recreated) on the second, and never removed between them.
        t1 = self._make_task(worktree_key="issue-7", target_branch="issue-7/slug")
        t2 = self._make_task(worktree_key="issue-7", target_branch="issue-7/slug")

        self.orch.dispatch(self.agent, t1)
        self.orch.dispatch(self.agent, t2)

        expected = self._expected("issue-7")
        self.git.create_worktree.assert_called_once_with(
            branch="issue-7/slug", path=expected, base=None,
        )
        self.git.sync_worktree_to_upstream.assert_called_once_with(expected, "issue-7/slug")
        self.git.remove_worktree.assert_not_called()
        self.assertEqual(self.agent.execute.call_args.kwargs["work_dir"], expected)

    def _seed_pipeline(self, key: str) -> None:
        """Dispatch one task so *key*'s pipeline session is live with a worktree."""
        self.orch.dispatch(
            self.agent, self._make_task(worktree_key=key, target_branch=f"{key}/slug"),
        )
        self.git.reset_mock()

    def test_workspace_persists_across_consecutive_phases(self) -> None:
        # No idle grace, no timing injection: the worktree survives across
        # ticks while the pipeline's work is still open.
        self._seed_pipeline("issue-7")
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="open",
        ):
            self.orch.repo.find_pr_for_issue.return_value = 42
            for _ in range(5):
                self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_not_called()
        self.assertIn("issue-7", self.orch._pipeline_sessions)

    def test_workspace_reclaimed_on_pr_merged(self) -> None:
        self._seed_pipeline("issue-7")
        self.orch.repo.find_pr_for_issue.return_value = 42
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="merged",
        ):
            self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_called_once_with(self._expected("issue-7"))
        self.assertNotIn("issue-7", self.orch._pipeline_sessions)

    def test_workspace_reclaimed_on_pr_closed_without_merge(self) -> None:
        self._seed_pipeline("issue-7")
        self.orch.repo.find_pr_for_issue.return_value = 42
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="closed",
        ):
            self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_called_once_with(self._expected("issue-7"))
        self.assertNotIn("issue-7", self.orch._pipeline_sessions)

    def test_workspace_reclaimed_on_issue_closed_with_no_pr(self) -> None:
        self._seed_pipeline("issue-7")
        self.orch.repo.find_pr_for_issue.return_value = None
        with patch("loony_dev.github.Issue.is_closed", return_value=True):
            self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_called_once_with(self._expected("issue-7"))
        self.assertNotIn("issue-7", self.orch._pipeline_sessions)

    def test_external_pr_pipeline_reclaimed_on_merge(self) -> None:
        # A ``pr-P`` pipeline (externally opened PR, no originating issue)
        # resolves its terminal state from the PR directly.
        self._seed_pipeline("pr-9")
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="merged",
        ) as ts:
            self.orch._reclaim_completed_pipelines()
        ts.assert_called_once()
        self.assertEqual(ts.call_args.args[0], 9)
        self.git.remove_worktree.assert_called_once_with(self._expected("pr-9"))
        self.assertNotIn("pr-9", self.orch._pipeline_sessions)

    def test_not_reclaimed_while_in_flight(self) -> None:
        # In-flight wins for safety: keep the worktree even if GitHub looks done.
        self._seed_pipeline("issue-7")
        self.orch._inflight[MagicMock()] = (
            self._make_task(worktree_key="issue-7"), self.agent,
        )
        self.orch.repo.find_pr_for_issue.return_value = 42
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="merged",
        ):
            self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_not_called()
        self.assertIn("issue-7", self.orch._pipeline_sessions)

    def test_not_reclaimed_while_drive_lease_held(self) -> None:
        from loony_dev import pipeline_lease

        self._seed_pipeline("issue-7")
        # A human interrogation holds the pipeline (#199) — never reclaim it.
        pipeline_lease.acquire_pipeline_lease(
            self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_DRIVE,
        )
        self.orch.repo.find_pr_for_issue.return_value = 42
        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="merged",
        ):
            self.orch._reclaim_completed_pipelines()
        self.git.remove_worktree.assert_not_called()
        self.assertIn("issue-7", self.orch._pipeline_sessions)

    def test_failed_removal_retains_session_for_retry(self) -> None:
        # Worktree removal is best-effort; if it fails the session must stay
        # live so a later tick retries rather than leaking the worktree.
        self._seed_pipeline("issue-7")
        ps = self.orch._pipeline_sessions["issue-7"]
        self.orch.repo.find_pr_for_issue.return_value = 42
        self.git.remove_worktree.side_effect = RuntimeError("stale worktree lock")

        with patch(
            "loony_dev.github.PullRequest.terminal_state", return_value="merged",
        ):
            self.orch._reclaim_completed_pipelines()
            # Removal raised → session retained, still live, for a later retry.
            self.assertIn("issue-7", self.orch._pipeline_sessions)
            self.assertTrue(ps.live)

            # A later tick retries once removal succeeds, and evicts.
            self.git.remove_worktree.side_effect = None
            self.orch._reclaim_completed_pipelines()
        self.assertNotIn("issue-7", self.orch._pipeline_sessions)


class TestPipelineLeaseExclusion(unittest.TestCase):
    """A human drive and a bot task must never co-run on one pipeline (#199)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        self.orch = Orchestrator(
            repo=_make_repo(), git=self.git, agents=[self.agent],
            interval=60, base_dir=self.tmp,
        )
        self.git.reset_mock()

    def test_claimed_keys_unions_active_drive_leases(self) -> None:
        from loony_dev import pipeline_lease
        pipeline_lease.acquire_pipeline_lease(
            self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_DRIVE,
        )
        self.assertIn("issue-7", self.orch._claimed_keys())

    def test_dispatch_skips_pipeline_a_drive_holds(self) -> None:
        from loony_dev import pipeline_lease
        pipeline_lease.acquire_pipeline_lease(
            self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_DRIVE,
        )
        task = MagicMock()
        task.worktree_key = "issue-7"
        task.target_branch = None
        task.task_type = "issue"
        # Force the gather to surface the task; the bot lease acquire must still
        # fail (the drive holds it), so on_start is never called.
        self.orch._find_work = MagicMock(return_value=[(task, self.agent)])
        self.orch._tick()
        task.on_start.assert_not_called()

    def test_bot_lease_released_lets_drive_acquire(self) -> None:
        from loony_dev import pipeline_lease
        # Bot holds the lease while a task runs…
        self.assertTrue(
            pipeline_lease.acquire_pipeline_lease(
                self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_BOT,
            )
        )
        # …so a drive cannot start.
        self.assertFalse(
            pipeline_lease.acquire_pipeline_lease(
                self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_DRIVE,
            )
        )
        # Once the bot releases (as _run_task's finally does), the drive can.
        pipeline_lease.release_pipeline_lease(
            self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_BOT,
        )
        self.assertTrue(
            pipeline_lease.acquire_pipeline_lease(
                self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_DRIVE,
            )
        )


class TestFencingStandDown(unittest.TestCase):
    """A reclaimed worker stands down mid-task without mutating shared state (#268)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        self.agent.name = "coding"
        self.orch = Orchestrator(
            repo=_make_repo(), git=self.git, agents=[self.agent],
            interval=60, base_dir=self.tmp,
        )
        self.git.reset_mock()

    def _make_task(self) -> MagicMock:
        task = MagicMock()
        task.worktree_key = "issue-7"
        task.target_branch = "feature/x"
        task.task_type = "implement_issue"
        task.describe.return_value = "do work"
        return task

    def _acquire_bot_lease(self) -> None:
        from loony_dev import pipeline_lease
        self.assertTrue(
            pipeline_lease.acquire_pipeline_lease(
                self.tmp, "owner/repo", "issue-7", holder=pipeline_lease.HOLDER_BOT,
            )
        )

    def _reclaim_lease(self) -> None:
        """Simulate another holder reclaiming the lease (new acquisition)."""
        from loony_dev import pipeline_lease
        path = pipeline_lease.lease_path(self.tmp, "owner/repo", "issue-7")
        path.write_text(
            '{"holder": "bot", "pid": 999999, "pipeline_key": "issue-7", '
            '"started_at": 99999999.0}'
        )

    def _lease_exists(self) -> bool:
        from loony_dev import pipeline_lease
        return pipeline_lease.lease_path(self.tmp, "owner/repo", "issue-7").exists()

    def _snapshot_state(self):
        from loony_dev import execution_state
        snap = execution_state.read_snapshot(self.tmp, "owner/repo", "issue-7")
        return None if snap is None else snap.state

    def test_normal_return_while_reclaimed_stands_down(self) -> None:
        # Exit shape (c): the agent returns success, but the pipeline was
        # reclaimed mid-turn — on_complete must NOT run, the lease must NOT be
        # released (the new holder owns it), and the snapshot must NOT finalize.
        self._acquire_bot_lease()
        task = self._make_task()

        def _execute(*a, **kw):
            self._reclaim_lease()
            return TaskResult(success=True, output="", summary="done")

        self.agent.execute.side_effect = _execute
        self.orch._run_task(self.agent, task)

        task.on_complete.assert_not_called()
        task.on_failure.assert_not_called()
        self.assertTrue(self._lease_exists())  # not released (new holder's lease)
        self.assertEqual(self._snapshot_state(), "running")  # not finalized

    def test_reclaim_during_on_complete_skips_lease_release(self) -> None:
        # The reclaim lands *after* the in-try fence check, during on_complete —
        # the finally re-check must still catch it so the snapshot is not
        # finalized and the lease (the new holder's) is not released (#268 critical).
        self._acquire_bot_lease()
        task = self._make_task()
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="ok")
        task.on_complete.side_effect = lambda *a, **kw: self._reclaim_lease()

        self.orch._run_task(self.agent, task)

        task.on_complete.assert_called_once()  # it did run (in-try fence passed)
        self.assertTrue(self._lease_exists())  # not released (new holder's lease)
        self.assertEqual(self._snapshot_state(), "running")  # not finalized to idle

    def test_agent_raises_fenced_error_stands_down(self) -> None:
        # Exit shape (a): the agent's own turn loop detected the reclaim and
        # raised LeaseFencedError — neither callback runs.
        from loony_dev import pipeline_lease
        self._acquire_bot_lease()
        task = self._make_task()

        def _execute(*a, **kw):
            self._reclaim_lease()
            raise pipeline_lease.LeaseFencedError("reclaimed mid-turn")

        self.agent.execute.side_effect = _execute
        self.orch._run_task(self.agent, task)

        task.on_complete.assert_not_called()
        task.on_failure.assert_not_called()
        self.assertTrue(self._lease_exists())

    def test_non_fence_exception_while_reclaimed_stands_down(self) -> None:
        # Exit shape (b): the agent raised an ordinary error while the lease was
        # reclaimed — the probe re-routes to stand-down, so on_failure is skipped.
        self._acquire_bot_lease()
        task = self._make_task()

        def _execute(*a, **kw):
            self._reclaim_lease()
            raise RuntimeError("boom")

        self.agent.execute.side_effect = _execute
        self.orch._run_task(self.agent, task)

        task.on_failure.assert_not_called()
        task.on_complete.assert_not_called()

    def test_non_fence_exception_without_reclaim_runs_on_failure(self) -> None:
        # Control: an ordinary error with the lease intact follows the normal
        # failure path (on_failure runs, lease released) — fencing is inert.
        self._acquire_bot_lease()
        task = self._make_task()
        self.agent.execute.side_effect = RuntimeError("boom")

        self.orch._run_task(self.agent, task)

        task.on_failure.assert_called_once()
        self.assertFalse(self._lease_exists())  # released normally

    def test_success_without_reclaim_runs_on_complete(self) -> None:
        # Control: a clean success with the lease intact still completes normally.
        self._acquire_bot_lease()
        task = self._make_task()
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="ok")

        self.orch._run_task(self.agent, task)

        task.on_complete.assert_called_once()
        self.assertFalse(self._lease_exists())  # released normally
        self.assertEqual(self._snapshot_state(), "idle")  # finalized


class TestStartupReconciliation(unittest.TestCase):
    """Startup flips crashed ``running`` snapshots to ``crashed`` (#268)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)

    def _write_running(self, key: str) -> None:
        from loony_dev import execution_state as es
        es.write_snapshot(
            self.tmp, "owner/repo", key,
            es.LiveState(
                pipeline_key=key, repo="owner/repo", stage="Implementing",
                current_skill="implement-issue", state="running", live=True,
            ),
        )

    def _state(self, key: str):
        from loony_dev import execution_state as es
        snap = es.read_snapshot(self.tmp, "owner/repo", key)
        return None if snap is None else snap

    def test_running_snapshot_without_lease_becomes_crashed(self) -> None:
        self._write_running("issue-7")  # no lease — writer is gone
        Orchestrator(
            repo=_make_repo(), git=self.git, agents=[], interval=60, base_dir=self.tmp,
        )
        snap = self._state("issue-7")
        self.assertEqual(snap.state, "crashed")
        self.assertFalse(snap.live)
        self.assertTrue(snap.needs_you)  # derives True for a crashed pipeline

    def test_running_snapshot_with_dead_pid_lease_becomes_crashed(self) -> None:
        from loony_dev import pipeline_lease
        self._write_running("issue-7")
        path = pipeline_lease.lease_path(self.tmp, "owner/repo", "issue-7")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"holder": "bot", "pid": 2147483646, "pipeline_key": "issue-7", '
            '"started_at": 1.0}'
        )
        Orchestrator(
            repo=_make_repo(), git=self.git, agents=[], interval=60, base_dir=self.tmp,
        )
        self.assertEqual(self._state("issue-7").state, "crashed")

    def test_running_snapshot_with_live_lease_is_left_alone(self) -> None:
        from loony_dev import pipeline_lease
        self._write_running("issue-9")
        # A live-pid lease (this process) means a genuinely active writer.
        self.assertTrue(
            pipeline_lease.acquire_pipeline_lease(
                self.tmp, "owner/repo", "issue-9", holder=pipeline_lease.HOLDER_BOT,
            )
        )
        Orchestrator(
            repo=_make_repo(), git=self.git, agents=[], interval=60, base_dir=self.tmp,
        )
        self.assertEqual(self._state("issue-9").state, "running")


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


class TestPipelineLogWiring(unittest.TestCase):
    """The per-pipeline log handler captures records under a task (issue #220)."""

    def setUp(self) -> None:
        import logging

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        self.agent.name = "coding"
        self.orch = _make_orchestrator(self.tmp, self.git, [self.agent])
        self.git.reset_mock()
        # The milestone records are INFO; the orchestrator logger inherits the
        # root level (WARNING by default in tests), so lower it for the test.
        root = logging.getLogger()
        self._prev_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, self._prev_level)
        self.handler = self.orch._install_pipeline_log_handler()
        self.addCleanup(self.handler.close)
        self.addCleanup(logging.getLogger().removeHandler, self.handler)

    def _make_task(self, *, worktree_key, target_branch=None) -> MagicMock:
        task = MagicMock()
        task.worktree_key = worktree_key
        task.target_branch = target_branch
        task.task_type = "implement"
        task.describe.return_value = "do work"
        return task

    def _pipeline_log_path(self, key: str) -> Path:
        from loony_dev import pipeline_log

        return pipeline_log.pipeline_log_path(self.tmp, "owner", "repo", key)

    def test_task_with_pipeline_writes_log_and_sidecar(self) -> None:
        from loony_dev import pipeline_log

        task = self._make_task(worktree_key="issue-7", target_branch="issue-7/slug")
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="done")

        self.orch.dispatch(self.agent, task)

        log_path = self._pipeline_log_path("issue-7")
        self.assertTrue(log_path.exists())
        contents = log_path.read_text()
        self.assertIn("starting implement phase", contents)
        self.assertIn("finished implement phase: success", contents)
        sidecar = pipeline_log.pipeline_key_sidecar_path(self.tmp, "owner", "repo", "issue-7")
        self.assertEqual(sidecar.read_text().strip(), "issue-7")

    def test_no_worktree_task_writes_no_pipeline_log(self) -> None:
        from loony_dev import pipeline_log

        task = self._make_task(worktree_key=None)
        self.agent.execute.return_value = TaskResult(success=True, output="", summary="ok")

        self.orch.dispatch(self.agent, task)

        pipelines_dir = pipeline_log.pipeline_logs_dir(self.tmp, "owner", "repo")
        self.assertFalse(pipelines_dir.exists())


class TestBaseDirThreading(unittest.TestCase):
    """An explicit base_dir wins over the git.work_dir fallback and reaches agents (#285)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Distinct trees: the checkout (git.work_dir) vs. the supervisor base_dir.
        self.checkout = self.tmp / "checkout"
        self.checkout.mkdir()
        self.base = self.tmp / "base"
        self.base.mkdir()

    def test_explicit_base_dir_threaded_to_agents(self) -> None:
        git = _make_git(self.checkout)
        agent = MagicMock()
        orch = Orchestrator(
            repo=_make_repo(), git=git, agents=[agent], interval=60, base_dir=self.base,
        )
        # The supervisor-threaded base_dir wins over the checkout fallback...
        self.assertEqual(orch.base_dir, self.base)
        # ...and is propagated to every agent for the observe registry / heartbeat.
        self.assertEqual(agent.base_dir, self.base)

    def test_falls_back_to_checkout_when_base_dir_unset(self) -> None:
        from loony_dev import config
        from loony_dev.config._settings import Settings

        git = _make_git(self.checkout)
        agent = MagicMock()
        # No configured base_dir (a bare standalone worker): keep today's behaviour.
        with patch.object(config, "settings", Settings({})):
            orch = Orchestrator(
                repo=_make_repo(), git=git, agents=[agent], interval=60,
            )
        self.assertEqual(orch.base_dir, self.checkout)
        self.assertEqual(agent.base_dir, self.checkout)


if __name__ == "__main__":
    unittest.main()
