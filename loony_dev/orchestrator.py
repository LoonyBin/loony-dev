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
from loony_dev.commands import install_commands
from loony_dev import pipeline_lease, session_registry
from loony_dev.pipeline_session import PipelineSession
from loony_dev.session import session_id_for

logger = logging.getLogger(__name__)

# Task classes ordered by priority (lowest number = highest priority).
# Since #197 the orchestrator sources work from pipelines (one ``next_task`` per
# pipeline, see ``loony_dev/pipeline.py``) rather than scanning these directly;
# this registry is retained as the canonical priority list and a test seam for
# the per-class ``discover()`` paths.
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
        base_dir: Path | None = None,
    ) -> None:
        from loony_dev import config
        self.repo = repo
        self.git = git
        self.agents = agents
        # Where the per-pipeline session registry and lease files live (shared
        # with the web process, issue #199). Defaults to the configured base dir,
        # falling back to the checkout root when unset (e.g. tests that build no
        # full config) so registry/lease state stays under the repo tree.
        if base_dir is not None:
            self.base_dir = Path(base_dir)
        else:
            configured = config.settings.get("base_dir")
            self.base_dir = Path(configured).resolve() if configured else Path(git.work_dir)
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

        # Long-lived owner objects for active pipelines (issue #198), keyed by
        # pipeline key (``issue-N`` / ``pr-P``). Each owns a reusable worktree +
        # session id that consecutive phases share. This mirrors ``_inflight``:
        # an in-memory cache whose durable truth stays GitHub + the on-disk
        # worktree/session/registry, so a crash empties it and the next tick
        # rebuilds it lazily. Created on first task, released once the pipeline
        # reaches a terminal GitHub state.
        self._pipeline_sessions: dict[str, PipelineSession] = {}
        self._pipeline_sessions_lock = threading.Lock()

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
                        # _run_task never ran, so release the pipeline lease taken
                        # at dispatch here instead of in its finally (issue #199).
                        pkey = task.worktree_key
                        if pkey is not None:
                            pipeline_lease.release_pipeline_lease(
                                self.base_dir, self.repo.name, pkey,
                                holder=pipeline_lease.HOLDER_BOT,
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
            # Release every retained pipeline worktree (issue #198) so a clean
            # shutdown leaves no live worktrees behind. Best-effort: the startup
            # prune reclaims any that survive a crash instead.
            self._release_all_pipeline_worktrees()

    def _release_all_pipeline_worktrees(self) -> None:
        """Remove all retained pipeline worktrees. Never raises (shutdown path)."""
        with self._pipeline_sessions_lock:
            sessions = list(self._pipeline_sessions.values())
            self._pipeline_sessions.clear()
        for ps in sessions:
            if not ps.live:
                continue
            with self._git_lock:
                self._remove_worktree(ps.worktree_path)
            ps.live = False

    def _free_slots(self) -> int:
        """Number of pool slots not currently occupied by in-flight tasks."""
        with self._inflight_lock:
            return self.max_concurrent - len(self._inflight)

    def _claimed_keys(self) -> set[str]:
        """Dedupe identities of in-flight tasks, so the gather avoids overlap.

        Unions the in-memory in-flight identities with pipeline keys currently
        held by a human **drive** session (issue #199). A drive runs in the web
        process, invisible to ``_inflight``, so its on-disk lease is the only
        cross-process signal that keeps the scheduler from dispatching an
        automated task onto a pipeline a human is interrogating.
        """
        with self._inflight_lock:
            inflight = list(self._inflight.values())
        claimed: set[str] = set()
        for task, _agent in inflight:
            claimed.add(self._task_identity(task))
        try:
            claimed |= pipeline_lease.active_drive_pipeline_keys(self.base_dir, self.repo.name)
        except Exception:
            logger.debug("Could not read drive pipeline leases", exc_info=True)
        return claimed

    @staticmethod
    def _task_identity(task: Task) -> str:
        """A task's dedupe identity: worktree_key when set, else target_branch.

        Two tasks sharing this identity contend for the same branch/worktree and
        must never run concurrently (git refuses the same branch in two
        worktrees). ``worktree_key`` is primary because key unification (#181)
        holds the invariant *same branch ⇒ same worktree_key*: every phase of an
        issue (plan, implement, review, CI fix, conflict) shares ``issue-N``, and
        external PRs share ``pr-P``. Keying on the worktree therefore collapses
        all same-issue tasks to one identity so the ``issue-N`` path can never be
        double-checked-out. ``target_branch`` covers no-worktree tasks that still
        pin a branch, and ``id(task)`` is the last-resort fallback for tasks with
        neither, which never overlap anyway.
        """
        return task.worktree_key or task.target_branch or f"task-{id(task)}"

    def _tick(self) -> None:
        self.repo.clear_tick_cache()
        self.repo.evict_stale_permission_cache()
        self.repo.evict_stale_check_runs_cache()

        try:
            self._dispatch_tick()
        finally:
            # Reclaim worktrees of pipelines that have reached a terminal GitHub
            # state — PR merged/closed, or issue closed with no PR (issue #198).
            # Runs every tick (including the early-return paths above). A live
            # pipeline's worktree is kept for the whole cycle so the operator can
            # ``cd`` in and inspect it at any point between phases.
            self._reclaim_completed_pipelines()

    def _dispatch_tick(self) -> None:
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
            # Take the cross-process pipeline lease *before* mutating GitHub, so a
            # human drive that raced in since the gather (issue #199) refuses the
            # bot cleanly without leaving a dangling in-progress label. Released in
            # ``_run_task``'s finally. Tasks with no pipeline (no worktree_key)
            # need no lease — nothing can interrogate them.
            pkey = task.worktree_key
            if pkey is not None and not pipeline_lease.acquire_pipeline_lease(
                self.base_dir, self.repo.name, pkey, holder=pipeline_lease.HOLDER_BOT,
            ):
                logger.info(
                    "Pipeline %s is held by an active drive session — skipping dispatch", pkey,
                )
                continue
            try:
                # The lease: mutate GitHub state synchronously in the tick thread
                # so the next gather/discover no longer sees this task. This is
                # the single-threaded mutation point concurrency safety relies on.
                task.on_start(self.repo)
            except Exception:
                logger.exception("on_start failed for %s — skipping dispatch", task.task_type)
                if pkey is not None:
                    pipeline_lease.release_pipeline_lease(
                        self.base_dir, self.repo.name, pkey, holder=pipeline_lease.HOLDER_BOT,
                    )
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

    def _gather_candidates(self) -> list[Task]:
        """Source of work: one task per pipeline via ``Pipeline.next_task`` (#197).

        Enumerates pipelines once (issue + PR facets grouped by branch key) and
        collects each pipeline's single highest-priority actionable task. This
        replaces the six independent ``Task.discover()`` scans; the scheduler in
        ``_find_work`` — priority arbitration, the ``_free_slots`` cap, and the
        ``_task_identity`` in-flight dedupe — is unchanged.
        """
        from loony_dev.pipeline import Pipeline

        candidates: list[Task] = []
        for pipeline in Pipeline.discover(self.repo):
            task = pipeline.next_task(self.repo)
            if task is not None:
                logger.debug(
                    "Pipeline %s -> task '%s'", pipeline.pipeline_key, task.task_type,
                )
                candidates.append(task)
        return candidates

    def _find_work(self, limit: int, claimed: set[str]) -> list[tuple[Task, Agent]]:
        """Gather up to *limit* non-overlapping (task, agent) pairs by priority.

        Candidates come from ``_gather_candidates`` (one per pipeline). They are
        arbitrated in the global priority order (lowest ``priority`` number
        first); tasks whose dedupe identity (see ``_task_identity``) is already
        in *claimed* — either in flight or selected earlier this gather — are
        skipped, so no two dispatched tasks contend for the same branch/worktree.
        """
        candidates = self._gather_candidates()
        # Stable sort by priority preserves pipeline enumeration order within a
        # priority tier, so the global ordering matches the old class-by-class
        # scan (which emitted every priority-5 task before any priority-10, …).
        candidates.sort(key=lambda t: t.priority)

        seen = set(claimed)
        results: list[tuple[Task, Agent]] = []
        for task in candidates:
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
        # The pipeline's long-lived owner (issue #198): created lazily on the
        # first task for this key, reused by every later phase. ``None`` for
        # tasks with no worktree (e.g. cleanup), which run against the base
        # checkout exactly as before.
        ps = self._pipeline_session_for(task)
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
                if ps is not None:
                    # Lazy create-or-reuse: the first phase materializes the
                    # worktree; later phases reuse it, syncing in place instead.
                    work_dir = self._ensure_pipeline_worktree(ps, task, target)
                else:
                    # A no-worktree task that still pins a branch refreshes that
                    # ref from the base checkout, just as before.
                    if target:
                        logger.info("Resetting branch %r to upstream state before task.", target)
                        self.git.reset_branch_to_upstream(target)
                    work_dir = self.git.work_dir

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
            # Release the pipeline lease taken at dispatch (issue #199) so the
            # next gather — and any human drive — can claim the pipeline again.
            pkey = task.worktree_key
            if pkey is not None:
                pipeline_lease.release_pipeline_lease(
                    self.base_dir, self.repo.name, pkey, holder=pipeline_lease.HOLDER_BOT,
                )
            # The worktree is NOT torn down here anymore (issue #198): it is
            # retained for the pipeline's next phase and reclaimed only once the
            # pipeline reaches a terminal GitHub state (see
            # ``_reclaim_completed_pipelines``).

    def _pipeline_session_for(self, task: Task) -> PipelineSession | None:
        """Get-or-create the :class:`PipelineSession` owning *task*'s worktree.

        Returns ``None`` for tasks with no ``worktree_key`` (they run against the
        base checkout). The map is the in-memory owner registry; a missing entry
        is built lazily here — never during pipeline discovery — so an idle
        pipeline holds no live resources (issue #198).
        """
        key = task.worktree_key
        if key is None:
            return None
        with self._pipeline_sessions_lock:
            ps = self._pipeline_sessions.get(key)
            if ps is None:
                ps = PipelineSession.for_task(
                    task,
                    worktree_root=self.worktree_root,
                    repo_name=self.repo.name,
                    default_branch=self.git.default_branch,
                )
                self._pipeline_sessions[key] = ps
            return ps

    def _ensure_pipeline_worktree(
        self, ps: PipelineSession, task: Task, target: str | None,
    ) -> Path:
        """Materialize *ps*'s worktree on first use, or sync it on reuse.

        Caller holds ``_git_lock`` (this mutates the base checkout's worktree
        list / refs). First creation does the one-time setup — folder-trust,
        session-registry record, slash-command install — that later phases skip.
        """
        if not ps.live:
            if target:
                logger.info("Resetting branch %r to upstream state before task.", target)
                self.git.reset_branch_to_upstream(target)
            ps.worktree_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Creating worktree for pipeline %s at %s (branch=%s, base=%s)",
                ps.pipeline_key, ps.worktree_path, ps.branch, ps.base,
            )
            self.git.create_worktree(branch=ps.branch, path=ps.worktree_path, base=ps.base)
            # Pre-trust the fresh worktree so the interactive ClaudeSession does
            # not block on claude's folder-trust dialog (which
            # --dangerously-skip-permissions does not bypass). New worktree paths
            # are always untrusted; without this, session startup hangs and every
            # task times out. See #178.
            trust_directory(ps.worktree_path)
            # Record (session_id → worktree_path) so a parked pipeline can be
            # resumed on demand into a fresh PTY (issue #199). This is the exact
            # cwd the upcoming turn writes its transcript to, so a later resume
            # lands in the same cwd and the JSONL is found (guards the #177
            # cross-worktree class). Real turns run via ``claude -p``, not the
            # bridge, so it is recorded here rather than relying on
            # ``publish_session`` firing.
            self._record_pipeline_session(task, ps.pipeline_key, ps.worktree_path, ps.branch)
            # Install the bundled slash commands into the worktree's
            # .claude/commands/ so agent turns can invoke them (#166). Each
            # worktree is a separate working tree, so the base checkout's commands
            # (installed at startup) are not visible here — install them per
            # worktree. Exclude them locally so `git add -A` never sweeps the
            # generated files into the PR.
            try:
                install_commands(ps.worktree_path)
                self.git.add_local_exclude(ps.worktree_path, ".claude/commands/")
            except OSError as exc:
                logger.warning(
                    "Failed to install slash commands into worktree %s: %s",
                    ps.worktree_path, exc,
                )
            ps.live = True
        elif target:
            # Reuse: the persistent worktree holds the branch checked out, so
            # ``reset_branch_to_upstream``'s ``git branch -f`` would refuse it —
            # sync from inside the worktree instead (issue #198).
            logger.info(
                "Syncing reused worktree %s to upstream %s before task.",
                ps.worktree_path, target,
            )
            self.git.sync_worktree_to_upstream(ps.worktree_path, target)
        return ps.worktree_path

    def _reclaim_completed_pipelines(self) -> None:
        """Release worktrees of pipelines that reached a terminal GitHub state (#198).

        A pipeline's worktree is retained for the whole issue/PR cycle so the
        operator can ``cd`` in and inspect it at any point between phases — there
        is deliberately no idle grace. It is reclaimed only once the work is
        truly done:

        - its PR is merged, or
        - its PR is closed without merging, or
        - it has no PR and its originating issue is closed.

        A pipeline that is **in-flight** or held by a human **drive** lease
        (issue #199) is always kept, even if its GitHub state already looks
        terminal — in-flight wins for safety. Reclamation removes the live
        worktree only; the on-disk session transcript and registry entry are
        retained. Never raises (best-effort housekeeping).
        """
        if not self._pipeline_sessions:
            return
        claimed = self._claimed_keys()
        with self._pipeline_sessions_lock:
            items = list(self._pipeline_sessions.items())
        for key, ps in items:
            if key in claimed:
                continue  # in-flight or held by a human drive — leave it live
            try:
                done = self._pipeline_is_complete(key)
            except Exception:
                logger.debug("Could not resolve terminal state for pipeline %s", key, exc_info=True)
                continue
            if not done:
                continue
            logger.info("Reclaiming completed pipeline %s — releasing worktree %s", key, ps.worktree_path)
            with self._git_lock:
                removed = self._remove_worktree(ps.worktree_path)
            if not removed:
                # Worktree removal is best-effort and failed (e.g. a stale lock);
                # keep the session live so a later tick retries rather than
                # silently leaking the worktree and dropping our handle to it.
                logger.warning(
                    "Worktree removal failed for pipeline %s — retaining session for retry", key,
                )
                continue
            ps.live = False
            with self._pipeline_sessions_lock:
                self._pipeline_sessions.pop(key, None)

    def _pipeline_is_complete(self, pipeline_key: str) -> bool:
        """True if *pipeline_key*'s issue/PR has reached a terminal GitHub state.

        Resolves the originating work from the key (``issue-N`` → issue ``N``;
        ``pr-P`` → PR ``P``) and queries its state directly — a completed
        pipeline no longer appears in the open-issue/open-PR discovery scan, so
        its terminal state must be fetched on demand here.
        """
        from loony_dev.github import Issue, PullRequest

        if pipeline_key.startswith("pr-"):
            pr_number = int(pipeline_key[len("pr-"):])
            state = PullRequest.terminal_state(pr_number, repo=self.repo)
            return state in ("merged", "closed")

        if pipeline_key.startswith("issue-"):
            issue_number = int(pipeline_key[len("issue-"):])
            pr_number = self.repo.find_pr_for_issue(issue_number)
            if pr_number is not None:
                state = PullRequest.terminal_state(pr_number, repo=self.repo)
                # While the PR is still open, the pipeline is in flight regardless
                # of the issue's own state.
                return state in ("merged", "closed")
            return Issue.is_closed(issue_number, repo=self.repo)

        # Unrecognised key shape — never auto-reclaim it.
        return False

    def _record_pipeline_session(
        self, task: Task, key: str, worktree_path: Path, branch: str,
    ) -> None:
        """Record this pipeline's ``(session_id → worktree_path)`` mapping.

        Best-effort: a registry write failure must never abort the task. The
        session id is the deterministic id the agent uses for ``--resume``
        continuity (``session_id_for(repo, session_key)``); ``key`` doubles as the
        pipeline key (``issue-N``) and the task key the registry is filed under.
        """
        session_key = task.session_key
        if session_key is None:
            return
        try:
            session_registry.record_session_worktree(
                self.base_dir,
                self.repo.name,
                pipeline_key=key,
                task_key=key,
                session_id=session_id_for(self.repo.name, session_key),
                worktree_path=str(worktree_path),
                branch=branch,
            )
        except Exception:
            logger.debug("Could not record pipeline session for %s", key, exc_info=True)

    def _remove_worktree(self, path: Path | None) -> bool:
        """Remove the per-task worktree at *path*, if any. Never raises.

        Returns ``True`` when the worktree is gone (removed, or there was
        nothing to remove) and ``False`` when removal genuinely failed, so the
        reclaim pass can keep the session live and retry instead of leaking the
        worktree.
        """
        if path is None:
            return True
        try:
            self.git.remove_worktree(path)
            return True
        except Exception:
            logger.exception("Failed to remove worktree at %s", path)
            return False

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
