"""Pipeline abstraction — one logical work-thread per issue/branch (issue #197).

A :class:`Pipeline` groups all phases of a single unit of work (plan → implement
→ review → CI fix → conflict resolution) under one branch-derived key. The
orchestrator enumerates pipelines once per tick (replacing six independent
``Task.discover()`` scans) and asks each for its single ``next_task()`` — the
highest-priority actionable task given the issue's + PR's current GitHub/git
state.

Two properties this buys us:

- **Idempotency computed once per pipeline.** "Has this already been handled?"
  is a pure function of state evaluated a single time, not re-derived six times
  with per-task ad-hoc markers (the #67 bug class).
- **A per-pipeline owner object.** Session/worktree reuse and on-demand
  interrogation (future issues) hang off this; nothing owns it today.

``Pipeline`` holds **no durable lifecycle state** — it is rebuilt fresh each tick
from facet objects fetched that tick, so crash → restart → rediscover stays
idempotent.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.tasks.base import issue_or_pr_keys
from loony_dev.tasks.ci_failure_task import ci_failure_action
from loony_dev.tasks.conflict_task import conflict_action
from loony_dev.tasks.issue_task import issue_action
from loony_dev.tasks.planning_task import planning_action
from loony_dev.tasks.pr_review_task import pr_review_action
from loony_dev.tasks.stuck_item_task import (
    stuck_issue_action,
    stuck_params,
    stuck_pr_action,
)

if TYPE_CHECKING:
    from loony_dev.github import Issue, PullRequest, Repo
    from loony_dev.tasks.base import Task

logger = logging.getLogger(__name__)

# Issue labels that can make an issue actionable; enumerated once per tick.
_RELEVANT_ISSUE_LABELS = ("ready-for-planning", "ready-for-development", "in-progress")


def _default_branch(repo: Repo) -> str:
    """Repository default branch, memoized in the per-tick cache.

    The conflict rung needs the default branch; without memoization a repo with
    many PRs would call ``detect_default_branch`` once per pipeline. The tick
    cache (cleared each ``Repo.clear_tick_cache``) keeps it to one gh call/tick.
    """
    cached = repo._tick_cache.get("default_branch")
    if cached is None:
        cached = repo.detect_default_branch()
        repo._tick_cache["default_branch"] = cached
    return cached


class Pipeline:
    """One logical work-thread, keyed by branch (``issue-N`` or ``pr-P``).

    Facets are the originating issue (if any) and the PR (if any). All phases of
    an issue share its ``issue-N`` key; a PR with an originating issue joins that
    issue's pipeline, while an externally-opened PR keys off its own ``pr-P``.
    The ``pipeline_key`` is exactly the task ``worktree_key`` (#181), so it
    doubles as the orchestrator's in-flight dedupe identity.
    """

    def __init__(
        self,
        pipeline_key: str,
        *,
        issue: Issue | None = None,
        pr: PullRequest | None = None,
    ) -> None:
        self.pipeline_key = pipeline_key
        self.issue = issue
        self.pr = pr

    # ------------------------------------------------------------------
    # Discovery — one enumeration replacing six discover() scans
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[Pipeline]:
        """Enumerate open issues + PRs once, grouped into pipelines by branch key."""
        from loony_dev.github import Issue, PullRequest

        pipelines: dict[str, Pipeline] = {}

        # Issues across the relevant labels, deduped by number (an issue can hold
        # more than one of these labels — e.g. ready-for-planning +
        # ready-for-development while a plan awaits approval).
        issues_by_number: dict[int, Issue] = {}
        for label in _RELEVANT_ISSUE_LABELS:
            for issue in Issue.list(label=label, repo=repo):
                issues_by_number.setdefault(issue.number, issue)
        for issue in issues_by_number.values():
            key = f"issue-{issue.number}"
            pipelines[key] = Pipeline(key, issue=issue)

        # Group open PRs onto their originating issue's pipeline, or a per-PR one.
        for pr in PullRequest.list_open(repo=repo):
            _, worktree_key = issue_or_pr_keys(pr)
            pipeline = pipelines.get(worktree_key)
            if pipeline is None:
                pipeline = Pipeline(worktree_key)
                pipelines[worktree_key] = pipeline
            if pipeline.pr is None:
                pipeline.pr = pr
            else:
                # One PR per pipeline: a second PR mapping to the same key (e.g.
                # two open PRs both closing issue N) is dropped. Log it so the
                # edge case is visible rather than silently hidden.
                logger.warning(
                    "Pipeline %s already has PR #%d; dropping duplicate PR #%d",
                    worktree_key, pipeline.pr.number, pr.number,
                )

        yield from pipelines.values()

    # ------------------------------------------------------------------
    # next_task — the single highest-priority action, as a pure read
    # ------------------------------------------------------------------

    def next_task(self, repo: Repo) -> Task | None:
        """Return this pipeline's single highest-priority actionable task, or None.

        Walks the same priority ladder the orchestrator used across six task
        classes, returning the first match — so a pipeline emits at most one
        task. Each rung is a pure predicate over already-fetched GitHub/git
        state (the same helpers ``discover()`` delegates to), so this never
        mutates GitHub and never accumulates state.
        """
        # Priority 5 — stuck cleanup (issue facet first, then PR, matching the
        # old StuckItemCleanupTask.discover ordering).
        threshold_hours, cutoff = stuck_params()
        if self.issue is not None:
            task = stuck_issue_action(self.issue, threshold_hours, cutoff)
            if task is not None:
                return task
        if self.pr is not None:
            task = stuck_pr_action(self.pr, threshold_hours, cutoff, repo.bot_name)
            if task is not None:
                return task

        # Priorities 10 / 15 / 20 — PR phases.
        if self.pr is not None:
            task = conflict_action(self.pr, repo.bot_name, _default_branch(repo))
            if task is not None:
                return task
            task = ci_failure_action(self.pr, repo.bot_name)
            if task is not None:
                return task
            task = pr_review_action(self.pr, repo)
            if task is not None:
                return task

        # Priorities 30 / 40 — issue phases.
        if self.issue is not None:
            task = planning_action(self.issue, repo)
            if task is not None:
                return task
            task = issue_action(self.issue, repo)
            if task is not None:
                return task

        return None

    def __repr__(self) -> str:
        facets = []
        if self.issue is not None:
            facets.append(f"issue=#{self.issue.number}")
        if self.pr is not None:
            facets.append(f"pr=#{self.pr.number}")
        return f"Pipeline({self.pipeline_key!r}, {', '.join(facets) or 'empty'})"
