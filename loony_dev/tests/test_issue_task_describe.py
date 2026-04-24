"""Tests for IssueTask prompt methods."""
from __future__ import annotations

from unittest.mock import MagicMock

from loony_dev.github import Issue
from loony_dev.tasks.issue_task import IssueTask


def _make_issue(number: int = 1, title: str = "Test issue", body: str = "body") -> Issue:
    repo = MagicMock()
    repo.bot_name = "loony-bot"
    return Issue(number=number, title=title, body=body, author="user", _repo=repo)


def test_describe_delegates_to_implement_prompt():
    task = IssueTask(_make_issue())
    assert task.describe() == task.implement_prompt()


def test_implement_prompt_contains_branch_instruction():
    task = IssueTask(_make_issue())
    assert "Create a new branch" in task.implement_prompt()


def test_implement_prompt_does_not_instruct_commit_push_or_pr():
    task = IssueTask(_make_issue())
    prompt = task.implement_prompt().lower()
    assert "gh pr create" not in prompt
    assert "git commit" not in prompt
    assert "git push" not in prompt


def test_implement_prompt_instructs_no_git_ops():
    task = IssueTask(_make_issue())
    assert "Do NOT commit, push, or create a pull request" in task.implement_prompt()


def test_fix_review_prompt_contains_review_output():
    task = IssueTask(_make_issue())
    review = "Line 5: missing type hint"
    prompt = task.fix_review_prompt(review)
    assert review in prompt
    assert "fix" in prompt.lower()
    assert "Do NOT commit" in prompt


def test_fix_hook_prompt_contains_hook_output():
    task = IssueTask(_make_issue())
    hook_out = "pre-commit hook failed: flake8 errors"
    prompt = task.fix_hook_prompt(hook_out)
    assert hook_out in prompt
    assert "Do NOT commit" in prompt


def test_commit_message_prompt_requests_conventional_commit():
    task = IssueTask(_make_issue(number=42, title="Add feature"))
    prompt = task.commit_message_prompt()
    assert "#42" in prompt
    assert "conventional commit" in prompt.lower()
    assert "ONLY" in prompt
