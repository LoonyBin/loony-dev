from __future__ import annotations

import concurrent.futures
import logging
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from concurrent.futures import Future

from loony_dev.tasks.ci_failure_task import CIFailureTask
from loony_dev.tasks.conflict_task import ConflictResolutionTask
from loony_dev.tasks.issue_task import IssueTask
from loony_dev.tasks.planning_task import PlanningTask
from loony_dev.tasks.pr_review_task import PRReviewTask
from loony_dev.tasks.stuck_item_task import StuckItemCleanupTask

from loony_dev.models import RateLimitedError

if TYPE_CHECKING:
    from loony_dev.agents.base import Agent
    from loony_dev.git import GitRepo
    from loony_dev.github import Repo
    from loony_dev.tasks.base import Task

# Imported at runtime (not TYPE_CHECKING) so isinstance checks work.
from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.claude_session import trust_directory

logger = logging.getLogger(__name__)

# Task classes ordered by priority (lowest number = highest priority).
# The orchestrator iterates these in order, stopping as soon as it finds
# a task that some configured agent can handle.
TASK_CLASSES = sorted(
    [StuckItemCleanupTask, ConflictResolutionTask, CIFailureTask, PRReviewTask, PlanningTask, IssueTask],
    key=lambda tc: tc.priority,
)


class Orchestrator:
    def __init__(
        self,
        repo: Repo,
        git: GitRepo,
        agents: list[Agent],
        interval: int | None = None,
        max_concurrent_tasks: int | None = None,
    ) -> None:
        from loony_dev import config
        self.repo = repo
        self.git = git
        self.agents = agents
        self.interval = interval if interval is not None else config.settings.get("interval", 60)
        resolved_max = (
            max_concurrent_tasks
            if max_concurrent_tasks is not None
            else config.settings.get("max_concurrent_tasks", 3)
        )
        self.max_concurrent = max(1, int(resolved_max))
        self._shutdown_requested: bool = False
        self._graceful_shutdown: bool = False

        # Thread pool that runs the per-task worktree lifecycle + agent
        # execution concurrently. The tick loop itself stays single-threaded;
        # only dispatched work fans out here.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrent, thread_name_prefix="task",
        )
        # In-flight registry: maps each submitted Future to its (task, agent).
        self._inflight: dict[Future, tuple[Task, Agent]] = {}
        self._inflight_lock = threading.Lock()
        # Serializes git operations that mutate the shared base checkout
        # (index/refs/worktree list). Agent execution runs OUTSIDE this lock,
        # inside the isolated worktree, so real concurrency is preserved.
        self._git_lock = threading.Lock()

        repo_short_name = repo.name.split("/", 1)[1] if "/" in repo.name else repo.name
        self.worktree_root = git.work_dir / ".worktrees" / repo.owner / repo_short_name
        self._prune_stale_worktrees()

    def run(self) -> None:
        """Main polling loop."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGQUIT, self._handle_signal)

        logger.info("Orchestrator started. Polling every %ds.", self.interval)
        while not self._shutdown_requested:
            try:
                self._tick()
            except Exception:
                logger.exception("Error during tick")

            # Interruptible sleep: check shutdown flag each second.
            for _ in range(self.interval):
                if self._shutdown_requested:
                    break
                time.sleep(1)

        self._on_shutdown()

    def _handle_signal(self, signum: int, frame: object) -> None:
        # Signal handlers must stay minimal: only flip flags here. All locking,
        # git, and GitHub work happens later in _on_shutdown on the run-loop
        # thread, where it is safe.
        if signum == signal.SIGQUIT:
            logger.info("SIGQUIT received — will drain in-flight tasks then shut down.")
            self._shutdown_requested = True
            self._graceful_shutdown = True
        else:
            logger.info("Signal %s received, shutting down…", signum)
            self._shutdown_requested = True

    def _on_shutdown(self) -> None:
        """Drain (graceful) or cancel-and-roll-back (immediate) in-flight tasks."""
        logger.info("Shutting down.")
        try:
            if self._graceful_shutdown:
                # SIGQUIT: stop gathering new work (loop already exited) and let
                # in-flight workers finish; each calls its own terminal callback.
                logger.info("Draining in-flight tasks before shutdown.")
            else:
                # SIGINT/SIGTERM: cancel or interrupt every in-flight task.
                with self._inflight_lock:
                    snapshot = list(self._inflight.items())
                for future, (task, agent) in snapshot:
                    if future.cancel():
                        # Never started: the lease (on_start) is dangling, so the
                        # orchestrator rolls back GitHub state on the task's behalf.
                        logger.info(
                            "Rolling back queued task interrupted before start: %s",
                            task.task_type,
                        )
                        try:
                            task.on_failure(
                                self.repo, RuntimeError("Interrupted by operator"),
                            )
                        except Exception:
                            logger.exception(
                                "Failed to roll back GitHub state for %s", task.task_type,
                            )
                    else:
                        # Running/finished: the worker owns the terminal callback.
                        # Interrupt the Claude subprocess so execute() returns
                        # non-zero and the worker's own on_failure runs.
                        logger.info(
                            "Terminating running task on shutdown: %s", task.task_type,
                        )
                        agent.terminate()
        finally:
            # Always join the pool, even if a rollback above raised.
            self._pool.shutdown(wait=True)

    def _free_slots(self) -> int:
        """Number of pool slots not currently occupied by in-flight tasks."""
        with self._inflight_lock:
            return self.max_concurrent - len(self._inflight)

    def _claimed_keys(self) -> set[str]:
        """Dedupe identities of in-flight tasks, so the gather avoids overlap."""
        with self._inflight_lock:
            inflight = list(self._inflight.values())
        claimed: set[str] = set()
        for task, _agent in inflight:
            claimed.add(self._task_identity(task))
        return claimed

    @staticmethod
    def _task_identity(task: Task) -> str:
        """A task's dedupe identity: target_branch when set, else worktree_key.

        Two tasks sharing this identity contend for the same branch/worktree and
        must never run concurrently (git refuses the same branch in two
        worktrees). ``id(task)`` is the last-resort fallback for tasks with
        neither, which never overlap anyway.
        """
        return task.target_branch or task.worktree_key or f"task-{id(task)}"

    def _tick(self) -> None:
        self.repo.clear_tick_cache()
        self.repo.evict_stale_permission_cache()
        self.repo.evict_stale_check_runs_cache()

        slots = self._free_slots()
        if slots <= 0:
            logger.debug("Pool saturated (%d in flight) — skipping gather.", self.max_concurrent)
            return

        batch = self._find_work(limit=slots, claimed=self._claimed_keys())
        if not batch:
            logger.debug("No tasks found.")
            return

        for task, agent in batch:
            logger.info("Dispatching task: %s", task.task_type)
            try:
                # The lease: mutate GitHub state synchronously in the tick thread
                # so the next gather/discover no longer sees this task. This is
                # the single-threaded mutation point concurrency safety relies on.
                task.on_start(self.repo)
            except Exception:
                logger.exception("on_start failed for %s — skipping dispatch", task.task_type)
                continue

            future = self._pool.submit(self._run_task, agent, task)
            with self._inflight_lock:
                self._inflight[future] = (task, agent)
            future.add_done_callback(self._task_done)

    def _task_done(self, future: Future) -> None:
        """Deregister a finished future and surface any unexpected error."""
        with self._inflight_lock:
            self._inflight.pop(future, None)
        try:
            exc = future.exception()
        except concurrent.futures.CancelledError:
            return
        if exc is not None:
            logger.error("Task worker raised unexpectedly: %s", exc, exc_info=exc)

    def _find_work(self, limit: int, claimed: set[str]) -> list[tuple[Task, Agent]]:
        """Gather up to *limit* non-overlapping (task, agent) pairs by priority.

        Tasks whose dedupe identity (see ``_task_identity``) is already in
        *claimed* — either in flight or selected earlier this gather — are
        skipped, so no two dispatched tasks contend for the same branch/worktree.
        """
        seen = set(claimed)
        results: list[tuple[Task, Agent]] = []
        for task_class in TASK_CLASSES:
            logger.debug("Checking %s for work...", task_class.__name__)
            found_in_class = 0
            for task in task_class.discover(self.repo):
                found_in_class += 1
                identity = self._task_identity(task)
                if identity in seen:
                    logger.debug(
                        "Task '%s' (id=%s) overlaps in-flight/selected work — skipping",
                        task.task_type, identity,
                    )
                    continue
                for agent in self.agents:
                    if agent.can_handle(task):
                        logger.debug(
                            "Selected agent '%s' for task '%s' (type=%s)",
                            agent.name, task, task.task_type,
                        )
                        seen.add(identity)
                        results.append((task, agent))
                        break
                else:
                    logger.debug(
                        "No agent can handle task type '%s' — skipping", task.task_type,
                    )
                if len(results) >= limit:
                    return results
            logger.debug("%s yielded %d candidate(s)", task_class.__name__, found_in_class)
        return results

    def dispatch(self, agent: Agent, task: Task) -> None:
        """Synchronous lease + run, kept as a thin wrapper for tests/callers."""
        task.on_start(self.repo)
        self._run_task(agent, task)

    def _run_task(self, agent: Agent, task: Task) -> None:
        """Worker body: prepare worktree, run the agent, finalize. Never raises.

        Runs in a pool thread. ``on_start`` (the lease) has already happened in
        the tick thread. Base-checkout git mutations are serialized by
        ``_git_lock``; agent execution runs outside it for real concurrency.
        """
        logger.debug("Task description:\n%s", task.describe())
        worktree_path: Path | None = None
        try:
            # ── Prepare worktree (mutates the shared base checkout) ──────────
            # All of this touches the base checkout's index/refs/worktree list
            # and is NOT safe to run concurrently, so it is serialized.
            with self._git_lock:
                try:
                    logger.debug("Current branch before sync: %s", self.git.current_branch())
                    logger.debug("Uncommitted changes before sync: %s", self.git.has_uncommitted_changes())
                except Exception:
                    logger.debug("Could not read git state before sync", exc_info=True)
                # Sync the base checkout so the ref the worktree forks from is current.
                self.git.ensure_main_up_to_date()

                target = task.target_branch
                if target:
                    logger.info("Resetting branch %r to upstream state before task.", target)
                    self.git.reset_branch_to_upstream(target)

                # Each task runs in its own worktree so stray state never lands
                # in the base checkout. Tasks with no worktree_key (e.g. cleanup)
                # run against the base checkout directly.
                work_dir = self.git.work_dir
                key = task.worktree_key
                if key is not None:
                    worktree_path = self.worktree_root / key
                    worktree_path.parent.mkdir(parents=True, exist_ok=True)
                    branch, base = self._worktree_branch_and_base(task, key, target)
                    logger.info(
                        "Creating worktree for task at %s (branch=%s, base=%s)",
                        worktree_path, branch, base,
                    )
                    self.git.create_worktree(branch=branch, path=worktree_path, base=base)
                    # Pre-trust the fresh worktree so the interactive ClaudeSession
                    # does not block on claude's folder-trust dialog (which
                    # --dangerously-skip-permissions does not bypass). New worktree
                    # paths are always untrusted; without this, session startup
                    # hangs and every task times out. See #178.
                    trust_directory(worktree_path)
                    work_dir = worktree_path

            # ── Execute (concurrent — runs inside the isolated worktree) ─────
            if isinstance(task, IssueTask) and isinstance(agent, CodingAgent):
                result = agent.execute_issue(task, work_dir=work_dir)
            else:
                result = agent.execute(task, work_dir=work_dir)
            if result.success:
                task.on_complete(self.repo, result)
            elif result.rate_limited:
                task.on_failure(self.repo, RateLimitedError(result.summary))
            else:
                task.on_failure(self.repo, RuntimeError(result.summary))
        except Exception as e:
            task.on_failure(self.repo, e)
        finally:
            # Worktree removal mutates the base checkout's worktree list too.
            with self._git_lock:
                self._remove_worktree(worktree_path)

    def _worktree_branch_and_base(
        self, task: Task, key: str, target: str | None,
    ) -> tuple[str, str | None]:
        """Decide the branch and start-ref for a task's worktree.

        A worktree maps 1:1 to a branch, and the base checkout is pinned to the
        default branch, so no worktree may reuse the default branch directly.

        - PR tasks operate on an existing branch (``target``); fork from it.
        - IssueTask creates its feature branch from the default branch.
        - Everything else (e.g. PlanningTask, which only reads code) forks a
          throwaway branch named after ``key`` from the default branch. It is
          never pushed and is discarded with the worktree.
        """
        if target:
            return target, None
        if isinstance(task, IssueTask):
            return task.branch_name, self.git.default_branch
        return key, self.git.default_branch

    def _remove_worktree(self, path: Path | None) -> None:
        """Remove the per-task worktree at *path*, if any. Never raises."""
        if path is None:
            return
        try:
            self.git.remove_worktree(path)
        except Exception:
            logger.exception("Failed to remove worktree at %s", path)

    def _prune_stale_worktrees(self) -> None:
        """Remove worktrees left over from crashed or killed prior runs.

        Runs at startup before the polling loop, when no tasks are active, so
        any worktree under ``worktree_root`` is by definition orphaned.
        """
        try:
            self.git._run("worktree", "prune")
        except Exception:
            logger.debug("git worktree prune failed during startup sweep", exc_info=True)

        root = self.worktree_root.resolve()
        try:
            for info in self.git.list_worktrees():
                if info.bare:
                    continue
                try:
                    under_root = info.path.resolve().is_relative_to(root)
                except (OSError, ValueError):
                    continue
                if under_root:
                    logger.info("Pruning stale worktree at %s", info.path)
                    self._remove_worktree(info.path)
        except Exception:
            logger.exception("Failed to enumerate worktrees during startup sweep")

        # Nuke any empty directories left behind once git metadata is pruned.
        shutil.rmtree(self.worktree_root, ignore_errors=True)
