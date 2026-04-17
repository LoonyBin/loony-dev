"""Repository-level operations: detection, authorization, labels, and caching."""
from __future__ import annotations

import functools
import logging
import subprocess
import time
from dataclasses import dataclass
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
# Caching decorators for Repo instance methods
# ---------------------------------------------------------------------------


def tick_cached(method):
    """Cache a Repo instance method result for the current tick.

    The cached value is stored in ``self._tick_cache`` under the method name
    and cleared by ``Repo.clear_tick_cache()`` at the start of each tick.
    """
    @functools.wraps(method)
    def wrapper(self, *args):
        key = method.__name__ if not args else (method.__name__, *args)
        if key in self._tick_cache:
            return self._tick_cache[key]
        result = method(self, *args)
        self._tick_cache[key] = result
        return result
    return wrapper


def ttl_cached(default_ttl: float):
    """Cache a Repo instance method result with a time-to-live (seconds).

    The cached value is stored in ``self._ttl_cache`` under the method name.
    Stale entries are replaced on the next access.
    """
    def decorator(method):
        key = method.__name__
        @functools.wraps(method)
        def wrapper(self):
            now = time.monotonic()
            entry = self._ttl_cache.get(key)
            if entry is not None:
                result, cached_at = entry
                if now - cached_at < default_ttl:
                    return result
            result = method(self)
            self._ttl_cache[key] = (result, now)
            return result
        return wrapper
    return decorator


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
        cwd: str | None = None,
    ) -> None:
        from loony_dev import config

        self.cwd = cwd
        self.name: str = repo or Repo.detect(cwd=cwd)
        self.client = GitHubClient(self.name, cwd=cwd)
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
        # TTL cache: method_name -> (result, monotonic_timestamp)
        self._ttl_cache: dict[str, tuple[Any, float]] = {}
        # Configurable TTL for the milestones cache (seconds); can be overridden by callers.
        self.milestone_cache_ttl: float = 3600.0

    # --- Detection ---

    @staticmethod
    def detect(cwd: str | None = None) -> str:
        """Detect owner/repo from git remote URL.

        Args:
            cwd: Directory to run the detection in. Defaults to the current working directory.
        """
        return run_gh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=cwd)

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
                cwd=self.cwd,
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

    # --- Collection proxies ---

    @property
    def milestones(self) -> dict[str, datetime | None]:
        """Return ``{title: due_on}`` for all open milestones (TTL-cached; see ``milestone_cache_ttl``)."""
        key = "milestones"
        now = time.monotonic()
        entry = self._ttl_cache.get(key)
        if entry is not None:
            result, cached_at = entry
            if now - cached_at < self.milestone_cache_ttl:
                return result
        from loony_dev.github.milestone import Milestone
        result = {ms.title: ms.due_on for ms in Milestone.list_open(repo=self)}
        self._ttl_cache[key] = (result, now)
        return result

    @functools.cached_property
    def issues(self):
        """Query proxy for repository issues (``repo.issues.open``)."""
        from loony_dev.github.issue import _IssueQuery
        return _IssueQuery(self)

    @functools.cached_property
    def pull_requests(self):
        """Collection proxy for repository pull requests (``repo.pull_requests.open``)."""
        from loony_dev.github.pull_request import PullRequestCollection
        return PullRequestCollection(self)

    @property
    @tick_cached
    def default_branch(self):
        """Return the repository's default branch (tick-cached)."""
        from loony_dev.github.branch import Branch
        return Branch(name=self.detect_default_branch(), repo=self)

    # --- Convenience helpers ---

    def add_label(self, number: int, label: str) -> None:
        try:
            self.client.gh("issue", "edit", str(number), "--add-label", label)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to add label '%s' to #%d: %s", label, number, exc)
            raise

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.client.gh("issue", "edit", str(number), "--remove-label", label)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to remove label '%s' from #%d: %s", label, number, exc)
            raise

    def post_comment(self, number: int, body: str) -> None:
        from loony_dev.models import truncate_for_log
        logger.debug("post_comment(#%d): %s", number, truncate_for_log(body))
        self.client.gh("issue", "comment", str(number), "--body", body)

    def get_issue_comments(self, number: int) -> list:
        """Get comments on an issue, sorted by creation time."""
        from loony_dev.github.comment import Comment
        return Comment.list_for_issue(number, repo=self)

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the PR number for a PR that references the given issue, or None.

        Merged PRs are preferred over open/closed ones to avoid hiding the real
        merged PR behind a newer abandoned PR.
        """
        for search_args in [
            ["--search", f"#{issue_number} in:title"],
            ["--search", f"#{issue_number}"],
        ]:
            try:
                data = self.client.gh_json(
                    "pr", "list",
                    "--state", "all",
                    *search_args,
                    "--json", "number,createdAt,state",
                )
                if data:
                    merged = [p for p in data if p.get("state") == "MERGED"]
                    candidates = merged if merged else data
                    sorted_prs = sorted(candidates, key=lambda p: p.get("createdAt", ""), reverse=True)
                    return sorted_prs[0]["number"]
            except subprocess.CalledProcessError:
                logger.warning("gh pr list search failed for issue #%d", issue_number)
        return None
