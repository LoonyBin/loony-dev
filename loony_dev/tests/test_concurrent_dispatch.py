"""Tests for concurrent task dispatch with a thread pool (issue #128)."""
from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult
from loony_dev.orchestrator import Orchestrator

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.name = "owner/repo"
    repo.owner = "owner"
    return repo


def _make_git(tmp_path: Path) -> MagicMock:
    git = MagicMock()
    git.work_dir = tmp_path
    git.default_branch = "main"
    git.list_worktrees.return_value = []
    return git


def _make_orchestrator(tmp_path: Path, git: MagicMock, agents: list, *, max_concurrent: int) -> Orchestrator:
    orch = Orchestrator(
        repo=_make_repo(), git=git, agents=agents,
        interval=60, max_concurrent_tasks=max_concurrent,
    )
    git.reset_mock()  # discard startup-sweep calls
    return orch


def _make_task(
    *,
    worktree_key: str | None,
    target_branch: str | None = None,
    task_type: str = "t",
    priority: int = 10,
) -> MagicMock:
    task = MagicMock()
    task.worktree_key = worktree_key
    task.target_branch = target_branch
    task.task_type = task_type
    task.priority = priority
    task.describe.return_value = "do work"
    return task


def _drain(orch: Orchestrator, timeout: float = 5.0) -> None:
    """Block until no futures remain in flight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with orch._inflight_lock:
            if not orch._inflight:
                return
        time.sleep(0.01)
    raise AssertionError("in-flight tasks did not drain in time")


class TestConcurrentCompletion(unittest.TestCase):
    """The headline acceptance test: two tasks run in parallel, no GitHub collisions."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_two_distinct_tasks_complete_concurrently(self) -> None:
        git = _make_git(self.tmp)
        created: list[Path] = []
        removed: list[Path] = []
        git.create_worktree.side_effect = lambda branch, path, base: created.append(path)
        git.remove_worktree.side_effect = lambda path: removed.append(path)

        # A single shared agent (mirrors the real worker) whose execute blocks
        # on a barrier — both tasks must arrive for either to proceed, proving
        # they run truly concurrently rather than serially.
        barrier = threading.Barrier(2, timeout=5)
        agent = MagicMock()
        agent.can_handle.return_value = True

        def execute(task: object, work_dir: Path) -> TaskResult:
            barrier.wait()
            return TaskResult(success=True, output="", summary="done")

        agent.execute.side_effect = execute

        t1 = _make_task(worktree_key="issue-1", target_branch="feature/1")
        t2 = _make_task(worktree_key="issue-2", target_branch="feature/2")

        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=2)
        orch._find_work = lambda limit, claimed: [(t1, agent), (t2, agent)][:limit]

        orch._tick()
        _drain(orch)
        orch._pool.shutdown(wait=True)

        # Both worktrees created at distinct paths. They are retained for the
        # pipelines' next phases (issue #198) — not torn down per task — so the
        # just-run tick (still within the idle grace) removes neither.
        self.assertEqual(len(created), 2)
        self.assertEqual(len(set(created)), 2)
        self.assertEqual(removed, [])
        self.assertEqual(set(orch._pipeline_sessions), {"issue-1", "issue-2"})

        # Each task got exactly one lease and one success callback, no failures.
        for t in (t1, t2):
            t.on_start.assert_called_once()
            t.on_complete.assert_called_once()
            t.on_failure.assert_not_called()


class TestFindWorkOverlap(unittest.TestCase):
    """_find_work must gather only non-overlapping tasks, up to the limit."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.git = _make_git(self.tmp)
        self.agent = MagicMock()
        self.agent.can_handle.return_value = True
        self.orch = _make_orchestrator(self.tmp, self.git, [self.agent], max_concurrent=3)

    def test_shared_worktree_key_yields_one(self) -> None:
        # After key unification (#181) the dedupe identity is worktree-key-first,
        # and the invariant *same branch ⇒ same worktree_key* means two tasks that
        # share a branch also share a worktree_key. Two phases of one issue (e.g.
        # review + CI fix on the same PR) collapse to one identity → one dispatch.
        a = _make_task(worktree_key="issue-5", target_branch="issue-5/slug")
        b = _make_task(worktree_key="issue-5", target_branch="issue-5/slug")
        self.orch._gather_candidates = lambda: [a, b]
        batch = self.orch._find_work(limit=2, claimed=set())
        self.assertEqual(len(batch), 1)

    def test_distinct_branches_yield_both(self) -> None:
        a = _make_task(worktree_key="k1", target_branch="feature/x")
        b = _make_task(worktree_key="k2", target_branch="feature/y")
        self.orch._gather_candidates = lambda: [a, b]
        batch = self.orch._find_work(limit=2, claimed=set())
        self.assertEqual(len(batch), 2)

    def test_respects_claimed_keys(self) -> None:
        a = _make_task(worktree_key="k1", target_branch="feature/x")
        b = _make_task(worktree_key="k2", target_branch="feature/y")
        self.orch._gather_candidates = lambda: [a, b]
        batch = self.orch._find_work(limit=2, claimed={"k1"})
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0][0], b)

    def test_worktree_key_used_when_no_target_branch(self) -> None:
        # Two IssueTask-style tasks (target_branch None) sharing a worktree_key.
        a = _make_task(worktree_key="issue-5", target_branch=None)
        b = _make_task(worktree_key="issue-5", target_branch=None)
        self.orch._gather_candidates = lambda: [a, b]
        batch = self.orch._find_work(limit=2, claimed=set())
        self.assertEqual(len(batch), 1)

    def test_limit_caps_results(self) -> None:
        tasks = [_make_task(worktree_key=f"k{i}", target_branch=f"f{i}") for i in range(5)]
        self.orch._gather_candidates = lambda: tasks
        batch = self.orch._find_work(limit=2, claimed=set())
        self.assertEqual(len(batch), 2)

    def test_priority_order_across_pipelines(self) -> None:
        # Candidates arrive unsorted; _find_work must arbitrate by priority
        # (lowest number first), matching the old class-by-class scan order.
        low = _make_task(worktree_key="k-low", target_branch="f-low", priority=40)
        high = _make_task(worktree_key="k-high", target_branch="f-high", priority=5)
        self.orch._gather_candidates = lambda: [low, high]
        batch = self.orch._find_work(limit=1, claimed=set())
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0][0], high)


class TestFreeSlotAccounting(unittest.TestCase):
    """A saturated pool dispatches nothing; slots free up as futures complete."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_saturated_pool_submits_nothing(self) -> None:
        git = _make_git(self.tmp)
        release = threading.Event()
        agent = MagicMock()
        agent.can_handle.return_value = True
        agent.execute.side_effect = lambda task, work_dir: (
            release.wait(timeout=5),
            TaskResult(success=True, output="", summary="ok"),
        )[1]

        t1 = _make_task(worktree_key="k1", target_branch="f1")
        t2 = _make_task(worktree_key="k2", target_branch="f2")

        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=1)
        orch._find_work = lambda limit, claimed: [(t1, agent)][:limit]

        orch._tick()  # fills the only slot with t1
        self.assertEqual(orch._free_slots(), 0)

        # Second tick: pool saturated -> t2 never even gathered/leased.
        orch._find_work = lambda limit, claimed: [(t2, agent)][:limit]
        orch._tick()
        t2.on_start.assert_not_called()

        # Release t1; slot frees up; next tick dispatches t2.
        release.set()
        _drain(orch)
        self.assertEqual(orch._free_slots(), 1)

        orch._tick()
        _drain(orch)
        orch._pool.shutdown(wait=True)
        t2.on_start.assert_called_once()
        t2.on_complete.assert_called_once()


class TestGitLockSerialization(unittest.TestCase):
    """Base-checkout git mutations must never overlap across worker threads."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_base_checkout_ops_are_serialized(self) -> None:
        active = {"n": 0}
        violations: list[str] = []
        track_lock = threading.Lock()

        def guard(name: str):
            def _fn(*args: object, **kwargs: object):
                with track_lock:
                    active["n"] += 1
                    if active["n"] > 1:
                        violations.append(name)
                time.sleep(0.02)
                with track_lock:
                    active["n"] -= 1
                return MagicMock()
            return _fn

        git = _make_git(self.tmp)
        git.ensure_main_up_to_date.side_effect = guard("ensure_main_up_to_date")
        git.create_worktree.side_effect = guard("create_worktree")
        git.remove_worktree.side_effect = guard("remove_worktree")
        git.current_branch.side_effect = guard("current_branch")
        git.has_uncommitted_changes.side_effect = guard("has_uncommitted_changes")

        # Force both tasks into execute() simultaneously so the only thing that
        # can serialize the git ops is _git_lock (not accidental sequencing).
        barrier = threading.Barrier(2, timeout=5)
        agent = MagicMock()
        agent.can_handle.return_value = True
        agent.execute.side_effect = lambda task, work_dir: (
            barrier.wait(),
            TaskResult(success=True, output="", summary="ok"),
        )[1]

        t1 = _make_task(worktree_key="k1", target_branch="f1")
        t2 = _make_task(worktree_key="k2", target_branch="f2")

        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=2)
        orch._find_work = lambda limit, claimed: [(t1, agent), (t2, agent)][:limit]

        orch._tick()
        _drain(orch)
        orch._pool.shutdown(wait=True)

        self.assertEqual(violations, [], f"git ops overlapped: {violations}")


class TestShutdownDrain(unittest.TestCase):
    """SIGQUIT drains in-flight work; SIGTERM cancels/rolls back."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_graceful_shutdown_drains_without_rollback(self) -> None:
        git = _make_git(self.tmp)
        release = threading.Event()
        agent = MagicMock()
        agent.can_handle.return_value = True
        agent.execute.side_effect = lambda task, work_dir: (
            release.wait(timeout=5),
            TaskResult(success=True, output="", summary="ok"),
        )[1]

        t1 = _make_task(worktree_key="k1", target_branch="f1")
        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=1)
        orch._find_work = lambda limit, claimed: [(t1, agent)][:limit]
        orch._tick()

        orch._graceful_shutdown = True
        # Release the worker shortly after shutdown begins; pool.shutdown waits.
        threading.Timer(0.1, release.set).start()
        orch._on_shutdown()

        t1.on_complete.assert_called_once()
        t1.on_failure.assert_not_called()
        with orch._inflight_lock:
            self.assertEqual(orch._inflight, {})

    def test_immediate_shutdown_terminates_running_and_rolls_back_queued(self) -> None:
        git = _make_git(self.tmp)
        started = threading.Event()
        block = threading.Event()

        agent = MagicMock()
        agent.can_handle.return_value = True

        def execute(task: object, work_dir: Path) -> TaskResult:
            started.set()
            block.wait(timeout=5)
            # Interrupted: returns a non-success result -> worker calls on_failure.
            return TaskResult(success=False, output="", summary="interrupted")

        agent.execute.side_effect = execute
        # terminate() unblocks the running worker, mirroring killing the subprocess.
        agent.terminate.side_effect = lambda: block.set()

        running = _make_task(worktree_key=None, target_branch=None, task_type="running")
        queued = _make_task(worktree_key=None, target_branch=None, task_type="queued")

        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=1)

        # Submit the running task and wait until it is actually executing.
        f1 = orch._pool.submit(orch._run_task, agent, running)
        with orch._inflight_lock:
            orch._inflight[f1] = (running, agent)
        f1.add_done_callback(orch._task_done)
        self.assertTrue(started.wait(timeout=5))

        # Submit a second task; with a 1-slot pool it stays queued (cancellable).
        f2 = orch._pool.submit(orch._run_task, agent, queued)
        with orch._inflight_lock:
            orch._inflight[f2] = (queued, agent)
        f2.add_done_callback(orch._task_done)

        orch._graceful_shutdown = False
        orch._on_shutdown()

        # Queued task: cancelled before start -> orchestrator rolled it back once.
        queued.on_failure.assert_called_once()
        agent.terminate.assert_called()
        # Running task: worker owns its terminal callback -> exactly one on_failure.
        running.on_failure.assert_called_once()
        running.on_complete.assert_not_called()


class TestQuotaGlobalDisable(unittest.TestCase):
    """A quota hit on the shared agent disables it for the next gather."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)

    def test_disabled_agent_is_not_selected_by_find_work(self) -> None:
        from loony_dev.agents.coding import CodingAgent

        agent = CodingAgent()
        git = _make_git(self.tmp)
        orch = _make_orchestrator(self.tmp, git, [agent], max_concurrent=2)

        task = _make_task(worktree_key="issue-9", target_branch=None, task_type="implement_issue")
        orch._gather_candidates = lambda: [task]
        # Before quota: agent can handle implement_issue.
        self.assertEqual(len(orch._find_work(limit=2, claimed=set())), 1)

        # Quota hit disables the shared instance for everyone.
        agent._handle_quota_error("You've hit your limit · resets 7:30pm (Asia/Calcutta)")
        self.assertTrue(agent.is_disabled())
        self.assertEqual(orch._find_work(limit=2, claimed=set()), [])

        # Manually expire the cooldown -> agent eligible again.
        agent._disabled_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(len(orch._find_work(limit=2, claimed=set())), 1)


class _RealSleepAgent(Agent):
    """Concrete agent that spawns real sleep subprocesses for terminate() tests."""

    name = "real_sleep"

    def can_handle(self, task: Task) -> bool:  # pragma: no cover
        return False

    def execute(self, task: Task, work_dir: Path) -> TaskResult:  # pragma: no cover
        raise NotImplementedError

    def spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._register_process(proc)
        return proc


class TestTerminateMultipleProcesses(unittest.TestCase):
    """terminate() must kill every concurrently-spawned subprocess, not just one."""

    def test_terminate_kills_all_active_processes(self) -> None:
        agent = _RealSleepAgent()
        procs = [agent.spawn(), agent.spawn()]
        try:
            agent.terminate()
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if all(p.poll() is not None for p in procs):
                    break
                time.sleep(0.05)
            for p in procs:
                self.assertIsNotNone(p.poll(), "a subprocess survived terminate()")
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    unittest.main()
