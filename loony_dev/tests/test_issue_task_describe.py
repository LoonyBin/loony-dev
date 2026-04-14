"""Tests for IssueTask.describe() — PR creation command."""
from __future__ import annotations

from unittest.mock import MagicMock

from loony_dev.github import Issue
from loony_dev.tasks.issue_task import IssueTask


def _make_issue(number: int = 1, title: str = "Test issue", body: str = "body") -> Issue:
    repo = MagicMock()
    repo.bot_name = "loony-bot"
    return Issue(number=number, title=title, body=body, author="user", _repo=repo)


def test_describe_contains_fork_safe_pr_command():
    task = IssueTask(_make_issue())
    description = task.describe()
    assert "gh pr create --assignee @me -R $(gh repo view --json nameWithOwner -q .nameWithOwner)" in description


def test_describe_contains_assignee_flag():
    task = IssueTask(_make_issue())
    description = task.describe()
    assert "--assignee @me" in description


def test_describe_does_not_contain_bare_gh_pr_create():
    task = IssueTask(_make_issue())
    description = task.describe()
    import re
    bare = re.search(r"gh pr create(?! --assignee @me -R)", description)
    assert bare is None, f"Found bare 'gh pr create' without '--assignee @me -R' at position {bare.start()}"
