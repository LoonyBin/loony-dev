"""Per-pipeline session manager — a reusable worktree + session id (issue #198).

A :class:`PipelineSession` is the long-lived owner object the orchestrator keeps
for each *active* pipeline (``issue-N`` / ``pr-P``). Consecutive tasks on one
pipeline (implement → CI fix → review → conflict) **reuse** its single git
worktree and deterministic Claude session id instead of paying a per-task
create/trust/install/teardown cycle. When a pipeline goes idle for a grace
period the orchestrator *hibernates* it — the live worktree is released while the
on-disk session transcript and registry entry are retained, so a later phase (or
on-demand interrogation, #199) can resume cheaply at the canonical path.

It is a plain state object — **not** a process. There is no PTY here; agent turns
still run via ``claude -p``. The orchestrator owns the git mutations (serialized
by its ``_git_lock``); this class only tracks *what* the pipeline's worktree and
session are and *whether* the worktree is currently live.

``PipelineSession`` is in-memory only. Durable truth stays GitHub + the on-disk
worktree/session/registry, so a crash simply empties the orchestrator's map and
the next tick rebuilds it lazily.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.tasks.base import Task


@dataclass
class PipelineSession:
    """Live-resource bookkeeping for one active pipeline.

    ``branch``/``base`` pin how the worktree is first created; ``session_id`` is
    the deterministic id agent turns ``--resume``. ``live`` is ``True`` while the
    worktree exists on disk, and ``last_active`` is a :func:`time.monotonic`
    stamp the hibernation sweep measures its idle grace against.
    """

    pipeline_key: str
    branch: str
    base: str | None
    worktree_path: Path
    session_id: str | None = None
    session_key: str | None = None
    live: bool = False
    last_active: float = 0.0

    @classmethod
    def for_task(
        cls,
        task: "Task",
        *,
        worktree_root: Path,
        repo_name: str,
        default_branch: str,
    ) -> "PipelineSession":
        """Derive a pipeline session's identity from *task*.

        Mirrors the orchestrator's worktree branch/base rules (a worktree maps
        1:1 to a branch and the base checkout is pinned to the default branch, so
        no worktree may reuse the default branch directly):

        - PR tasks operate on an existing branch (``target_branch``); fork from
          it.
        - Planning and implementation both own the issue's feature branch and
          create it from the default branch — sharing the ``issue-N`` worktree
          (#181).
        - Anything else forks a throwaway branch named after the worktree key.
        """
        from loony_dev.session import session_id_for
        from loony_dev.tasks.issue_task import IssueTask
        from loony_dev.tasks.planning_task import PlanningTask

        key = task.worktree_key
        if key is None:
            raise ValueError("task has no worktree_key; cannot own a pipeline session")

        target = task.target_branch
        if target:
            branch, base = target, None
        elif isinstance(task, (PlanningTask, IssueTask)):
            branch, base = task.branch_name, default_branch
        else:
            branch, base = key, default_branch

        session_key = task.session_key
        session_id = session_id_for(repo_name, session_key) if session_key else None
        return cls(
            pipeline_key=key,
            branch=branch,
            base=base,
            worktree_path=worktree_root / key,
            session_id=session_id,
            session_key=session_key,
        )

    def mark_active(self, now: float | None = None) -> None:
        """Stamp the pipeline as just-active (defaults to ``time.monotonic()``)."""
        self.last_active = time.monotonic() if now is None else now

    def is_idle(self, now: float, grace: float) -> bool:
        """True if the worktree is live and has been idle at least *grace* secs."""
        return self.live and (now - self.last_active) >= grace
