"""Tests for PRReviewTask._new_since_bot() and discover() — inline comment timing.

Issue #78: Inline review comments were silently dropped from the polling loop
because their ``created_at`` timestamp reflects when the reviewer *drafted* the
comment, not when the review was *submitted*. If drafting happened before the
bot's last SUCCESS_MARKER, the comments sorted before the marker and were
excluded from "new" comments even though the review was submitted afterwards.

The fix is to use the review's ``submitted_at`` as the effective timestamp for
all inline comments that belong to it.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from loony_dev.github.comment import Comment
from loony_dev.github.content import Content
from loony_dev.github.pull_request import PullRequest
from loony_dev.tasks.base import FAILURE_MARKER, SUCCESS_MARKER, SUCCESS_MARKER_PREFIX, encode_marker
from loony_dev.models import TaskResult
from loony_dev.tasks.pr_review_task import PRReviewTask

BOT_NAME = "loony-bot"
REVIEWER = "alice"


def _mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.bot_name = BOT_NAME
    return repo


def _comment(author: str, body: str, ts: str, path: str | None = None, line: int | None = None) -> Comment:
    return Comment(author=author, body=body, created_at=ts, path=path, line=line)


def _success(ts: str, last_seen: str | None = None) -> Comment:
    if last_seen is not None:
        marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen)
    else:
        marker = SUCCESS_MARKER
    return _comment(BOT_NAME, f"{marker}\nReview addressed.", ts)


def _inline(ts: str, path: str = "foo.py", line: int = 10) -> Comment:
    return _comment(REVIEWER, "Please fix this.", ts, path=path, line=line)


def _general(ts: str) -> Comment:
    return _comment(REVIEWER, "LGTM overall.", ts)


class TestNewSinceBot(unittest.TestCase):

    def test_inline_only_no_prior_marker(self) -> None:
        comments = [
            _inline("2024-01-01T10:00:00Z"),
            _inline("2024-01-01T11:00:00Z"),
        ]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, comments)

    def test_inline_drafted_before_marker_submitted_after(self) -> None:
        marker = _success("2024-01-01T12:00:00Z")
        inline_with_submitted_ts = _inline("2024-01-01T13:00:00Z")

        comments = sorted([marker, inline_with_submitted_ts], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [inline_with_submitted_ts])

    def test_inline_both_before_marker(self) -> None:
        inline_before = _inline("2024-01-01T10:00:00Z")
        marker = _success("2024-01-01T12:00:00Z")

        comments = sorted([inline_before, marker], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [])

    def test_general_and_inline_mixed(self) -> None:
        old_general = _general("2024-01-01T09:00:00Z")
        marker = _success("2024-01-01T12:00:00Z")
        new_inline = _inline("2024-01-01T13:00:00Z")
        new_general = _general("2024-01-01T14:00:00Z")

        comments = sorted(
            [old_general, marker, new_inline, new_general],
            key=lambda c: c.created_at,
        )
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [new_inline, new_general])

    def test_uses_last_success_marker(self) -> None:
        old_marker = _success("2024-01-01T10:00:00Z")
        middle_comment = _general("2024-01-01T11:00:00Z")
        new_marker = _success("2024-01-01T12:00:00Z")
        after_new = _inline("2024-01-01T13:00:00Z")

        comments = [old_marker, middle_comment, new_marker, after_new]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [after_new])

    def test_timestamp_filter_picks_up_midrun_comment(self) -> None:
        t1_comment = _general("2024-01-01T10:00:00Z")
        t2_midrun = _general("2024-01-01T11:00:00Z")
        t3_marker = _success("2024-01-01T12:00:00Z", last_seen="2024-01-01T10:00:00Z")

        comments = sorted([t1_comment, t2_midrun, t3_marker], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t2_midrun])

    def test_timestamp_filter_excludes_already_seen_comments(self) -> None:
        t1 = _general("2024-01-01T09:00:00Z")
        t2 = _general("2024-01-01T10:00:00Z")
        marker = _success("2024-01-01T11:00:00Z", last_seen="2024-01-01T10:00:00Z")
        t3 = _general("2024-01-01T12:00:00Z")

        comments = sorted([t1, t2, marker, t3], key=lambda c: c.created_at)
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t3])

    def test_old_marker_backward_compat_still_uses_position(self) -> None:
        t1 = _general("2024-01-01T09:00:00Z")
        marker = _success("2024-01-01T10:00:00Z")
        t2 = _general("2024-01-01T11:00:00Z")

        comments = [t1, marker, t2]
        result = PRReviewTask._new_since_bot(comments, BOT_NAME)
        self.assertEqual(result, [t2])


class TestDiscoverInlineOnly(unittest.TestCase):
    """discover() should yield a task when the only new comments are inline."""

    def _make_repo_with_prs(self, pr_data: list[dict], inline_comments: list[Comment]) -> MagicMock:
        repo = _mock_repo()
        repo._tick_cache = {}
        repo.skip_ci_checks = set()
        repo._check_runs_cache = {}
        repo.name = "owner/repo"

        prs = [PullRequest._from_api(d, repo) for d in pr_data]
        # Patch list_open to return our PRs
        repo._tick_cache["open_prs"] = prs

        # Patch inline_comments via the GraphQL transport.  Each inline comment
        # becomes its own thread (mirrors single-comment review submissions in
        # the live API and keeps the fixture flat).
        repo.client.gh_graphql.return_value = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": f"thread{i}",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {"nodes": [{
                                        "databaseId": 1000 + i,
                                        "author": {"login": c.author},
                                        "body": str(c.body),
                                        "url": f"https://github.com/owner/repo/pull/1#discussion_r{1000+i}",
                                        # createdAt is the *drafted* time in GraphQL
                                        # (mirrors REST) -- we deliberately use a
                                        # different value as submittedAt to lock
                                        # down the #78 fix.
                                        "createdAt": "2024-01-01T00:00:00Z",
                                        "path": c.path,
                                        "line": c.line,
                                        "replyTo": None,
                                        "pullRequestReview": {"submittedAt": c.created_at},
                                    }]},
                                }
                                for i, c in enumerate(inline_comments)
                            ],
                        },
                    },
                },
            },
        }
        repo.is_authorized = MagicMock(return_value=True)
        return repo

    def _pr_data(self, comments: list[dict] | None = None) -> dict:
        return {
            "number": 1,
            "headRefName": "feature/branch",
            "headRefOid": "abc123",
            "title": "My PR",
            "comments": comments or [],
            "reviews": [],
            "labels": [],
            "mergeable": "MERGEABLE",
            "updatedAt": "2024-01-01T14:00:00Z",
            "assignees": [{"login": BOT_NAME}],
        }

    def test_discover_triggers_on_inline_only(self) -> None:
        inline = _inline("2024-01-01T13:00:00Z")
        repo = self._make_repo_with_prs([self._pr_data()], [inline])

        tasks = list(PRReviewTask.discover(repo))

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].pr.number, 1)

    def test_discover_skips_when_inline_before_marker(self) -> None:
        inline_before = _inline("2024-01-01T10:00:00Z")
        pr_data = self._pr_data(comments=[{
            "author": {"login": BOT_NAME},
            "body": f"{SUCCESS_MARKER}\nReview addressed.",
            "createdAt": "2024-01-01T12:00:00Z",
        }])
        repo = self._make_repo_with_prs([pr_data], [inline_before])

        tasks = list(PRReviewTask.discover(repo))

        self.assertEqual(tasks, [])

    def test_discover_carries_comments_for_failure_escalation(self) -> None:
        """The yielded task's PR must carry full `comments`, not just
        `new_comments` — otherwise get_comments() returns [] and the
        repeated-failure -> in-error escalation can never see prior failures.
        Regression for PR #177's unbounded retry loop.
        """
        prior_failure = {
            "author": {"login": BOT_NAME},
            "body": f"{FAILURE_MARKER}\n\nFailed to address review comments: boom",
            "createdAt": "2024-01-01T11:00:00Z",
        }
        pr_data = self._pr_data(comments=[prior_failure])
        inline = _inline("2024-01-01T13:00:00Z")
        repo = self._make_repo_with_prs([pr_data], [inline])

        tasks = list(PRReviewTask.discover(repo))

        self.assertEqual(len(tasks), 1)
        carried = tasks[0].pr.get_comments()
        self.assertTrue(
            any(str(c.body).startswith(FAILURE_MARKER) and c.author == BOT_NAME for c in carried),
            "task PR dropped the bot's prior failure comment — escalation would never fire",
        )


class TestOnComplete(unittest.TestCase):
    """on_complete must always write a success marker, even when post_summary=False."""

    def _make_pr(self, comment_ts: str = "2024-01-01T10:00:00Z") -> MagicMock:
        pr = MagicMock()
        pr.number = 1
        pr.new_comments = [_comment(REVIEWER, "Review comment", comment_ts)]
        return pr

    def _make_repo(self) -> MagicMock:
        return _mock_repo()

    def _make_result(self, post_summary: bool, summary: str = "Some summary") -> TaskResult:
        return TaskResult(success=True, output="", summary=summary, post_summary=post_summary)

    def test_post_summary_true_posts_marker_with_summary(self) -> None:
        pr = self._make_pr()
        task = PRReviewTask(pr)
        result = self._make_result(post_summary=True, summary="Fixed the bug.")

        task.on_complete(self._make_repo(), result)

        pr.add_comment.assert_called_once()
        body = pr.add_comment.call_args[0][0]
        self.assertIn("loony-success", body)
        self.assertIn("last-seen=", body)
        self.assertIn("Fixed the bug.", body)

    def test_post_summary_false_still_posts_marker(self) -> None:
        """When no code changes are made, on_complete must still post a success marker
        so that _new_since_bot advances last-seen and doesn't re-trigger the task."""
        pr = self._make_pr()
        task = PRReviewTask(pr)
        result = self._make_result(post_summary=False)

        task.on_complete(self._make_repo(), result)

        pr.add_comment.assert_called_once()
        body = pr.add_comment.call_args[0][0]
        self.assertIn("loony-success", body)
        self.assertIn("last-seen=", body)

    def test_post_summary_false_marker_prevents_retrigger(self) -> None:
        """Simulate a full cycle: on_complete posts marker, then _new_since_bot
        sees no new comments because last-seen was advanced."""
        comment_ts = "2024-01-01T10:00:00Z"
        pr = self._make_pr(comment_ts)
        task = PRReviewTask(pr)
        result = self._make_result(post_summary=False)

        task.on_complete(self._make_repo(), result)

        posted_body = pr.add_comment.call_args[0][0]
        # Simulate next tick: the posted marker comment is now in the comment list
        marker_comment = _comment(BOT_NAME, posted_body, "2024-01-01T10:05:00Z")
        original_comment = _comment(REVIEWER, "Review comment", comment_ts)
        all_comments = sorted([original_comment, marker_comment], key=lambda c: c.created_at)

        new = PRReviewTask._new_since_bot(all_comments, BOT_NAME)
        self.assertEqual(new, [], "No comments should be new after marker is posted")


class TestContextPayload(unittest.TestCase):
    """PRReviewTask.context_payload() — the /address-reviews slash-command context (#166)."""

    def _pr(self) -> MagicMock:
        repo = MagicMock()
        repo.owner = "LoonyBin"
        repo.name = "LoonyBin/loony-dev"
        pr = MagicMock()
        pr.number = 12
        pr.title = "My PR"
        pr.branch = "issue-1/feature"
        pr._repo = repo
        pr.new_comments = [
            _comment(REVIEWER, "Fix this bug.", "2024-01-01T10:00:00Z", path="a.py", line=3),
        ]
        return pr

    def test_command_name_is_address_reviews(self) -> None:
        self.assertEqual(PRReviewTask.command_name, "address-reviews")

    def test_payload_keys_and_values(self) -> None:
        task = PRReviewTask(self._pr())
        payload = task.context_payload()
        self.assertEqual(payload["pr_number"], 12)
        self.assertEqual(payload["pr"], 12)
        self.assertEqual(payload["title"], "My PR")
        self.assertEqual(payload["branch"], "issue-1/feature")
        self.assertEqual(payload["owner"], "LoonyBin")
        self.assertEqual(payload["repo"], "loony-dev")
        self.assertIn("allow_create_issues", payload)
        # The comment blocks are pre-formatted into the `comments` string.
        self.assertIn("Fix this bug.", payload["comments"])
        self.assertIn("author=alice", payload["comments"])

    def test_describe_is_short_label(self) -> None:
        task = PRReviewTask(self._pr())
        self.assertEqual(task.describe(), "Address review comments on PR #12: My PR")


if __name__ == "__main__":
    unittest.main()
