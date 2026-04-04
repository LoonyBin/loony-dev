from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import Comment, truncate_for_log
from loony_dev.tasks.base import Task

if TYPE_CHECKING:
    from loony_dev.github import GitHubClient
    from loony_dev.models import Issue, TaskResult

logger = logging.getLogger(__name__)

DESIGN_MARKER = "<!-- loony-design -->"

# Matches markdown images: ![alt](url) and HTML images: <img src="url">
_IMG_MD_RE = re.compile(r"!\[.*?\]\((https?://[^)]+)\)")
_IMG_HTML_RE = re.compile(r'<img[^>]+src=["\']?(https?://[^"\'>\s]+)', re.IGNORECASE)


class DesignTask(Task):
    task_type = "design_issue"
    priority = 25

    def __init__(
        self,
        issue: Issue,
        existing_design: str | None,
        new_comments: list[Comment],
        image_urls: list[str],
    ) -> None:
        self.issue = issue
        self.existing_design = existing_design
        self.new_comments = new_comments
        self.image_urls = image_urls

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(github: GitHubClient) -> Iterator[DesignTask]:
        """Yield design tasks for issues that need a new or revised design."""
        for issue, labels in github.list_issues("ready-for-design"):
            logger.debug(
                "Examining issue #%d: %s (labels=%s)", issue.number, issue.title, labels
            )
            if "ready-for-planning" in labels:
                # User approved the design; hand off to planning agent.
                logger.debug(
                    "Issue #%d has 'ready-for-planning' — design approved, "
                    "removing 'ready-for-design'",
                    issue.number,
                )
                github.remove_label(issue.number, "ready-for-design")
                continue
            comments = github.get_issue_comments(issue.number)
            existing_design, new_comments = DesignTask._analyze_design_comments(
                comments, github.bot_name
            )
            if existing_design is not None:
                logger.debug(
                    "Issue #%d: existing design found (%d chars), %d new comment(s) since last design",
                    issue.number, len(existing_design), len(new_comments),
                )
            else:
                logger.debug(
                    "Issue #%d: no existing design — will create initial design", issue.number
                )
            if existing_design is None or new_comments:
                image_urls = DesignTask._extract_image_urls(issue.body or "")
                yield DesignTask(issue, existing_design, new_comments, image_urls)
            else:
                logger.debug(
                    "Issue #%d: design exists and no new feedback — skipping", issue.number
                )

    @staticmethod
    def _analyze_design_comments(
        comments: list[Comment], bot_name: str
    ) -> tuple[str | None, list[Comment]]:
        """Return (existing_design, new_user_comments_since_last_design).

        Only a bot comment starting with DESIGN_MARKER counts as a design.
        Other bot comments (e.g. failure notices) are ignored.
        """
        bot_last_design_idx = -1
        bot_last_design: str | None = None

        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(DESIGN_MARKER):
                bot_last_design_idx = i
                bot_last_design = c.body[len(DESIGN_MARKER):].strip()

        if bot_last_design_idx == -1:
            new_comments = [c for c in comments if c.author != bot_name]
        else:
            new_comments = [
                c for c in comments[bot_last_design_idx + 1:] if c.author != bot_name
            ]

        return bot_last_design, new_comments

    @staticmethod
    def _extract_image_urls(body: str) -> list[str]:
        """Extract image URLs from markdown and HTML img tags in the issue body."""
        urls = _IMG_MD_RE.findall(body) + _IMG_HTML_RE.findall(body)
        # Deduplicate while preserving order
        seen: set[str] = set()
        result = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    def describe(self) -> str:
        if self.existing_design is None:
            return (
                f"Create UI/UX design notes and specifications for the following GitHub issue.\n"
                f"Describe layout, user flows, component hierarchy, and visual design decisions.\n"
                f"If images or mockups are provided, analyse them and incorporate them into your design.\n\n"
                f"Issue #{self.issue.number}: {self.issue.title}\n\n"
                f"{self.issue.body}\n\n"
                f"Output ONLY the design specification in well-structured markdown.\n"
                f"Do NOT write any code — design only."
            )

        feedback = "\n\n".join(
            f"**{c.author}:** {c.body}" for c in self.new_comments
        )
        return (
            f"Revise the UI/UX design specification for GitHub issue #{self.issue.number} "
            f"based on user feedback.\n\n"
            f"Issue #{self.issue.number}: {self.issue.title}\n\n"
            f"{self.issue.body}\n\n"
            f"## Current Design\n\n{self.existing_design}\n\n"
            f"## User Feedback\n\n{feedback}\n\n"
            f"Output ONLY the updated design specification in well-structured markdown.\n"
            f"Do NOT write any code — design only."
        )

    def on_start(self, github: GitHubClient) -> None:
        logger.debug("Issue #%d: starting design", self.issue.number)
        github.add_label(self.issue.number, "in-progress")
        github.assign_self(self.issue.number)

    def on_complete(self, github: GitHubClient, result: TaskResult) -> None:
        logger.debug(
            "Issue #%d: posting design (%d chars): %s",
            self.issue.number, len(result.summary), truncate_for_log(result.summary),
        )
        github.remove_label(self.issue.number, "in-progress")
        github.post_comment(self.issue.number, f"{DESIGN_MARKER}\n\n{result.summary}")

    def on_failure(self, github: GitHubClient, error: Exception) -> None:
        logger.debug("Issue #%d: design failed (%s)", self.issue.number, error)
        github.remove_label(self.issue.number, "in-progress")
        github.post_comment(
            self.issue.number,
            f"Design failed: {error}",
        )
