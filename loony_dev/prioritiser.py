"""Two-phase issue prioritiser for the project-manager command.

Phase 1 — Heuristic filter
    Scores and ranks the full backlog using lightweight signals (milestone
    alignment, labels, age) to produce a small shortlist.  No external API
    calls beyond what the caller already holds.

Phase 2 — AI agent ranking
    Calls the Claude CLI to pick the single best candidate from the shortlist
    based on strategic value.  Falls back to the Phase-1 top pick if the
    Claude call fails.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient

logger = logging.getLogger(__name__)

# Labels that indicate an issue is already inside the worker pipeline.
_PIPELINE_LABELS = frozenset({"ready-for-planning", "ready-for-development", "in-progress"})

# Labels that permanently disqualify a candidate.
_EXCLUDE_LABELS = frozenset({"blocked", "wontfix", "duplicate"})

# The plan-comment marker written by PlanningTask.
_PLAN_MARKER_PREFIX = "<!-- loony-plan"

_DEFAULT_SHORTLIST_SIZE = 5
_DEFAULT_MILESTONE_SOON_DAYS = 14

# Pattern for dependency references in issue bodies.
_DEP_NUMBER_RE = re.compile(r"#(\d+)")


def _parse_dep_numbers(body: str, patterns: list[str]) -> list[int]:
    """Extract referenced issue numbers from *body* using *patterns*.

    Each pattern is a literal prefix ending just before the ``#`` sign
    (e.g. ``"Depends on #"``).  Every number that follows a matching
    prefix is returned.
    """
    numbers: list[int] = []
    for pattern in patterns:
        # Escape the prefix for use in a regex, replacing the trailing #
        # with a literal # followed by a capture group for digits.
        escaped = re.escape(pattern)
        regex = re.compile(escaped.rstrip(r"\#").rstrip() + r"\s*#(\d+)", re.IGNORECASE)
        for match in regex.finditer(body or ""):
            numbers.append(int(match.group(1)))
    return numbers


def _score_issue(
    issue: dict,
    milestone_soon_days: int,
) -> int:
    """Return a priority score for *issue* (lower = higher priority)."""
    score = 0
    labels = {lbl["name"] for lbl in issue.get("labels", [])}

    # Milestone alignment
    milestone = issue.get("milestone")
    if milestone:
        score -= 50
        due_on_str: str | None = milestone.get("due_on")
        if due_on_str:
            try:
                due_date = datetime.fromisoformat(due_on_str.replace("Z", "+00:00"))
                days_until = (due_date - datetime.now(timezone.utc)).days
                if days_until <= milestone_soon_days:
                    score -= 30
            except (ValueError, AttributeError):
                pass

    # Existing plan in the issue body (heuristic — full check needs comments API)
    if _PLAN_MARKER_PREFIX in (issue.get("body") or ""):
        score -= 15

    # Value labels
    if "priority:high" in labels:
        score -= 10
    if "bug" in labels:
        score -= 10
    if "enhancement" in labels:
        score -= 5

    # Issue age (starvation prevention: older issues get a slight boost)
    created_at = issue.get("createdAt") or issue.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days
            score -= age_days // 7
        except (ValueError, AttributeError):
            pass

    return score


class Prioritiser:
    """Select the best next issue to promote into the worker pipeline.

    Parameters
    ----------
    github:
        GitHubClient instance used for dependency checks.
    shortlist_size:
        Maximum number of candidates passed to the AI agent (Phase 2).
    milestone_soon_days:
        Issues whose milestone is due within this many days receive an
        extra priority boost in Phase 1.
    dependency_patterns:
        List of literal prefixes that introduce a blocking dependency
        reference (e.g. ``["Depends on #", "Blocked by #"]``).
    ai_model:
        Claude model ID to use for Phase-2 ranking.
    """

    def __init__(
        self,
        github: GitHubClient,
        shortlist_size: int = _DEFAULT_SHORTLIST_SIZE,
        milestone_soon_days: int = _DEFAULT_MILESTONE_SOON_DAYS,
        dependency_patterns: list[str] | None = None,
        ai_model: str = "claude-opus-4-6",
    ) -> None:
        self.github = github
        self.shortlist_size = shortlist_size
        self.milestone_soon_days = milestone_soon_days
        self.dependency_patterns: list[str] = dependency_patterns or [
            "Depends on #",
            "Blocked by #",
        ]
        self.ai_model = ai_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_next(
        self,
        all_issues: list[dict],
        open_pr_issue_numbers: set[int],
    ) -> tuple[dict, str] | None:
        """Return ``(issue_dict, rationale)`` for the best candidate, or ``None``.

        *all_issues* is the full list of open issues (from
        ``github.list_issues_all()``).  *open_pr_issue_numbers* is the
        set of issue numbers that already have an open PR (from
        ``github.get_open_pr_issue_numbers()``).
        """
        open_issue_numbers = {i["number"] for i in all_issues}
        shortlist = self._phase1_filter(all_issues, open_pr_issue_numbers, open_issue_numbers)

        if not shortlist:
            logger.info("Prioritiser: no candidates available after Phase-1 filter.")
            return None

        if len(shortlist) == 1:
            logger.info(
                "Prioritiser: single candidate #%d — skipping Phase-2 AI call.",
                shortlist[0]["number"],
            )
            return shortlist[0], ""

        return self._phase2_ai_rank(shortlist)

    # ------------------------------------------------------------------
    # Phase 1: heuristic filter
    # ------------------------------------------------------------------

    def _phase1_filter(
        self,
        all_issues: list[dict],
        open_pr_issue_numbers: set[int],
        open_issue_numbers: set[int],
    ) -> list[dict]:
        """Narrow *all_issues* to a scored shortlist of at most ``shortlist_size`` items."""
        candidates = [
            issue for issue in all_issues
            if not self._should_exclude(issue, open_pr_issue_numbers, open_issue_numbers)
        ]

        if not candidates:
            return []

        scored = sorted(
            candidates,
            key=lambda iss: _score_issue(iss, self.milestone_soon_days),
        )
        shortlist = scored[: self.shortlist_size]
        logger.debug(
            "Prioritiser Phase 1: %d candidate(s) → shortlist of %d",
            len(candidates), len(shortlist),
        )
        return shortlist

    def _should_exclude(
        self,
        issue: dict,
        open_pr_issue_numbers: set[int],
        open_issue_numbers: set[int],
    ) -> bool:
        """Return ``True`` if *issue* must be excluded from candidacy."""
        number = issue["number"]
        labels = {lbl["name"] for lbl in issue.get("labels", [])}

        # Active pipeline labels
        if labels & _PIPELINE_LABELS:
            return True

        # Permanent exclusion labels
        if labels & _EXCLUDE_LABELS:
            return True

        # Already has an open PR
        if number in open_pr_issue_numbers:
            return True

        # Unresolved blocking dependencies
        body = issue.get("body") or ""
        for dep_num in _parse_dep_numbers(body, self.dependency_patterns):
            if dep_num in open_issue_numbers:
                logger.debug(
                    "Issue #%d excluded: open blocking dependency #%d",
                    number, dep_num,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Phase 2: AI agent ranking
    # ------------------------------------------------------------------

    def _phase2_ai_rank(self, shortlist: list[dict]) -> tuple[dict, str]:
        """Ask Claude to select the best candidate from *shortlist*.

        Falls back to the Phase-1 top pick if the Claude call fails.
        """
        milestones = self.github.list_milestones()
        try:
            chosen_number, rationale = self._call_claude(shortlist, milestones)
            for issue in shortlist:
                if issue["number"] == chosen_number:
                    logger.info(
                        "Prioritiser Phase 2: AI selected #%d — %s",
                        chosen_number, rationale,
                    )
                    return issue, rationale
            logger.warning(
                "Prioritiser Phase 2: AI returned unknown issue #%d; "
                "falling back to Phase-1 top pick.",
                chosen_number,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Prioritiser Phase 2: Claude call failed (%s); "
                "falling back to Phase-1 top pick.",
                exc,
            )

        return shortlist[0], ""

    def _call_claude(
        self,
        shortlist: list[dict],
        milestones: dict[str, datetime | None],
    ) -> tuple[int, str]:
        """Invoke the Claude CLI to rank *shortlist*.

        Returns ``(chosen_issue_number, rationale_text)``.
        Raises on any error so the caller can fall back gracefully.
        """
        milestones_text = "\n".join(
            f"- {name}: due {due.strftime('%Y-%m-%d') if due else 'no due date'}"
            for name, due in milestones.items()
        ) or "No open milestones."

        issues_text = "\n\n".join(
            "Issue #{number}: {title}\n"
            "Labels: {labels}\n"
            "Milestone: {milestone}\n"
            "Body (first 500 chars):\n{body}".format(
                number=iss["number"],
                title=iss.get("title", ""),
                labels=", ".join(lbl["name"] for lbl in iss.get("labels", [])) or "none",
                milestone=(iss.get("milestone") or {}).get("title", "none"),
                body=(iss.get("body") or "")[:500],
            )
            for iss in shortlist
        )

        prompt = (
            "You are an agile product manager helping to prioritise engineering work.\n\n"
            f"Open milestones:\n{milestones_text}\n\n"
            f"Candidate issues to evaluate:\n{issues_text}\n\n"
            "Select the single most valuable issue to work on next. Consider:\n"
            "- Strategic value and user impact\n"
            "- Technical risk and unblocking potential\n"
            "- Milestone alignment and upcoming deadlines\n"
            "- Momentum (issues close to completion)\n\n"
            'Respond with ONLY a JSON object: {"issue_number": <number>, "rationale": "<brief reason>"}'
        )

        result = subprocess.run(
            ["claude", "-p", "--model", self.ai_model, prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Claude exited {result.returncode}: {result.stderr[:300]}"
            )

        output = result.stdout.strip()
        # The model might wrap the JSON in a code fence or prose; extract conservatively.
        json_match = re.search(r'\{[^{}]*"issue_number"[^{}]*\}', output)
        if not json_match:
            raise ValueError(f"Could not parse JSON from Claude output: {output[:400]}")

        data = json.loads(json_match.group())
        rationale: str = data.get("rationale", "")
        return int(data["issue_number"]), rationale
