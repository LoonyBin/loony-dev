"""Repository-level operations: detection, authorization, labels, and caching."""
from __future__ import annotations

import functools
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loony_dev.github.client import GitHubClient, gh_setting, run_gh

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

_ROLE_HIERARCHY = ["none", "read", "triage", "write", "admin"]


def _roles_at_or_above(min_role: str) -> set[str]:
    """Return the set of role names that are >= *min_role* in the hierarchy."""
    try:
        idx = _ROLE_HIERARCHY.index(min_role)
    except ValueError:
        logger.warning("Unknown min_role %r; defaulting to 'triage'", min_role)
        idx = _ROLE_HIERARCHY.index("triage")
    return set(_ROLE_HIERARCHY[idx:])


# ---------------------------------------------------------------------------
# Datetime helper (used by models)
# ---------------------------------------------------------------------------


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string from the GitHub API into a UTC-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

REQUIRED_LABELS = [
    {"name": "ready-for-development", "color": "0075ca", "description": "Issue is ready for implementation"},
    {"name": "ready-for-planning",    "color": "e4e669", "description": "Issue needs planning/triage"},
    {"name": "in-progress",           "color": "d93f0b", "description": "Bot is actively working on this"},
]


# ---------------------------------------------------------------------------
# Check-runs cache entry
# ---------------------------------------------------------------------------


@dataclass
class CheckRunsCacheEntry:
    failing_runs: list  # list[CheckRun] — forward reference to avoid circular import
    all_completed: bool
    cached_at: float  # time.monotonic()


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------


class Repo:
    """Repository-level operations: detection, auth, labels, caching.

    This is the main entry point for interacting with a GitHub repository.
    Active Record models receive a ``Repo`` reference to access the underlying
    ``GitHubClient`` transport.
    """

    def __init__(
        self,
        repo: str | None = None,
        bot_name: str | None = None,
        allowed_users: set[str] | None = None,
        min_role: str | None = None,
        skip_ci_checks: set[str] | None = None,
    ) -> None:
        from loony_dev import config

        self.name: str = repo or Repo.detect()
        self.client = GitHubClient(self.name)
        self.bot_name: str = bot_name or config.settings.get("bot_name") or Repo.detect_bot_name()
        self.allowed_users: set[str] = (
            allowed_users if allowed_users is not None
            else set(config.settings.get("allowed_users") or [])
        )
        self.min_role: str = min_role or config.settings.get("min_role") or "triage"
        self.skip_ci_checks: set[str] = (
            skip_ci_checks if skip_ci_checks is not None
            else set(config.settings.get("skip_ci_checks") or [])
        )
        # Cache: username -> (permission_level, monotonic_timestamp)
        self._permission_cache: dict[str, tuple[str | None, float]] = {}
        # Tick-scoped cache: cleared at the start of each tick
        self._tick_cache: dict[str, Any] = {}
        # Cross-tick cache: head_sha -> CheckRunsCacheEntry
        self._check_runs_cache: dict[str, CheckRunsCacheEntry] = {}
        # Long-lived cache: (milestones_dict, cached_at_monotonic)
        self._milestones_cache: tuple[dict[str, datetime | None], float] | None = None
        # Short-lived cache: (issues_list, cached_at_monotonic)
        self._issues_all_cache: tuple[list[dict], float] | None = None

    # --- Detection ---

    @staticmethod
    def detect() -> str:
        """Detect owner/repo from git remote URL."""
        return run_gh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def detect_bot_name() -> str:
        """Detect the authenticated GitHub user's login via the gh CLI."""
        return run_gh("gh", "api", "user", "-q", ".login")

    def detect_default_branch(self) -> str:
        """Detect the repository's default branch via the gh CLI.

        Falls back to ``"main"`` if detection fails.
        """
        try:
            branch = run_gh(
                "gh", "repo", "view", self.name,
                "--json", "defaultBranchRef",
                "-q", ".defaultBranchRef.name",
            )
            if branch:
                return branch
        except subprocess.CalledProcessError:
            logger.warning("Failed to detect default branch for %s; falling back to 'main'", self.name)
        return "main"

    # --- Authorization ---

    def get_user_permission(self, username: str) -> str | None:
        """Return the user's repository permission level, or None.

        Results are cached for ``permission_cache_ttl`` seconds.
        """
        now = time.monotonic()
        if username in self._permission_cache:
            cached_perm, cached_at = self._permission_cache[username]
            if now - cached_at < gh_setting("permission_cache_ttl"):
                logger.debug("Permission cache hit for %r: %r", username, cached_perm)
                return cached_perm

        try:
            output = self.client.gh(
                "api",
                f"repos/{self.name}/collaborators/{username}/permission",
                "-q", ".permission",
            )
            permission: str | None = output if output else None
        except subprocess.CalledProcessError:
            permission = None

        self._permission_cache[username] = (permission, now)
        logger.debug("Permission for %r in %s: %r (cached)", username, self.name, permission)
        return permission

    def is_authorized(self, username: str) -> bool:
        """Return True if *username* is authorized to trigger agent runs."""
        if username in self.allowed_users:
            return True
        permission = self.get_user_permission(username)
        return permission in _roles_at_or_above(self.min_role)

    def evict_stale_permission_cache(self) -> None:
        """Remove expired entries from the permission cache."""
        now = time.monotonic()
        stale = [u for u, (_, ts) in self._permission_cache.items() if now - ts >= gh_setting("permission_cache_ttl")]
        for u in stale:
            del self._permission_cache[u]
        if stale:
            logger.debug("Evicted %d stale permission cache entries", len(stale))

    # --- Tick-scoped cache ---

    def clear_tick_cache(self) -> None:
        """Discard all per-tick cached data.  Call at the start of each tick."""
        self._tick_cache.clear()

    # --- Check-runs cache ---

    def evict_stale_check_runs_cache(self) -> None:
        """Remove expired entries from the check-runs cache."""
        now = time.monotonic()
        stale = [sha for sha, entry in self._check_runs_cache.items() if now - entry.cached_at >= gh_setting("check_runs_cache_ttl")]
        for sha in stale:
            del self._check_runs_cache[sha]
        if stale:
            logger.debug("Evicted %d stale check-runs cache entries", len(stale))

    # --- Labels ---

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create label if it doesn't exist.  Silently ignores conflicts (422)."""
        try:
            self.client.gh(
                "api", f"repos/{self.name}/labels",
                "--method", "POST",
                "-f", f"name={name}",
                "-f", f"color={color}",
                "-f", f"description={description}",
            )
            logger.debug("Created label %r in %s", name, self.name)
        except subprocess.CalledProcessError as e:
            output = (e.stderr or "") + (e.stdout or "")
            if "already_exists" in output or "422" in output:
                logger.debug("Label %r already exists in %s", name, self.name)
            else:
                logger.warning(
                    "Failed to provision label %r in %s: %s",
                    name, self.name, (e.stderr or "").strip(),
                )

    def ensure_required_labels(self) -> None:
        """Provision all labels required by loony-dev into this repo."""
        logger.info("Provisioning required labels for %s", self.name)
        for label in REQUIRED_LABELS:
            self.ensure_label(**label)

    # --- Milestones ---

    def list_milestones(self, ttl: float = 3600.0) -> dict[str, datetime | None]:
        """Return ``{title: due_on}`` for all open milestones.

        Cached for *ttl* seconds (default 1 hour) since milestones change
        infrequently.  Call ``invalidate_milestones_cache()`` after mutations.
        """
        now = time.monotonic()
        if self._milestones_cache is not None:
            cached_ms, cached_at = self._milestones_cache
            if now - cached_at < ttl:
                logger.debug("list_milestones() cache hit (%d milestones)", len(cached_ms))
                return cached_ms

        try:
            data = self.client.gh_api("milestones?state=open&per_page=100")
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch milestones for %s", self.name)
            data = []

        result: dict[str, datetime | None] = {}
        if isinstance(data, list):
            for ms in data:
                title = ms.get("title", "")
                result[title] = parse_datetime(ms.get("due_on"))

        self._milestones_cache = (result, now)
        logger.debug("list_milestones() fetched %d milestone(s)", len(result))
        return result

    def invalidate_milestones_cache(self) -> None:
        """Discard the cached milestone list."""
        self._milestones_cache = None

    # --- All-issues cache ---

    def list_issues_all(self, ttl: float = 120.0) -> list[dict]:
        """Return all open issues with labels, milestone, and timestamps.

        Cached for *ttl* seconds (default 2 min).  Call
        ``invalidate_issues_cache()`` after mutations that change issue state.

        User-controlled string fields are sanitized for prompt injection.
        """
        now = time.monotonic()
        if self._issues_all_cache is not None:
            cached_issues, cached_at = self._issues_all_cache
            if now - cached_at < ttl:
                logger.debug("list_issues_all() cache hit (%d issues)", len(cached_issues))
                return cached_issues

        try:
            data = self.client.gh_json(
                "issue", "list",
                "--state", "open",
                "--json", "number,title,body,labels,milestone,createdAt,updatedAt,author",
                "--limit", "500",
            )
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch all issues for %s", self.name)
            data = []

        if not isinstance(data, list):
            data = []

        from loony_dev.sanitize import sanitize_user_content
        result = []
        for item in data:
            sanitized = dict(item)
            sanitized["title"] = sanitize_user_content(item.get("title", "")).text
            sanitized["body"] = sanitize_user_content(item.get("body") or "").text
            result.append(sanitized)

        self._issues_all_cache = (result, now)
        logger.debug("list_issues_all() fetched %d open issue(s)", len(result))
        return result

    def invalidate_issues_cache(self) -> None:
        """Discard the cached all-issues list after a mutation."""
        self._issues_all_cache = None

    # --- Open PR → issue number mapping ---

    def get_open_pr_issue_numbers(self) -> set[int]:
        """Return the set of issue numbers that have an associated open PR.

        Parses PR titles and branch names for ``#N`` patterns.  Tick-cached.
        """
        cache_key = "open_pr_issue_numbers"
        cached = self._tick_cache.get(cache_key)
        if cached is not None:
            logger.debug("get_open_pr_issue_numbers() tick-cache hit (%d)", len(cached))
            return cached

        issue_numbers: set[int] = set()
        ref_pattern = re.compile(r"#(\d+)")
        for pr in self.list_open_prs():
            for match in ref_pattern.finditer(pr.get("title", "")):
                issue_numbers.add(int(match.group(1)))
            for match in re.finditer(r"(?:^|[/-])(\d+)(?:[/-]|$)", pr.get("headRefName", "")):
                issue_numbers.add(int(match.group(1)))

        self._tick_cache[cache_key] = issue_numbers
        logger.debug("get_open_pr_issue_numbers() found %d issue(s) with open PRs", len(issue_numbers))
        return issue_numbers

    # --- Branch health ---

    def get_branch_check_runs(self, sha: str) -> list[dict]:
        """Return all check runs for *sha* with name/status/conclusion fields.

        Used to health-check the default branch.  Tick-cached per SHA.
        """
        cache_key = f"branch_checks:{sha}"
        cached = self._tick_cache.get(cache_key)
        if cached is not None:
            logger.debug("get_branch_check_runs(%r) tick-cache hit", sha)
            return cached

        try:
            data = self.client.gh_api(f"commits/{sha}/check-runs")
            runs = []
            if isinstance(data, dict):
                for run in data.get("check_runs", []):
                    runs.append({
                        "name": run.get("name", ""),
                        "status": run.get("status", ""),
                        "conclusion": run.get("conclusion") or "",
                    })
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch check runs for SHA %r", sha)
            runs = []

        self._tick_cache[cache_key] = runs
        logger.debug("get_branch_check_runs(%r) returned %d run(s)", sha, len(runs))
        return runs

    def get_default_branch_sha(self) -> str:
        """Return the HEAD commit SHA of the repository's default branch."""
        branch = self.detect_default_branch()
        try:
            output = self.client.gh(
                "api", f"repos/{self.name}/branches/{branch}",
                "-q", ".commit.sha",
            )
            return output.strip()
        except subprocess.CalledProcessError:
            logger.warning("Failed to get HEAD SHA for branch %r", branch)
            return ""

    # --- Deployment workflow ---

    def get_deployment_run(self, workflow: str, after_timestamp: datetime) -> bool:
        """Return True if a successful *workflow* run completed after *after_timestamp*.

        *workflow* should be the workflow file name without extension (e.g. ``"deploy"``).
        """
        try:
            data = self.client.gh_api(
                f"actions/workflows/{workflow}.yml/runs?status=success&per_page=10"
            )
            if not isinstance(data, dict):
                return False
            for run in data.get("workflow_runs", []):
                completed_at = parse_datetime(run.get("completed_at"))
                if completed_at and completed_at > after_timestamp:
                    logger.debug(
                        "get_deployment_run(%r): found successful run completed at %s",
                        workflow, completed_at,
                    )
                    return True
            return False
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch deployment workflow runs for %r", workflow)
            return False

    # --- PR merging ---

    def get_pr_merged_at(self, pr_number: int) -> datetime | None:
        """Return the merged-at timestamp if PR *pr_number* was merged, else ``None``."""
        try:
            output = self.client.gh(
                "pr", "view", str(pr_number),
                "--json", "state,merged,mergedAt",
                "-q", ".",
            )
            data = json.loads(output)
            if data.get("merged"):
                return parse_datetime(data.get("mergedAt"))
        except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError):
            logger.warning("Failed to fetch merge state for PR #%d", pr_number)
        return None

    def merge_pull_request(self, pr_number: int, merge_method: str = "squash") -> bool:
        """Merge PR *pr_number* using *merge_method*.

        Returns ``True`` on success and invalidates the issues and open-PRs caches.
        """
        try:
            self.client.gh("pr", "merge", str(pr_number), f"--{merge_method}")
            logger.info("Merged PR #%d via %s", pr_number, merge_method)
            self.invalidate_issues_cache()
            self._tick_cache.pop("open_prs_raw", None)
            self._tick_cache.pop("open_pr_issue_numbers", None)
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to merge PR #%d: %s",
                pr_number, ((exc.stderr or "") + (exc.stdout or "")).strip()[:300],
            )
            return False

    # --- Compat helpers for project_manager ---

    def list_open_prs(self) -> list[dict]:
        """Return all open PRs as raw dicts.  Results are tick-cached."""
        cached = self._tick_cache.get("open_prs_raw")
        if cached is not None:
            logger.debug("list_open_prs() tick-cache hit (%d PRs)", len(cached))
            return cached
        items = self.client.gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,headRefOid,title,labels,mergeable,updatedAt,assignees,isDraft",
        )
        if not isinstance(items, list):
            items = []
        logger.debug("list_open_prs() returned %d open PR(s)", len(items))
        self._tick_cache["open_prs_raw"] = items
        return items

    def add_label(self, number: int, label: str) -> None:
        try:
            self.client.gh("issue", "edit", str(number), "--add-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to add label '%s' to #%d", label, number)

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.client.gh("issue", "edit", str(number), "--remove-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to remove label '%s' from #%d", label, number)

    def post_comment(self, number: int, body: str) -> None:
        from loony_dev.models import truncate_for_log
        logger.debug("post_comment(#%d): %s", number, truncate_for_log(body))
        self.client.gh("issue", "comment", str(number), "--body", body)

    def get_issue_comments(self, number: int) -> list:
        """Get comments on an issue, sorted by creation time."""
        from loony_dev.github.comment import Comment
        return Comment.list_for_issue(number, repo=self)

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the PR number for a PR that references the given issue, or None."""
        for search_args in [
            ["--search", f"#{issue_number} in:title"],
            ["--search", f"#{issue_number}"],
        ]:
            try:
                data = self.client.gh_json(
                    "pr", "list",
                    "--state", "all",
                    *search_args,
                    "--json", "number,createdAt",
                )
                if data:
                    sorted_prs = sorted(data, key=lambda p: p.get("createdAt", ""), reverse=True)
                    return sorted_prs[0]["number"]
            except subprocess.CalledProcessError:
                logger.warning("gh pr list search failed for issue #%d", issue_number)
        return None

    def get_pr_check_runs(self, head_sha: str) -> list:
        """Return completed failing check runs for the given commit SHA."""
        from loony_dev.github.check_run import CheckRun
        return CheckRun.list_failing(head_sha, repo=self)
