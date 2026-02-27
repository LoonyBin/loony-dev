from __future__ import annotations

import json
import logging
import subprocess

from loony_dev.models import Comment, Issue, truncate_for_log

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, repo: str, bot_name: str) -> None:
        self.repo = repo
        self.bot_name = bot_name

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

    # --- Issues ---

    def list_issues(self, label: str) -> list[tuple[Issue, list[str]]]:
        """Return open issues with the given label, along with their label names."""
        data = self._gh_json(
            "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels",
        )
        result = [
            (
                Issue(number=item["number"], title=item["title"], body=item.get("body", "")),
                [l["name"] for l in item.get("labels", [])],
            )
            for item in data
        ]
        logger.debug("list_issues(label=%r) returned %d issue(s)", label, len(result))
        return result

    def get_issue_comments(self, number: int) -> list[Comment]:
        """Get all comments on an issue, sorted by creation time."""
        data = self._gh_json("issue", "view", str(number), "--json", "comments")
        if not isinstance(data, dict):
            return []
        comments = [
            Comment(
                author=c.get("author", {}).get("login", ""),
                body=c.get("body", ""),
                created_at=c.get("createdAt", ""),
            )
            for c in data.get("comments", [])
        ]
        comments.sort(key=lambda c: c.created_at)
        logger.debug("get_issue_comments(#%d) returned %d comment(s)", number, len(comments))
        return comments

    # --- Pull Requests ---

    def list_open_prs(self) -> list[dict]:
        """Fetch all open PRs with their labels, comments, and reviews."""
        result = self._gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,title,comments,reviews,labels,mergeable",
        )
        logger.debug("list_open_prs() returned %d open PR(s)", len(result))
        return result

    def get_pr_inline_comments(self, pr_number: int) -> list[Comment]:
        """Fetch inline review comments for a PR."""
        try:
            data = self._gh_api(f"pulls/{pr_number}/comments")
            if isinstance(data, list):
                comments = [
                    Comment(
                        author=c.get("user", {}).get("login", ""),
                        body=c.get("body", ""),
                        created_at=c.get("created_at", ""),
                        path=c.get("path"),
                        line=c.get("line"),
                    )
                    for c in data
                ]
                logger.debug("get_pr_inline_comments(#%d) returned %d comment(s)", pr_number, len(comments))
                return comments
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch inline review comments for PR #%d", pr_number)
        return []

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
