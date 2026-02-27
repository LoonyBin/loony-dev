from __future__ import annotations

import json
import logging
import subprocess

from loony_dev.models import Comment, Issue, PullRequest

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, repo: str, bot_name: str = "loony-dev[bot]") -> None:
        self.repo = repo
        self.bot_name = bot_name

    def _gh(self, *args: str) -> str:
        """Run a gh CLI command and return stdout."""
        cmd = ["gh", *args, "-R", self.repo]
        logger.debug("Running: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def _gh_json(self, *args: str) -> list | dict:
        """Run a gh CLI command and parse JSON output."""
        output = self._gh(*args)
        if not output:
            return []
        return json.loads(output)

    # --- Issues ---

    def get_ready_issues(self) -> list[Issue]:
        """Get issues labeled 'ready-for-development'."""
        data = self._gh_json(
            "issue", "list",
            "--label", "ready-for-development",
            "--state", "open",
            "--json", "number,title,body",
        )
        return [
            Issue(number=item["number"], title=item["title"], body=item.get("body", ""))
            for item in data
        ]

    # --- Pull Requests ---

    def get_prs_needing_review(self) -> list[PullRequest]:
        """Find PRs with new comments after bot's last comment.

        Excludes PRs already labeled 'in-progress'.
        """
        data = self._gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,headRefName,title,comments,reviews,labels",
        )

        prs = []
        for item in data:
            labels = [l["name"] for l in item.get("labels", [])]
            if "in-progress" in labels:
                continue

            all_comments = self._get_all_comments(item)
            new_comments = self._comments_after_bot_watermark(all_comments)

            if new_comments:
                prs.append(PullRequest(
                    number=item["number"],
                    branch=item["headRefName"],
                    title=item["title"],
                    new_comments=new_comments,
                ))
        return prs

    def _get_all_comments(self, pr_data: dict) -> list[Comment]:
        """Extract all comments (issue comments + review comments) from PR data."""
        comments = []

        for c in pr_data.get("comments", []):
            comments.append(Comment(
                author=c.get("author", {}).get("login", ""),
                body=c.get("body", ""),
                created_at=c.get("createdAt", ""),
            ))

        for review in pr_data.get("reviews", []):
            if review.get("body"):
                comments.append(Comment(
                    author=review.get("author", {}).get("login", ""),
                    body=review.get("body", ""),
                    created_at=review.get("submittedAt", ""),
                ))

        # Also fetch inline review comments via API
        try:
            inline = self._gh_json(
                "api", f"repos/{self.repo}/pulls/{pr_data['number']}/comments",
            )
            if isinstance(inline, list):
                for c in inline:
                    comments.append(Comment(
                        author=c.get("user", {}).get("login", ""),
                        body=c.get("body", ""),
                        created_at=c.get("created_at", ""),
                        path=c.get("path"),
                        line=c.get("line"),
                    ))
        except subprocess.CalledProcessError:
            logger.warning("Failed to fetch inline review comments for PR #%d", pr_data["number"])

        comments.sort(key=lambda c: c.created_at)
        return comments

    def _comments_after_bot_watermark(self, comments: list[Comment]) -> list[Comment]:
        """Return comments that appear after the bot's last comment."""
        bot_last_idx = -1
        for i, c in enumerate(comments):
            if c.author == self.bot_name:
                bot_last_idx = i

        if bot_last_idx == -1:
            # Bot has never commented — all non-bot comments are "new"
            return [c for c in comments if c.author != self.bot_name]

        return [
            c for c in comments[bot_last_idx + 1:]
            if c.author != self.bot_name
        ]

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
