"""Tests for IssueTask prompt methods."""
from __future__ import annotations

from unittest.mock import MagicMock

from loony_dev.github import Issue
from loony_dev.tasks.issue_task import IssueTask, _slugify


def _make_issue(number: int = 1, title: str = "Test issue", body: str = "body") -> Issue:
    repo = MagicMock()
    repo.bot_name = "loony-bot"
    return Issue(number=number, title=title, body=body, author="user", _repo=repo)


def test_branch_name_is_deterministic():
    task = IssueTask(_make_issue(number=42, title="Fix the bug"))
    assert task.branch_name == "issue-42/fix-the-bug"
    assert task.branch_name == "issue-42/fix-the-bug"


def test_branch_name_slugifies_title():
    task = IssueTask(_make_issue(number=189, title="PR A — Strip `scheduled` state"))
    assert task.branch_name == "issue-189/pr-a-strip-scheduled-state"


def test_branch_name_truncates_long_titles():
    long_title = "A" * 100
    task = IssueTask(_make_issue(number=1, title=long_title))
    slug = task.branch_name.split("/", 1)[1]
    assert len(slug) <= 50


def test_describe_is_short_human_readable_label():
    # describe() is now a concise label for logging — not the turn sent to Claude
    # (that is the /implement-issue slash command built from implement_payload).
    task = IssueTask(_make_issue(number=7, title="Add login"))
    assert task.describe() == "Implement issue #7: Add login"


def test_implement_payload_includes_issue_text_when_no_plan():
    task = IssueTask(_make_issue(number=7, title="Add login", body="do the thing"))
    payload = task.implement_payload()
    assert payload["issue_number"] == 7
    assert payload["title"] == "Add login"
    assert payload["body"] == "do the thing"
    assert "plan" not in payload


def test_implement_payload_includes_plan_when_present():
    task = IssueTask(_make_issue(number=7), plan="## Approved plan\n\nStep 1")
    payload = task.implement_payload()
    assert payload["plan"] == "## Approved plan\n\nStep 1"
    # body is still carried so the command body has the issue text as context.
    assert "body" in payload


def test_fix_review_payload_contains_review_output():
    task = IssueTask(_make_issue(number=3))
    review = "Line 5: missing type hint"
    payload = task.fix_review_payload(review)
    assert payload["issue_number"] == 3
    assert payload["review_output"] == review


def test_fix_hook_payload_contains_hook_output():
    task = IssueTask(_make_issue(number=3))
    hook_out = "pre-commit hook failed: flake8 errors"
    payload = task.fix_hook_payload(hook_out)
    assert payload["issue_number"] == 3
    assert payload["hook_output"] == hook_out


def test_commit_message_payload_carries_number_and_title():
    task = IssueTask(_make_issue(number=42, title="Add feature"))
    payload = task.commit_message_payload()
    assert payload["issue_number"] == 42
    assert payload["title"] == "Add feature"


def test_pr_body_payload_carries_diff_and_issue_text():
    task = IssueTask(_make_issue(number=42, title="Add feature", body="why"))
    payload = task.pr_body_payload("diff --git a b")
    assert payload["issue_number"] == 42
    assert payload["title"] == "Add feature"
    assert payload["body"] == "why"
    assert payload["diff"] == "diff --git a b"
