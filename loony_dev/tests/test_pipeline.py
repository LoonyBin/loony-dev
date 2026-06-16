"""Tests for the Pipeline abstraction (issue #197).

A Pipeline groups all phases of one logical work-thread (issue + its PR) under a
single branch key and exposes ``next_task()`` — a *pure function of GitHub + git
state* returning the single highest-priority actionable task. These tests pin:

- discovery + grouping (issue↔PR onto one ``issue-N`` pipeline);
- the priority ladder in ``next_task`` (one task per pipeline, highest wins);
- per-pipeline idempotency, incl. the #67 CI marker-vs-updatedAt regression;
- the characterization property: the pipeline-centric ``_find_work`` dispatches
  the same (task_type) set the six-scan ``discover()`` loop would; and
- the crash-recovery property: no durable in-memory state — rediscovery is
  identical.
"""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from loony_dev.github.check_run import CheckRun
from loony_dev.github.comment import Comment
from loony_dev.github.issue import Issue
from loony_dev.github.pull_request import PullRequest
from loony_dev.orchestrator import TASK_CLASSES, Orchestrator
from loony_dev.pipeline import Pipeline
from loony_dev.tasks.base import CI_FAILURE_MARKER

BOT = "loony-bot"
USER = "alice"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_repo() -> MagicMock:
    repo = MagicMock()
    repo.name = "owner/repo"
    repo.owner = "owner"
    repo.bot_name = BOT
    repo._tick_cache = {}
    repo.is_authorized = MagicMock(return_value=True)
    repo.detect_default_branch.return_value = "main"
    return repo


def _issue(
    number: int,
    *,
    labels: list[str],
    assignees: list[str] | None = None,
    updated_at: datetime | None = None,
    repo: MagicMock,
) -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        body="body",
        author=USER,
        updated_at=updated_at,
        labels=labels,
        assignees=assignees or [],
        _repo=repo,
    )


def _pr(
    number: int,
    *,
    branch: str,
    labels: list[str] | None = None,
    assignees_bot: bool = True,
    mergeable: str = "MERGEABLE",
    head_sha: str = "sha1",
    comments: list[Comment] | None = None,
    updated_at: datetime | None = None,
    repo: MagicMock,
) -> PullRequest:
    return PullRequest(
        number=number,
        branch=branch,
        title=f"PR {number}",
        head_sha=head_sha,
        mergeable=mergeable,
        updated_at=updated_at,
        labels=labels or [],
        comments=comments or [],
        assignees=[{"login": BOT}] if assignees_bot else [{"login": USER}],
        _repo=repo,
    )


@contextlib.contextmanager
def _world(
    repo: MagicMock,
    *,
    issues: list[Issue] | None = None,
    prs: list[PullRequest] | None = None,
    issue_comments: dict[int, list[Comment]] | None = None,
    failing_checks: dict[str, list[CheckRun]] | None = None,
):
    """Patch the GitHub listing surface so discovery sees *issues* and *prs*."""
    issues = issues or []
    prs = prs or []
    issue_comments = issue_comments or {}
    failing_checks = failing_checks or {}
    repo._tick_cache["open_prs"] = prs

    def _issue_list(*, label: str, repo: MagicMock) -> list[Issue]:
        return [i for i in issues if label in i.labels]

    def _comments_for(number: int, *, repo: MagicMock) -> list[Comment]:
        return issue_comments.get(number, [])

    def _failing(head_sha: str, *, repo: MagicMock) -> list[CheckRun]:
        return failing_checks.get(head_sha, [])

    with patch.object(Issue, "list", staticmethod(_issue_list)), \
         patch.object(Comment, "list_for_issue", staticmethod(_comments_for)), \
         patch.object(Comment, "list_inline_for_pr", staticmethod(lambda n, *, repo: [])), \
         patch.object(CheckRun, "list_failing", staticmethod(_failing)):
        yield


def _make_orchestrator(repo: MagicMock, testcase: unittest.TestCase) -> Orchestrator:
    tmpdir = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmpdir.cleanup)
    git = MagicMock()
    git.work_dir = Path(tmpdir.name)
    git.default_branch = "main"
    git.list_worktrees.return_value = []
    return Orchestrator(repo=repo, git=git, agents=[_always_agent()], interval=60)


def _always_agent() -> MagicMock:
    agent = MagicMock()
    agent.name = "agent"
    agent.can_handle.return_value = True
    return agent


def _types(tasks) -> set[str]:
    return {t.task_type for t in tasks}


# ---------------------------------------------------------------------------
# Discovery + grouping
# ---------------------------------------------------------------------------


class TestPipelineDiscovery(unittest.TestCase):

    def test_issue_with_no_pr_is_one_pipeline(self) -> None:
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-development"], repo=repo)
        with _world(repo, issues=[issue]):
            pipelines = list(Pipeline.discover(repo))
        self.assertEqual(len(pipelines), 1)
        self.assertEqual(pipelines[0].pipeline_key, "issue-7")
        self.assertIs(pipelines[0].issue, issue)
        self.assertIsNone(pipelines[0].pr)

    def test_pr_joins_originating_issue_pipeline(self) -> None:
        repo = _make_repo()
        issue = _issue(7, labels=["in-progress"], repo=repo)
        pr = _pr(20, branch="issue-7/slug", repo=repo)
        with _world(repo, issues=[issue], prs=[pr]):
            pipelines = {p.pipeline_key: p for p in Pipeline.discover(repo)}
        self.assertEqual(set(pipelines), {"issue-7"})
        self.assertIs(pipelines["issue-7"].issue, issue)
        self.assertIs(pipelines["issue-7"].pr, pr)

    def test_external_pr_forms_own_pipeline(self) -> None:
        repo = _make_repo()
        pr = _pr(20, branch="feature/external", repo=repo)
        with _world(repo, prs=[pr]):
            pipelines = {p.pipeline_key: p for p in Pipeline.discover(repo)}
        self.assertEqual(set(pipelines), {"pr-20"})
        self.assertIsNone(pipelines["pr-20"].issue)
        self.assertIs(pipelines["pr-20"].pr, pr)

    def test_issue_deduped_across_labels(self) -> None:
        # An issue carrying two relevant labels appears once, not twice.
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-planning", "ready-for-development"], repo=repo)
        with _world(repo, issues=[issue]):
            pipelines = list(Pipeline.discover(repo))
        self.assertEqual(len(pipelines), 1)


# ---------------------------------------------------------------------------
# next_task — the priority ladder
# ---------------------------------------------------------------------------


class TestNextTask(unittest.TestCase):

    def test_ready_for_planning_yields_planning(self) -> None:
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-planning"], repo=repo)
        with _world(repo, issues=[issue]):
            task = Pipeline("issue-7", issue=issue).next_task(repo)
        self.assertEqual(task.task_type, "plan_issue")

    def test_ready_for_development_yields_implement(self) -> None:
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-development"], repo=repo)
        with _world(repo, issues=[issue]):
            task = Pipeline("issue-7", issue=issue).next_task(repo)
        self.assertEqual(task.task_type, "implement_issue")

    def test_both_labels_yields_implement_not_plan(self) -> None:
        # Plan approved (ready-for-development present) => implement, never plan.
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-planning", "ready-for-development"], repo=repo)
        with _world(repo, issues=[issue]):
            task = Pipeline("issue-7", issue=issue).next_task(repo)
        self.assertEqual(task.task_type, "implement_issue")

    def test_conflict_beats_ci_and_review(self) -> None:
        repo = _make_repo()
        pr = _pr(
            20, branch="issue-7/slug", mergeable="CONFLICTING",
            comments=[Comment(author=USER, body="please fix", created_at="2024-01-01T00:00:00Z")],
            repo=repo,
        )
        failing = {"sha1": [CheckRun(name="t", status="completed", conclusion="failure", details_url="u")]}
        with _world(repo, prs=[pr], failing_checks=failing):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertEqual(task.task_type, "resolve_conflicts")

    def test_stuck_beats_conflict(self) -> None:
        repo = _make_repo()
        old = datetime(2000, 1, 1, tzinfo=timezone.utc)
        pr = _pr(
            20, branch="issue-7/slug", labels=["in-progress"],
            mergeable="CONFLICTING", updated_at=old, repo=repo,
        )
        with _world(repo, prs=[pr]), \
             patch("loony_dev.tasks.stuck_item_task.stuck_params", return_value=(0, datetime.now(timezone.utc))):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertEqual(task.task_type, "cleanup_stuck")

    def test_in_error_pr_parks(self) -> None:
        repo = _make_repo()
        pr = _pr(20, branch="issue-7/slug", labels=["in-error"], mergeable="CONFLICTING", repo=repo)
        with _world(repo, prs=[pr]):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertIsNone(task)

    def test_other_assignee_issue_parks(self) -> None:
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-development"], assignees=["someone-else"], repo=repo)
        with _world(repo, issues=[issue]):
            task = Pipeline("issue-7", issue=issue).next_task(repo)
        self.assertIsNone(task)

    def test_idle_pipeline_yields_none(self) -> None:
        repo = _make_repo()
        pr = _pr(20, branch="feature/x", mergeable="MERGEABLE", repo=repo)
        with _world(repo, prs=[pr]):
            task = Pipeline("pr-20", pr=pr).next_task(repo)
        self.assertIsNone(task)


# ---------------------------------------------------------------------------
# #67 regression — CI idempotency is a pure function of state in next_task
# ---------------------------------------------------------------------------


class TestCIIdempotencyRegression(unittest.TestCase):
    """The marker-vs-updatedAt 'already handled?' check lives once in next_task."""

    def _ci_pr(self, *, marker_at: str | None, updated_at: datetime) -> tuple[MagicMock, PullRequest]:
        repo = _make_repo()
        comments = []
        if marker_at is not None:
            comments.append(Comment(author=BOT, body=f"{CI_FAILURE_MARKER}\n\ndone", created_at=marker_at))
        pr = _pr(
            20, branch="issue-7/slug", mergeable="MERGEABLE",
            comments=comments, updated_at=updated_at, repo=repo,
        )
        return repo, pr

    def test_unhandled_failure_yields_ci_task(self) -> None:
        repo, pr = self._ci_pr(marker_at=None, updated_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
        failing = {"sha1": [CheckRun(name="t", status="completed", conclusion="failure", details_url="u")]}
        with _world(repo, prs=[pr], failing_checks=failing):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertEqual(task.task_type, "fix_ci")

    def test_marker_after_push_means_handled(self) -> None:
        # Marker posted at/after updatedAt => already handled, no task.
        repo, pr = self._ci_pr(
            marker_at="2024-01-02T00:00:00Z", updated_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        failing = {"sha1": [CheckRun(name="t", status="completed", conclusion="failure", details_url="u")]}
        with _world(repo, prs=[pr], failing_checks=failing):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertIsNone(task)

    def test_marker_before_new_push_reopens(self) -> None:
        # A newer push (updatedAt after the marker) => failure is fresh again.
        repo, pr = self._ci_pr(
            marker_at="2024-01-01T00:00:00Z", updated_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        failing = {"sha1": [CheckRun(name="t", status="completed", conclusion="failure", details_url="u")]}
        with _world(repo, prs=[pr], failing_checks=failing):
            task = Pipeline("issue-7", pr=pr).next_task(repo)
        self.assertEqual(task.task_type, "fix_ci")


# ---------------------------------------------------------------------------
# Characterization — pipeline loop dispatches the same set as the six-scan loop
# ---------------------------------------------------------------------------


def _old_style_select(repo: MagicMock, agents: list, limit: int) -> list:
    """Re-implementation of the pre-#197 class-by-class scan, for comparison."""
    seen: set[str] = set()
    results = []
    for tc in sorted(TASK_CLASSES, key=lambda c: c.priority):
        for task in tc.discover(repo):
            identity = task.worktree_key or task.target_branch or f"task-{id(task)}"
            if identity in seen:
                continue
            for agent in agents:
                if agent.can_handle(task):
                    seen.add(identity)
                    results.append(task)
                    break
            if len(results) >= limit:
                return results
    return results


class TestCharacterization(unittest.TestCase):
    """For representative repo states, both loops dispatch the same task-type set."""

    def _assert_same(self, repo: MagicMock, *, issues=None, prs=None,
                     issue_comments=None, failing_checks=None) -> None:
        with _world(repo, issues=issues, prs=prs,
                    issue_comments=issue_comments, failing_checks=failing_checks):
            old = _old_style_select(repo, [_always_agent()], limit=10)
            orch = _make_orchestrator(repo, self)
            new = [task for task, _agent in orch._find_work(limit=10, claimed=set())]
        self.assertEqual(_types(old), _types(new))

    def test_planning_needed(self) -> None:
        repo = _make_repo()
        self._assert_same(repo, issues=[_issue(1, labels=["ready-for-planning"], repo=repo)])

    def test_ready_to_implement(self) -> None:
        repo = _make_repo()
        self._assert_same(repo, issues=[_issue(1, labels=["ready-for-development"], repo=repo)])

    def test_pr_conflicting(self) -> None:
        repo = _make_repo()
        pr = _pr(5, branch="issue-1/slug", mergeable="CONFLICTING", repo=repo)
        self._assert_same(repo, prs=[pr])

    def test_pr_ci_failing(self) -> None:
        repo = _make_repo()
        pr = _pr(5, branch="issue-1/slug", mergeable="MERGEABLE",
                 updated_at=datetime(2024, 1, 2, tzinfo=timezone.utc), repo=repo)
        failing = {"sha1": [CheckRun(name="t", status="completed", conclusion="failure", details_url="u")]}
        self._assert_same(repo, prs=[pr], failing_checks=failing)

    def test_multiple_independent_pipelines(self) -> None:
        repo = _make_repo()
        issues = [
            _issue(1, labels=["ready-for-planning"], repo=repo),
            _issue(2, labels=["ready-for-development"], repo=repo),
        ]
        pr = _pr(5, branch="feature/ext", mergeable="CONFLICTING", repo=repo)
        self._assert_same(repo, issues=issues, prs=[pr])

    def test_idle_repo(self) -> None:
        repo = _make_repo()
        self._assert_same(repo)


# ---------------------------------------------------------------------------
# Crash-recovery / purity property — no durable in-memory state
# ---------------------------------------------------------------------------


class TestPurity(unittest.TestCase):

    def test_repeated_discovery_is_identical(self) -> None:
        repo = _make_repo()
        issue = _issue(1, labels=["ready-for-development"], repo=repo)
        pr = _pr(5, branch="feature/x", mergeable="CONFLICTING", repo=repo)
        with _world(repo, issues=[issue], prs=[pr]):
            first = {p.pipeline_key: (p.next_task(repo).task_type if p.next_task(repo) else None)
                     for p in Pipeline.discover(repo)}
            # A second, independent enumeration (simulating restart) must agree.
            second = {p.pipeline_key: (p.next_task(repo).task_type if p.next_task(repo) else None)
                      for p in Pipeline.discover(repo)}
        self.assertEqual(first, second)

    def test_discovery_creates_no_pipeline_sessions(self) -> None:
        # Lazy instantiation (#198): enumerating pipelines and gathering work must
        # not materialize any PipelineSession or touch a git worktree — those are
        # created only when a task is dispatched.
        repo = _make_repo()
        issue = _issue(1, labels=["ready-for-development"], repo=repo)
        pr = _pr(5, branch="feature/x", mergeable="CONFLICTING", repo=repo)
        with _world(repo, issues=[issue], prs=[pr]):
            orch = _make_orchestrator(repo, self)
            batch = orch._find_work(limit=10, claimed=set())
        # Work was actually found, so the no-session assertion is meaningful
        # (not vacuously true on an empty gather).
        self.assertTrue(batch)
        self.assertEqual(orch._pipeline_sessions, {})
        orch.git.create_worktree.assert_not_called()

    def test_pipeline_holds_no_progress_state(self) -> None:
        repo = _make_repo()
        issue = _issue(1, labels=["ready-for-development"], repo=repo)
        with _world(repo, issues=[issue]):
            pipeline = Pipeline("issue-1", issue=issue)
            t1 = pipeline.next_task(repo)
            t2 = pipeline.next_task(repo)
        # Calling next_task twice returns the same kind of task — no internal
        # cursor advanced, nothing consumed.
        self.assertEqual(t1.task_type, t2.task_type)
        self.assertEqual(set(vars(pipeline)), {"pipeline_key", "issue", "pr"})


# ---------------------------------------------------------------------------
# R3 — planning label reconciliation relocated to IssueTask.on_start
# ---------------------------------------------------------------------------


class TestPlanningLabelReconciliation(unittest.TestCase):
    """The stale 'ready-for-planning' cleanup moved out of discovery (a read) into
    the implementation lease, so pipeline enumeration never mutates GitHub."""

    def _issue_task(self, labels: list[str]) -> tuple[MagicMock, object]:
        from loony_dev.tasks.issue_task import IssueTask

        repo = _make_repo()
        issue = MagicMock()
        issue.number = 7
        issue.labels = labels
        return repo, IssueTask(issue)

    def test_on_start_removes_stale_planning_label(self) -> None:
        repo, task = self._issue_task(["ready-for-planning", "ready-for-development"])
        task.on_start(repo)
        removed = [c.args[0] for c in task.issue.remove_label.call_args_list]
        self.assertIn("ready-for-planning", removed)
        self.assertIn("ready-for-development", removed)
        task.issue.add_label.assert_any_call("in-progress")

    def test_on_start_skips_planning_removal_when_absent(self) -> None:
        repo, task = self._issue_task(["ready-for-development"])
        task.on_start(repo)
        removed = [c.args[0] for c in task.issue.remove_label.call_args_list]
        self.assertNotIn("ready-for-planning", removed)
        self.assertIn("ready-for-development", removed)

    def test_next_task_does_not_mutate_github(self) -> None:
        # next_task is a pure read: for a both-labels issue it returns the
        # implement task and touches no label-mutating method.
        repo = _make_repo()
        issue = _issue(7, labels=["ready-for-planning", "ready-for-development"], repo=repo)
        issue.remove_label = MagicMock()
        issue.add_label = MagicMock()
        with _world(repo, issues=[issue]):
            task = Pipeline("issue-7", issue=issue).next_task(repo)
        self.assertEqual(task.task_type, "implement_issue")
        issue.remove_label.assert_not_called()
        issue.add_label.assert_not_called()


if __name__ == "__main__":
    unittest.main()
