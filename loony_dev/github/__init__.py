"""GitHub package — Active Record models with Content safety tracking.

Usage::

    from loony_dev.github import Repo, Issue, Comment, PullRequest

    repo = Repo("owner/repo")       # or Repo() to autodetect
    issue = Issue.get(87, repo=repo)
    issue.body.is_safe               # False — from the internet
    issue.body.sanitize().is_safe    # True — cleaned
    issue.add_comment("Hello!")      # Post a comment
    issue.add_label("in-progress")   # Add a label
"""
from loony_dev.github.branch import Branch
from loony_dev.github.check_run import CheckRun
from loony_dev.github.client import GitHubClient
from loony_dev.github.comment import Comment, WarningComment
from loony_dev.github.content import Content, ValidationResult
from loony_dev.github.issue import GitHubItem, Issue, IssueCollection
from loony_dev.github.milestone import Milestone
from loony_dev.github.pull_request import PullRequest, PullRequestCollection
from loony_dev.github.repo import Repo
from loony_dev.github.workflow import Workflow, WorkflowRun, WorkflowRunCollection

__all__ = [
    "Branch",
    "CheckRun",
    "Comment",
    "Content",
    "GitHubClient",
    "GitHubItem",
    "Issue",
    "IssueCollection",
    "Milestone",
    "PullRequest",
    "PullRequestCollection",
    "Repo",
    "ValidationResult",
    "WarningComment",
    "Workflow",
    "WorkflowRun",
    "WorkflowRunCollection",
]
