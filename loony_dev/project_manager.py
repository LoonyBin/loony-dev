"""ProjectManager — orchestrates the worker pipeline at a higher level.

While the ``worker`` command *reactively* handles issues as they move through
labelled states, the project manager *proactively* selects, prioritises, and
promotes issues through the pipeline — ensuring that exactly N issues are
always in progress.

Key responsibilities:
- Health-check the default branch before every action (circuit-breaker).
- Count issues currently "in flight" across all active pipeline stages.
- If fewer than N are in flight, run the two-phase prioritiser and promote
  the best candidate into the pipeline.
- Auto-merge open PRs once CI passes and a configurable delay has elapsed
  (unless ``--skip-merge`` is set).
- Detect merged PRs and optionally verify successful deployment before
  marking issues as done.
- Repeat until interrupted.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loony_dev.github.pull_request import PullRequest
from loony_dev.github.workflow import Workflow
from loony_dev.prioritiser import Prioritiser
from loony_dev.tasks.planning_task import PLAN_MARKER_PREFIX

if TYPE_CHECKING:
    from loony_dev.github import Repo

logger = logging.getLogger(__name__)

# Sentinel injected into comments posted by the project manager.
PM_MARKER = "<!-- loony-pm -->"

# Labels that place an issue firmly inside the worker pipeline.
_PIPELINE_LABELS = frozenset({"ready-for-planning", "ready-for-development", "in-progress"})

# Label applied when the issue lifecycle is complete.
_DONE_LABEL = "done"


class ProjectManager:
    """Polling loop that drives issues through the worker pipeline.

    Parameters
    ----------
    github:
        Authenticated ``GitHubClient`` for the target repository.
    n:
        Maximum number of issues to keep in flight simultaneously.
    interval:
        Polling interval in seconds.
    skip_planning:
        If ``True``, promote candidates directly to ``ready-for-development``
        (skip the planning stage entirely).
    skip_merge:
        If ``True``, never auto-merge PRs — leave merging to a human reviewer.
    merge_delay:
        Seconds to wait after CI passes before auto-merging, to allow reviewer
        response.  Ignored when ``skip_merge`` is ``True``.
    deploy_workflow:
        Name of the deployment workflow to check for post-merge verification
        (without the ``.yml`` extension).  ``None`` or empty disables the check.
    milestone_soon_days:
        Days until milestone counts as "due soon" for prioritisation.
    milestone_cache_ttl:
        Seconds to cache milestone data.
    shortlist_size:
        Number of candidates forwarded to the AI agent in Phase 2.
    dependency_patterns:
        Prefixes that introduce blocking dependencies in issue bodies.
    ai_model:
        Claude model used for Phase-2 candidate ranking.
    """

    def __init__(
        self,
        github: Repo,
        n: int = 1,
        interval: int = 120,
        skip_planning: bool = False,
        skip_merge: bool = False,
        merge_delay: int = 86400,
        deploy_workflow: str | None = "deploy",
        milestone_soon_days: int = 14,
        milestone_cache_ttl: float = 3600.0,
        shortlist_size: int = 5,
        dependency_patterns: list[str] | None = None,
        ai_model: str = "claude-opus-4-6",
    ) -> None:
        self.github = github
        self.n = n
        self.interval = interval
        self.skip_planning = skip_planning
        self.skip_merge = skip_merge
        self.merge_delay = merge_delay
        self.deploy_workflow = deploy_workflow or ""
        self.milestone_cache_ttl = milestone_cache_ttl

        self._prioritiser = Prioritiser(
            github=github,
            shortlist_size=shortlist_size,
            milestone_soon_days=milestone_soon_days,
            dependency_patterns=dependency_patterns,
            ai_model=ai_model,
        )

        # Signal handling
        self._running: bool = True

        # Circuit-breaker state
        self._pipeline_paused: bool = False

        # PR merge-delay tracking: pr_number → monotonic timestamp when CI first passed
        self._ci_passed_at: dict[int, float] = {}

        # Deployment tracking: issue_number → (pr_number, merge_timestamp)
        self._merged_issue_prs: dict[int, tuple[int, datetime]] = {}

        # In-memory deduplication: prevents repeated log noise / API mutations
        # while GitHub state is settling.  Entries are strings like "promoted:42"
        # or "merge-delay:17" or "merge-done:17".
        self._actions_taken: set[str] = set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the polling loop.  Blocks until interrupted."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info(
            "ProjectManager started for %s (n=%d, interval=%ds, skip_merge=%s).",
            self.github.name, self.n, self.interval, self.skip_merge,
        )

        while self._running:
            try:
                self._tick()
            except Exception:
                logger.exception("Unhandled error during project-manager tick.")

            # Interruptible sleep: wake up each second to check the shutdown flag.
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("ProjectManager shut down cleanly.")

    def _handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("Signal %s received — shutting down after current tick.", signum)
        self._running = False

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Single iteration of the polling loop."""
        # Clear per-tick GitHub caches.
        self.github.clear_tick_cache()
        self.github.evict_stale_permission_cache()
        self.github.evict_stale_check_runs_cache()

        # 1. Health gate ─────────────────────────────────────────────────
        if not self._check_main_branch_health():
            if not self._pipeline_paused:
                self._pipeline_paused = True
                logger.warning(
                    "Pipeline paused: CI/deployment failure detected on default branch of %s. "
                    "No new issues will be promoted and no PRs will be merged until resolved.",
                    self.github.name,
                )
            return

        if self._pipeline_paused:
            self._pipeline_paused = False
            logger.info(
                "Pipeline resumed: default branch of %s is healthy again.",
                self.github.name,
            )

        # Snapshot data sources used across the rest of the tick.
        all_issues = self.github.issues.open
        open_prs = list(self.github.pull_requests.open)
        open_pr_by_number = {pr.number: pr for pr in open_prs}
        open_pr_issue_numbers = {num for pr in open_prs for num in pr.issues}

        # 2. Advance completed stages ─────────────────────────────────────
        if not self.skip_merge:
            self._maybe_merge_ready_prs(open_prs)

        self._check_merged_for_deployment(all_issues, open_pr_by_number)

        # 3. Promote new candidates ───────────────────────────────────────
        in_flight = self._count_in_flight(all_issues, open_pr_issue_numbers)
        slots_available = self.n - in_flight
        logger.debug(
            "In-flight: %d / %d  (slots available: %d)",
            in_flight, self.n, slots_available,
        )

        for _ in range(max(0, slots_available)):
            # Invalidate and re-fetch so each promotion sees up-to-date labels.
            self.github.issues.invalidate()
            all_issues = self.github.issues.open
            open_pr_issue_numbers = {num for pr in self.github.pull_requests.open for num in pr.issues}
            candidate = self._prioritiser.select_next(all_issues, open_pr_issue_numbers)
            if candidate is None:
                break
            issue_dict, rationale = candidate
            self._promote_to_pipeline(issue_dict, rationale)

    # ------------------------------------------------------------------
    # Health check (circuit-breaker)
    # ------------------------------------------------------------------

    def _check_main_branch_health(self) -> bool:
        """Return ``True`` if all check runs on the default branch's HEAD pass.

        Any incomplete or failing check run causes this to return ``False``.
        Returns ``True`` when no check runs exist (nothing to fail on).
        """
        branch = self.github.default_branch
        if not branch.sha:
            logger.warning("Could not determine default branch SHA; assuming healthy.")
            return True

        for run in branch.check_runs:
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")
            if status != "completed":
                logger.debug(
                    "Health check: run %r still %r — branch not yet settled.",
                    run.get("name"), status,
                )
                return False
            if conclusion not in ("success", "skipped", "neutral"):
                logger.debug(
                    "Health check: run %r has conclusion=%r — branch unhealthy.",
                    run.get("name"), conclusion,
                )
                return False

        return True

    # ------------------------------------------------------------------
    # In-flight counting
    # ------------------------------------------------------------------

    def _count_in_flight(
        self,
        all_issues: list[dict],
        open_pr_issue_numbers: set[int],
    ) -> int:
        """Count issues currently active in the pipeline."""
        count = 0
        for issue in all_issues:
            labels = {lbl["name"] for lbl in issue.get("labels", [])}
            # Explicitly labelled pipeline stages
            if labels & _PIPELINE_LABELS:
                count += 1
                continue
            # PR open with no pipeline label (implementation submitted, awaiting merge)
            number = issue["number"]
            if number in open_pr_issue_numbers and not (labels & _PIPELINE_LABELS):
                count += 1
        # Also count issues we know have been merged but deployment not yet confirmed.
        count += len(self._merged_issue_prs)
        return count

    # ------------------------------------------------------------------
    # PR merging with configurable delay
    # ------------------------------------------------------------------

    def _maybe_merge_ready_prs(self, open_prs: list[PullRequest]) -> None:
        """Merge PRs whose CI has passed and whose merge delay has elapsed."""
        now = time.monotonic()
        now_dt = datetime.now(timezone.utc)

        for pr in open_prs:
            labels = set(pr.labels)

            # Skip PRs that still carry active pipeline labels (worker is busy).
            if labels & _PIPELINE_LABELS:
                continue

            # Skip draft PRs.
            if pr.is_draft:
                continue

            if not pr.head_sha:
                continue

            # Fetch check runs for this PR's head commit.
            check_runs = pr.check_runs
            # pr.check_runs returns only FAILING runs.  We need to know
            # whether all runs are completed as well.
            entry = self.github._check_runs_cache.get(pr.head_sha)  # noqa: SLF001
            if entry is None:
                # Not yet cached — checks might still be running.
                logger.debug("PR #%d: check-runs cache miss — skipping this tick.", pr.number)
                continue

            if not entry.all_completed:
                logger.debug("PR #%d: CI still running — skipping.", pr.number)
                continue

            if check_runs:
                # Failing checks — CI failure handling belongs to CIFailureTask.
                logger.debug("PR #%d: %d failing check(s) — skip merge.", pr.number, len(check_runs))
                continue

            # All checks completed successfully.
            if pr.number not in self._ci_passed_at:
                self._ci_passed_at[pr.number] = now
                logger.info("PR #%d: CI passed; waiting %ds before auto-merge.", pr.number, self.merge_delay)

            elapsed = now - self._ci_passed_at[pr.number]
            if elapsed < self.merge_delay:
                remaining = int(self.merge_delay - elapsed)
                action_key = f"merge-delay:{pr.number}"
                if action_key not in self._actions_taken:
                    self._actions_taken.add(action_key)
                    logger.info(
                        "PR #%d: merge delay active — %dh %dm remaining before auto-merge.",
                        pr.number, remaining // 3600, (remaining % 3600) // 60,
                    )
                continue

            # Delay elapsed — merge.
            merge_action_key = f"merge-done:{pr.number}"
            if merge_action_key in self._actions_taken:
                continue

            logger.info("PR #%d: merge delay elapsed — auto-merging.", pr.number)
            success = pr.merge("squash")
            if success:
                self._actions_taken.add(merge_action_key)
                # Find the associated issue to track deployment.
                issue_number = pr.issues[0] if pr.issues else None
                if issue_number is not None:
                    self._merged_issue_prs[issue_number] = (pr.number, now_dt)
                    logger.info(
                        "Tracking deployment for issue #%d (PR #%d merged at %s).",
                        issue_number, pr.number, now_dt.isoformat(),
                    )
                # Remove merge-delay tracking for this PR.
                self._ci_passed_at.pop(pr.number, None)
                self._actions_taken.discard(f"merge-delay:{pr.number}")

    # ------------------------------------------------------------------
    # Deployment verification
    # ------------------------------------------------------------------

    def _check_merged_for_deployment(
        self,
        all_issues: list[dict],
        open_pr_by_number: dict[int, PullRequest],
    ) -> None:
        """Detect successful deployments for recently merged PRs and mark done."""
        done_issues: list[int] = []

        for issue_number, (pr_number, merge_ts) in list(self._merged_issue_prs.items()):
            # If the PR reappears as open, something went wrong — abort tracking.
            if pr_number in open_pr_by_number:
                logger.warning(
                    "PR #%d unexpectedly still open — removing from merge tracking.",
                    pr_number,
                )
                done_issues.append(issue_number)
                continue

            deployment_confirmed = self._deployment_confirmed(merge_ts)
            if deployment_confirmed:
                logger.info(
                    "Deployment confirmed for issue #%d (PR #%d). Marking done.",
                    issue_number, pr_number,
                )
                action_key = f"done:{issue_number}"
                if action_key not in self._actions_taken:
                    self._actions_taken.add(action_key)
                    self.github.add_label(issue_number, _DONE_LABEL)
                    self.github.post_comment(
                        issue_number,
                        f"{PM_MARKER} Deployment confirmed. Issue complete. :white_check_mark:",
                    )
                    self.github.issues.invalidate()
                done_issues.append(issue_number)

        for issue_number in done_issues:
            self._merged_issue_prs.pop(issue_number, None)

        # Also detect externally merged PRs for issues the PM promoted.
        # If an issue no longer has pipeline labels AND no open PR but we
        # haven't tracked its PR yet, probe for a recently merged PR.
        open_pr_issue_numbers = {num for pr in self.github.pull_requests.open for num in pr.issues}
        for issue in all_issues:
            number = issue["number"]
            labels = {lbl["name"] for lbl in issue.get("labels", [])}
            if labels & _PIPELINE_LABELS:
                continue
            if number in open_pr_issue_numbers:
                continue
            if number in self._merged_issue_prs:
                continue
            if _DONE_LABEL in labels:
                continue
            action_key = f"promoted:{number}"
            if action_key not in self._actions_taken:
                continue
            # This was an issue we promoted; it has no pipeline label and no open PR.
            # Look for a merged PR.
            pr_number = self.github.find_pr_for_issue(number)
            if pr_number is None:
                continue
            pr = PullRequest.get(pr_number, repo=self.github)
            merged_at = pr.merged_at
            if merged_at is not None:
                logger.info(
                    "Externally merged PR #%d detected for issue #%d.",
                    pr_number, number,
                )
                self._merged_issue_prs[number] = (pr_number, merged_at)

    def _deployment_confirmed(self, after: datetime) -> bool:
        """Return ``True`` if a successful deployment occurred after *after*."""
        # If no deploy workflow is configured, consider deployment instant.
        if not self.deploy_workflow:
            return True
        workflow = Workflow(self.deploy_workflow, repo=self.github)
        runs = workflow.runs.where(conclusion="success", timestamp_is_gt=after)
        return bool(runs)

    # ------------------------------------------------------------------
    # Pipeline promotion
    # ------------------------------------------------------------------

    def _promote_to_pipeline(self, issue: dict, rationale: str) -> None:
        """Label *issue* to enter the worker pipeline and post a comment."""
        number = issue["number"]
        action_key = f"promoted:{number}"
        if action_key in self._actions_taken:
            logger.debug("Issue #%d already promoted this session — skipping.", number)
            return

        use_planning = not self.skip_planning and not self._has_existing_plan(number)
        target_label = "ready-for-planning" if use_planning else "ready-for-development"

        logger.info(
            "Promoting issue #%d (%r) → %s. Rationale: %s",
            number, issue.get("title", ""), target_label, rationale or "(heuristic)",
        )

        rationale_section = f"\n\n**Rationale:** {rationale}" if rationale else ""
        comment_body = (
            f"{PM_MARKER} Promoting to `{target_label}`."
            f"{rationale_section}"
        )
        self.github.add_label(number, target_label)
        self.github.post_comment(number, comment_body)
        self.github.issues.invalidate()

        self._actions_taken.add(action_key)

    def _has_existing_plan(self, issue_number: int) -> bool:
        """Return ``True`` if the issue already has a bot plan comment."""
        comments = self.github.get_issue_comments(issue_number)
        return any(
            c.author == self.github.bot_name and c.body.startswith(PLAN_MARKER_PREFIX)
            for c in comments
        )
