"""Per-pipeline session manager — a reusable worktree + session id (issue #198).

A :class:`PipelineSession` is the long-lived owner object the orchestrator keeps
for each *active* pipeline (``issue-N`` / ``pr-P``). Consecutive tasks on one
pipeline (implement → CI fix → review → conflict) **reuse** its single git
worktree and deterministic Claude session id instead of paying a per-task
create/trust/install/teardown cycle. The worktree is retained for the whole
issue/PR cycle — so the operator can ``cd`` into it and inspect what the bot did
at any point between phases — and is released only once the pipeline reaches a
terminal GitHub state (PR merged/closed, or issue closed with no PR), leaving the
on-disk session transcript and registry entry behind for on-demand
interrogation (#199).

It is a plain state object — **not** a process. There is no PTY here; agent turns
still run via ``claude -p``. The orchestrator owns the git mutations (serialized
by its ``_git_lock``); this class only tracks *what* the pipeline's worktree and
session are and *whether* the worktree is currently live.

``PipelineSession`` is in-memory only. Durable truth stays GitHub + the on-disk
worktree/session/registry, so a crash simply empties the orchestrator's map and
the next tick rebuilds it lazily.
"""
from __future__ import annotations

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
    worktree exists on disk. The worktree is retained for the whole issue/PR
    cycle and released only when the pipeline reaches a terminal GitHub state
    (the orchestrator's ``_reclaim_completed_pipelines``), so there is no idle
    timer here — the operator can inspect the worktree at any point between
    phases.
    """

    pipeline_key: str
    branch: str
    base: str | None
    worktree_path: Path
    session_id: str | None = None
    session_key: str | None = None
    live: bool = False

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
