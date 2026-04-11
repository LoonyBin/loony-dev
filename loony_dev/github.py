from __future__ import annotations

import functools
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loony_dev.models import CheckRun, Comment, Issue, truncate_for_log
from loony_dev.sanitize import InjectionType, sanitize_user_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

# GitHub permission levels from lowest to highest.
_ROLE_HIERARCHY = ["none", "read", "triage", "write", "admin"]

# Roles that are authorized when min_role="triage" (the default).
_DEFAULT_AUTHORIZED_ROLES = {"admin", "write", "triage"}

# Defaults for the [github] config section.  Used when a key is absent from
# config.settings["github"] (or when settings haven't been populated yet).
_DEFAULTS: dict[str, int | float] = {
    "permission_cache_ttl": 600,       # seconds
    "check_runs_cache_ttl": 3600,      # seconds (1 hour)
    "max_retries": 5,
    "initial_backoff": 2.0,            # seconds
}

_GH_RATE_LIMIT_PATTERNS = ("rate limit", "abuse detection", "secondary rate", "403", "429")


def _gh_setting(key: str) -> int | float:
    """Read a ``[github]`` config value, falling back to ``_DEFAULTS``."""
    from loony_dev import config
    section = config.settings.get("github")
    if isinstance(section, dict) and key in section:
        return type(_DEFAULTS[key])(section[key])
    return _DEFAULTS[key]


@dataclass
class _CheckRunsCacheEntry:
    failing_runs: list[CheckRun]
    all_completed: bool  # True iff every check run had status=="completed"
    cached_at: float  # time.monotonic()


def _is_retryable_gh_error(exc: subprocess.CalledProcessError) -> bool:
    """Return True if the gh CLI error looks like a rate-limit or transient server error."""
    combined = ((exc.stdout or "") + (exc.stderr or "")).lower()
    return any(p in combined for p in _GH_RATE_LIMIT_PATTERNS)


def _run_gh(*cmd: str) -> str:
    """Run a gh CLI command with retry and exponential backoff on rate-limit errors.

    Reads ``max_retries`` and ``initial_backoff`` from the ``[github]`` config
    section.  Non-retryable errors are raised immediately.
    """
    max_retries = int(_gh_setting("max_retries"))
    logger.debug("Running: %s", " ".join(cmd))
    backoff = float(_gh_setting("initial_backoff"))
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            if attempt < max_retries and _is_retryable_gh_error(exc):
                logger.warning(
                    "gh rate-limited (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries + 1, backoff,
                    (exc.stderr or exc.stdout or "").strip()[:200],
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _roles_at_or_above(min_role: str) -> set[str]:
    """Return the set of role names that are >= min_role in the hierarchy."""
    try:
        idx = _ROLE_HIERARCHY.index(min_role)
    except ValueError:
        logger.warning("Unknown min_role %r; defaulting to 'triage'", min_role)
        idx = _ROLE_HIERARCHY.index("triage")
    return set(_ROLE_HIERARCHY[idx:])


def is_authorized(
    github: GitHubClient,
    username: str,
) -> bool:
    """Return True if *username* is authorized to trigger agent runs.

    A user is authorized if they are in the explicit *allowed_users* set or if
    their repository permission level is at or above *min_role*.
    """
    if username in github.allowed_users:
        return True
    permission = github.get_user_permission(username)
    return permission in _roles_at_or_above(github.min_role)


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string from the GitHub API into a UTC-aware datetime."""
    if not value:
        return None
    # GitHub returns strings like "2024-01-15T10:30:00Z"; replace Z for Python 3.10 compat.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


CI_FAILURE_MARKER = "<!-- loony-ci-failure -->"

INJECTION_WARNING_SENTINEL = "<!-- loonybin-injection-warning field="

REQUIRED_LABELS = [
    {"name": "ready-for-development", "color": "0075ca", "description": "Issue is ready for implementation"},
    {"name": "ready-for-planning",    "color": "e4e669", "description": "Issue needs planning/triage"},
    {"name": "in-progress",           "color": "d93f0b", "description": "Bot is actively working on this"},
]


class GitHubClient:
    def __init__(
        self,
        repo: str,
        bot_name: str | None = None,
        allowed_users: set[str] | None = None,
        min_role: str | None = None,
        skip_ci_checks: set[str] | None = None,
    ) -> None:
        from loony_dev import config
        self.repo = repo
        self.bot_name = bot_name or config.settings.get("bot_name") or self.detect_bot_name()
        self.allowed_users: set[str] = (
            allowed_users if allowed_users is not None
            else set(config.settings.get("allowed_users") or [])
        )
        self.min_role = min_role or config.settings.get("min_role") or "triage"
        self.skip_ci_checks: set[str] = (
            skip_ci_checks if skip_ci_checks is not None
            else set(config.settings.get("skip_ci_checks") or [])
        )
        # Cache: username -> (permission_level, monotonic_timestamp)
        self._permission_cache: dict[str, tuple[str | None, float]] = {}
        # Tick-scoped cache: cleared at the start of each tick
        self._tick_cache: dict[str, Any] = {}
        # Cross-tick cache: head_sha -> _CheckRunsCacheEntry
        self._check_runs_cache: dict[str, _CheckRunsCacheEntry] = {}

    def _gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout (with retry on rate-limit)."""
        cmd = ["gh", *args]
        if args and args[0] != "api":
            cmd += ["-R", self.repo]
        return _run_gh(*cmd)

    def _gh_api(self, endpoint: str) -> list | dict:
        """Call gh api for this repo and parse JSON output."""
        output = self._gh("api", f"repos/{self.repo}/{endpoint}")
        if not output:
            return []
        return json.loads(output)

    def _gh_json(self, *args: str) -> list | dict:
        """Run a gh CLI command and parse JSON output."""
        output = self._gh(*args)
        if not output:
            return []
        return json.loads(output)

    # --- Prompt injection defense ---

    def _sanitize_field(
        self,
        value: str | None,
        field_name: str,
        item_type: str,
        item_number: int,
    ) -> str:
        """Sanitize a single user-controlled string field.

        If hidden content is detected, a WARNING is logged and a reply comment
        is posted to the relevant issue/PR so maintainers are alerted.
        """
        result = sanitize_user_content(value)
        if result.has_injections:
            injection_labels = ", ".join(i.value for i in result.injections)
            logger.warning(
                "Potential prompt injection detected in %s #%d field=%r types=[%s] — content stripped",
                item_type,
                item_number,
                field_name,
                injection_labels,
            )
            self._post_injection_warning(item_number, field_name, result.injections)
        return result.text

    def _injection_warning_exists(self, item_number: int, field_name: str) -> bool:
        """Return True if a warning comment for *field_name* has already been posted."""
        comments = self.get_issue_comments(item_number)
        sentinel = f'{INJECTION_WARNING_SENTINEL}"{field_name}" -->'
        return any(sentinel in c.body for c in comments)

    def _post_injection_warning(
        self,
        number: int,
        field_name: str,
        injections: list[InjectionType],
    ) -> None:
        """Post a GitHub comment warning maintainers of detected hidden content."""
        if self._injection_warning_exists(number, field_name):
            return
        injection_labels = ", ".join(f"`{i.value}`" for i in injections)
        sentinel = f'{INJECTION_WARNING_SENTINEL}"{field_name}" -->'
        body = (
            f"{sentinel}\n"
            "> [!WARNING]\n"
            "> **Potential prompt injection attempt detected.**\n"
            ">\n"
            f"> Hidden content was found in the **{field_name}** field of this item "
            f"(detected: {injection_labels}).\n"
            "> The hidden content was stripped before processing and did not reach the AI agent.\n"
            ">\n"
            "> This may indicate a malicious actor attempting to hijack the AI agent. "
            "> A human should review the original content of this item."
        )
        try:
            self.post_comment(number, body)
        except Exception as exc:
            logger.warning(
                "Failed to post injection warning comment on #%d: %s", number, exc
            )

    # --- Authorization ---

    def get_user_permission(self, username: str) -> str | None:
        """Return the user's repository permission level, or None if not a collaborator.

        Possible values: 'admin', 'write', 'triage', 'read', 'none'.
        Results are cached for ``permission_cache_ttl`` seconds (see ``[github]`` config).
        """
        now = time.monotonic()
        if username in self._permission_cache:
            cached_perm, cached_at = self._permission_cache[username]
            if now - cached_at < _gh_setting("permission_cache_ttl"):
                logger.debug("Permission cache hit for %r: %r", username, cached_perm)
                return cached_perm

        try:
            output = self._gh(
                "api",
                f"repos/{self.repo}/collaborators/{username}/permission",
                "-q", ".permission",
            )
            permission: str | None = output if output else None
        except subprocess.CalledProcessError:
            # 404 means the user is not a collaborator.
            permission = None

        self._permission_cache[username] = (permission, now)
        logger.debug("Permission for %r in %s: %r (cached)", username, self.repo, permission)
        return permission

    def evict_stale_permission_cache(self) -> None:
        """Remove expired entries from the permission cache."""
        now = time.monotonic()
        stale = [u for u, (_, ts) in self._permission_cache.items() if now - ts >= _gh_setting("permission_cache_ttl")]
        for u in stale:
            del self._permission_cache[u]
        if stale:
            logger.debug("Evicted %d stale permission cache entries", len(stale))

    # --- Tick-scoped cache ---

    def clear_tick_cache(self) -> None:
        """Discard all per-tick cached data. Call at the start of each tick."""
        self._tick_cache.clear()

    # --- Cross-tick check-runs cache ---

    def evict_stale_check_runs_cache(self) -> None:
        """Remove expired entries from the check-runs cache."""
        now = time.monotonic()
        stale = [sha for sha, entry in self._check_runs_cache.items() if now - entry.cached_at >= _gh_setting("check_runs_cache_ttl")]
        for sha in stale:
            del self._check_runs_cache[sha]
        if stale:
            logger.debug("Evicted %d stale check-runs cache entries", len(stale))

    # --- Issues ---

    def list_issues(self, label: str) -> list[tuple[Issue, list[str]]]:
        """Return open issues with the given label, along with their label names."""
        data = self._gh_json(
            "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,author,updatedAt",
        )
        result = []
        for item in data:
            number = item["number"]
            result.append((
                Issue(
                    number=number,
                    title=self._sanitize_field(item["title"], "title", "issue", number),
                    body=self._sanitize_field(item.get("body", ""), "body", "issue", number),
                    author=item.get("author", {}).get("login", ""),
                    updated_at=_parse_datetime(item.get("updatedAt")),
                ),
                [l["name"] for l in item.get("labels", [])],
            ))
        logger.debug("list_issues(label=%r) returned %d issue(s)", label, len(result))
        return result

    def get_issue_comments(self, number: int) -> list[Comment]:
        """Get all comments on an issue, sorted by creation time."""
        data = self._gh_json("issue", "view", str(number), "--json", "comments")
        if not isinstance(data, dict):
            return []
        comments = []
        for c in data.get("comments", []):
            author = c.get("author", {}).get("login", "")
            body = c.get("body", "")
            if author != self.bot_name:
                body = self._sanitize_field(body, "body", "issue", number)
            comments.append(Comment(author=author, body=body, created_at=c.get("createdAt", "")))
        comments.sort(key=lambda c: c.created_at)
        logger.debug("get_issue_comments(#%d) returned %d comment(s)", number, len(comments))
        return comments

    # --- Pull Requests ---

    def list_open_prs(self) -> list[dict]:
        """Fetch all open PRs with their labels, comments, and reviews.

        User-controlled string fields (title, comment/review bodies) are
        sanitized for prompt injection before being returned.

        Results are cached for the duration of the current tick (cleared by
        ``clear_tick_cache()`` at the start of each tick).
        """
        cached = self._tick_cache.get("open_prs")
        if cached is not None:
            logger.debug("list_open_prs() tick-cache hit (%d PRs)", len(cached))
            return cached
        items = self._gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,headRefOid,title,comments,reviews,labels,mergeable,updatedAt,assignees",
        )
        sanitized = []
        for item in items:
            pr_number = item["number"]
            item = dict(item)  # shallow copy so we don't mutate cached data
            item["title"] = self._sanitize_field(item.get("title", ""), "title", "pr", pr_number)
            item["comments"] = [
                {**c, "body": (
                    c.get("body", "") if c.get("author", {}).get("login", "") == self.bot_name
                    else self._sanitize_field(c.get("body", ""), "body", "pr", pr_number)
                )}
                for c in item.get("comments", [])
            ]
            item["reviews"] = [
                {**r, "body": (
                    r.get("body", "") if r.get("author", {}).get("login", "") == self.bot_name
                    else self._sanitize_field(r.get("body", ""), "body", "pr", pr_number)
                )}
                for r in item.get("reviews", [])
            ]
            sanitized.append(item)
        logger.debug("list_open_prs() returned %d open PR(s)", len(sanitized))
        self._tick_cache["open_prs"] = sanitized
        return sanitized

    def is_assigned_to_bot(self, pr: dict) -> bool:
        """Return True if the bot is listed as an assignee on the given PR dict."""
        return any(
            a.get("login", "") == self.bot_name
            for a in pr.get("assignees", [])
        )

    def get_pr_inline_comments(self, pr_number: int) -> list[Comment]:
        """Fetch inline review comments for a PR.

        Uses each comment's associated review ``submitted_at`` as the effective
        ``created_at`` timestamp. Inline comments are drafted before a review is
        submitted, so their own ``created_at`` may pre-date the bot's last
        SUCCESS_MARKER even though the review was submitted afterwards. Using
        ``submitted_at`` ensures correct ordering relative to the marker.
        """
        try:
            data = self._gh_api(f"pulls/{pr_number}/comments")
            data_reviews = self._gh_api(f"pulls/{pr_number}/reviews")
            review_submitted_at: dict[int, str] = {}
            if isinstance(data_reviews, list):
                review_submitted_at = {
                    r["id"]: r["submitted_at"]
                    for r in data_reviews
                    if r.get("submitted_at")
                }
            logger.debug(
                "get_pr_inline_comments(#%d) fetched %d review(s)",
                pr_number, len(review_submitted_at),
            )
            if isinstance(data, list):
                comments = []
                for c in data:
                    author = c.get("user", {}).get("login", "")
                    body = c.get("body", "")
                    if author != self.bot_name:
                        body = self._sanitize_field(body, "body", "pr", pr_number)
                    review_id = c.get("pull_request_review_id")
                    effective_ts = (
                        review_submitted_at.get(review_id)
                        if review_id is not None
                        else None
                    ) or c.get("created_at", "")
                    comments.append(Comment(
                        author=author,
                        body=body,
                        created_at=effective_ts,
                        path=c.get("path"),
                        line=c.get("line"),
                    ))
                logger.debug("get_pr_inline_comments(#%d) returned %d comment(s)", pr_number, len(comments))
                return comments
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch inline review comments for PR #%d", pr_number)
        return []

    def get_pr_check_runs(self, head_sha: str) -> list[CheckRun]:
        """Return completed failing check runs for the given commit SHA.

        Filters for status == "completed" and conclusion in ("failure", "timed_out").
        Excludes checks whose name is in self.skip_ci_checks.

        Results for SHAs where all checks have completed are cached for
        ``check_runs_cache_ttl`` seconds across ticks (see ``[github]`` config).
        """
        now = time.monotonic()
        entry = self._check_runs_cache.get(head_sha)
        if entry is not None and entry.all_completed and now - entry.cached_at < _gh_setting("check_runs_cache_ttl"):
            logger.debug("get_pr_check_runs(%r) cache hit (%d failing)", head_sha, len(entry.failing_runs))
            return entry.failing_runs

        try:
            data = self._gh_api(f"commits/{head_sha}/check-runs")
            if not isinstance(data, dict):
                return []
            all_runs = data.get("check_runs", [])
            all_completed = all(r.get("status") == "completed" for r in all_runs)
            runs = []
            for run in all_runs:
                name = run.get("name", "")
                if name in self.skip_ci_checks:
                    continue
                status = run.get("status", "")
                conclusion = run.get("conclusion") or ""
                if status == "completed" and conclusion in ("failure", "timed_out"):
                    runs.append(CheckRun(
                        name=name,
                        status=status,
                        conclusion=conclusion,
                        details_url=run.get("details_url", run.get("html_url", "")),
                    ))
            self._check_runs_cache[head_sha] = _CheckRunsCacheEntry(
                failing_runs=runs,
                all_completed=all_completed,
                cached_at=now,
            )
            logger.debug("get_pr_check_runs(%r) returned %d failing run(s) (all_completed=%s)", head_sha, len(runs), all_completed)
            return runs
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch check runs for SHA %r", head_sha)
            return []

    def find_pr_for_issue(self, issue_number: int) -> int | None:
        """Return the PR number for a PR that references the given issue, or None."""
        for search_args in [
            ["--search", f"#{issue_number} in:title"],
            ["--search", f"#{issue_number}"],
        ]:
            try:
                data = self._gh_json(
                    "pr", "list",
                    "--state", "open",
                    *search_args,
                    "--json", "number,createdAt",
                )
                if data:
                    sorted_prs = sorted(data, key=lambda p: p.get("createdAt", ""), reverse=True)
                    return sorted_prs[0]["number"]
            except subprocess.CalledProcessError:
                logger.warning("gh pr list search failed for issue #%d", issue_number)
        return None

    def add_pr_reviewer(self, pr_number: int, reviewer: str) -> None:
        """Request a review from reviewer on the given PR."""
        try:
            self._gh("pr", "edit", str(pr_number), "--add-reviewer", reviewer)
            logger.debug("add_pr_reviewer(#%d, %r) succeeded", pr_number, reviewer)
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Failed to add reviewer %r to PR #%d: %s", reviewer, pr_number, e
            )
            raise

    # --- Labels ---

    def add_label(self, number: int, label: str) -> None:
        try:
            self._gh("issue", "edit", str(number), "--add-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to add label '%s' to #%d", label, number)

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._gh("issue", "edit", str(number), "--remove-label", label)
        except subprocess.CalledProcessError:
            logger.warning("Failed to remove label '%s' from #%d", label, number)

    def assign_self(self, number: int) -> None:
        try:
            self._gh("issue", "edit", str(number), "--add-assignee", "@me")
        except subprocess.CalledProcessError:
            logger.warning("Failed to assign self to #%d", number)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create label if it doesn't exist. Silently ignores conflicts (422)."""
        try:
            self._gh(
                "api", f"repos/{self.repo}/labels",
                "--method", "POST",
                "-f", f"name={name}",
                "-f", f"color={color}",
                "-f", f"description={description}",
            )
            logger.debug("Created label %r in %s", name, self.repo)
        except subprocess.CalledProcessError as e:
            output = (e.stderr or "") + (e.stdout or "")
            if "already_exists" in output or "422" in output:
                logger.debug("Label %r already exists in %s", name, self.repo)
            else:
                logger.warning(
                    "Failed to provision label %r in %s: %s",
                    name, self.repo, (e.stderr or "").strip(),
                )

    def ensure_required_labels(self) -> None:
        """Provision all labels required by loony-dev into this repo."""
        logger.info("Provisioning required labels for %s", self.repo)
        for label in REQUIRED_LABELS:
            self.ensure_label(**label)

    # --- Comments ---

    def post_comment(self, number: int, body: str) -> None:
        logger.debug("post_comment(#%d): %s", number, truncate_for_log(body))
        self._gh("issue", "comment", str(number), "--body", body)

    # --- Repo detection ---

    @staticmethod
    def detect_repo() -> str:
        """Detect owner/repo from git remote URL."""
        return _run_gh("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def detect_bot_name() -> str:
        """Detect the authenticated GitHub user's login via the gh CLI."""
        return _run_gh("gh", "api", "user", "-q", ".login")

    def detect_default_branch(self) -> str:
        """Detect the repository's default branch via the gh CLI.

        Returns the default branch name (e.g. ``main``, ``master``,
        ``development``).  Falls back to ``"main"`` if detection fails.
        """
        try:
            branch = _run_gh(
                "gh", "repo", "view", self.repo,
                "--json", "defaultBranchRef",
                "-q", ".defaultBranchRef.name",
            )
            if branch:
                return branch
        except subprocess.CalledProcessError:
            logger.warning("Failed to detect default branch for %s; falling back to 'main'", self.repo)
        return "main"
