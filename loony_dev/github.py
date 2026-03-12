from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone

from loony_dev import config
from loony_dev.models import Comment, Issue, truncate_for_log
from loony_dev.sanitize import InjectionType, sanitize_user_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

# GitHub permission levels from lowest to highest.
_ROLE_HIERARCHY = ["none", "read", "triage", "write", "admin"]

# Roles that are authorized when min_role="triage" (the default).
_DEFAULT_AUTHORIZED_ROLES = {"admin", "write", "triage"}



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

    A user is authorized if they are in the explicit *allowed_users* set (from
    config) or if their repository permission level is at or above *min_role*.
    """
    allowed_users = set(config.settings.get("ALLOWED_USERS", []))
    min_role = config.settings.get("MIN_ROLE", "triage")
    if username in allowed_users:
        return True
    permission = github.get_user_permission(username)
    return permission in _roles_at_or_above(min_role)


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string from the GitHub API into a UTC-aware datetime."""
    if not value:
        return None
    # GitHub returns strings like "2024-01-15T10:30:00Z"; replace Z for Python 3.10 compat.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
    ) -> None:
        self.repo = repo
        self.bot_name = bot_name or config.settings.get("BOT_NAME", "")
        # Cache: username -> (permission_level, monotonic_timestamp)
        self._permission_cache: dict[str, tuple[str | None, float]] = {}

    def _gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout."""
        cmd = ["gh", *args]
        if args and args[0] != "api":
            cmd += ["-R", self.repo]
        logger.debug("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

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

    def _post_injection_warning(
        self,
        number: int,
        field_name: str,
        injections: list[InjectionType],
    ) -> None:
        """Post a GitHub comment warning maintainers of detected hidden content."""
        injection_labels = ", ".join(f"`{i.value}`" for i in injections)
        body = (
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
        Results are cached for ``config.settings.PERMISSION_CACHE_TTL`` seconds.
        """
        cache_ttl = config.settings.PERMISSION_CACHE_TTL
        now = time.monotonic()
        if username in self._permission_cache:
            cached_perm, cached_at = self._permission_cache[username]
            if now - cached_at < cache_ttl:
                logger.debug("Permission cache hit for %r: %r", username, cached_perm)
                return cached_perm

        try:
            output = subprocess.run(
                [
                    "gh", "api",
                    f"repos/{self.repo}/collaborators/{username}/permission",
                    "-q", ".permission",
                ],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            permission: str | None = output if output else None
        except subprocess.CalledProcessError:
            # 404 means the user is not a collaborator.
            permission = None

        self._permission_cache[username] = (permission, now)
        logger.debug("Permission for %r in %s: %r (cached)", username, self.repo, permission)
        return permission

    def evict_stale_permission_cache(self) -> None:
        """Remove expired entries from the permission cache."""
        cache_ttl = config.settings.PERMISSION_CACHE_TTL
        now = time.monotonic()
        stale = [u for u, (_, ts) in self._permission_cache.items() if now - ts >= cache_ttl]
        for u in stale:
            del self._permission_cache[u]
        if stale:
            logger.debug("Evicted %d stale permission cache entries", len(stale))

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
        """
        items = self._gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,title,comments,reviews,labels,mergeable,updatedAt",
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
        return sanitized

    def get_pr_inline_comments(self, pr_number: int) -> list[Comment]:
        """Fetch inline review comments for a PR."""
        try:
            data = self._gh_api(f"pulls/{pr_number}/comments")
            if isinstance(data, list):
                comments = []
                for c in data:
                    author = c.get("user", {}).get("login", "")
                    body = c.get("body", "")
                    if author != self.bot_name:
                        body = self._sanitize_field(body, "body", "pr", pr_number)
                    comments.append(Comment(
                        author=author,
                        body=body,
                        created_at=c.get("created_at", ""),
                        path=c.get("path"),
                        line=c.get("line"),
                    ))
                logger.debug("get_pr_inline_comments(#%d) returned %d comment(s)", pr_number, len(comments))
                return comments
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch inline review comments for PR #%d", pr_number)
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

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create label if it doesn't exist. Silently ignores conflicts (422)."""
        try:
            result = subprocess.run(
                [
                    "gh", "api", f"repos/{self.repo}/labels",
                    "--method", "POST",
                    "-f", f"name={name}",
                    "-f", f"color={color}",
                    "-f", f"description={description}",
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                logger.debug("Created label %r in %s", name, self.repo)
                return
            # A 422 response means the label already exists — parse to confirm.
            try:
                body = json.loads(result.stdout)
                errors = body.get("errors", [])
                if any(e.get("code") == "already_exists" for e in errors):
                    logger.debug("Label %r already exists in %s", name, self.repo)
                    return
            except (ValueError, AttributeError):
                pass
            logger.warning(
                "Failed to provision label %r in %s: %s",
                name, self.repo, (result.stderr or result.stdout).strip(),
            )
        except Exception as exc:
            logger.warning("Failed to provision label %r in %s: %s", name, self.repo, exc)

    def ensure_required_labels(self) -> None:
        """Provision all labels required by loony-dev into this repo."""
        logger.info("Provisioning required labels for %s", self.repo)
        for label in REQUIRED_LABELS:
            self.ensure_label(**label)

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
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def detect_bot_name() -> str:
        """Detect the authenticated GitHub user's login via the gh CLI."""
        result = subprocess.run(
            ["gh", "api", "user", "-q", ".login"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
