from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

from loony_dev.models import RateLimitedError, truncate_for_log
from loony_dev.tasks.base import (
    FAILURE_MARKER,
    FAILURE_MARKER_PREFIX,
    SUCCESS_MARKER,
    SUCCESS_MARKER_PREFIX,
    Task,
    decode_last_seen,
    encode_marker,
)

if TYPE_CHECKING:
    from loony_dev.github import Comment, PullRequest, Repo
    from loony_dev.models import TaskResult

logger = logging.getLogger(__name__)


_REVIEW_INSTRUCTIONS_TEMPLATE = """\
# Instructions

You are addressing review comments on a PR. Each comment below comes from
either a human reviewer or a review bot (e.g. CodeRabbit). Apply the same
triage to all of them.

## Triage each comment: FIX, IGNORE, or DEFER

For every comment, first VERIFY the finding against the current code on disk
before deciding. The commenter may be wrong, may be looking at stale code,
or may be flagging something that is not actually present in this PR's diff.

Apply the diff-scope test:
  Would this finding still be true on the base branch, without this PR's
  changes?
  - If NO -> the finding is about code this PR introduces or modifies. It is
    in scope. Default to FIX unless the suggestion is clearly wrong or
    conflicts with repo conventions (then IGNORE with a reason).
  - If YES -> the finding is about a pre-existing latent bug or unrelated
    code. It is out of scope for this PR. DEFER: do NOT change the code in
    this PR.

Repo policy: pre-existing latent bug fixes get their own PR. If you find one
bug in a class of code (one query missing a filter, one mishandled null),
the class likely has more -- surveying it is its own work, not a side-quest
in this PR. Do not bundle "while you're here" fixes into a feature or
refactor PR, even if a reviewer asks for it.

Author priors:
- Authorized human reviewers are usually right about intent. Push back only
  if you have concrete code-grounded evidence.
- Review bots have a high false-positive rate, especially for "consider
  also fixing X" suggestions outside the diff. Verify carefully and lean
  toward DEFER on out-of-scope findings.

## Acting on each verdict

FIX (in-scope, valid):
- Make the smallest change that addresses the concern. Do not bundle
  unrelated cleanups.
- Run the project's pre-commit checks (.githooks/pre-commit if present).
- Commit and push. Reference the comment URL in the commit message.

IGNORE (false positive, stylistic disagreement, conflicts with repo
convention):
- Reply to the comment with one or two sentences explaining WHY, grounded
  in the code or convention. Not "won't fix" -- say why.
- For inline review threads (kind=inline), do NOT resolve the thread
  yourself -- leave it for the human reviewer to decide.

DEFER (valid but out of scope per the diff-scope test):
- Reply to the comment: "Acknowledged -- pre-existing, not in scope for
  this PR. Filing separately per repo policy." Link the follow-up issue
  if you opened one.
- {defer_action}
- For inline review threads (kind=inline), do NOT resolve the thread
  yourself.

For FIX verdicts on inline review threads, resolve the thread after the
commit lands (the commit speaks for itself). For human-authored threads,
err toward leaving resolution to the human.

## Replying and resolving

Each comment block above carries its `kind`, `id`, and (for inline review
comments) `thread_id` and `in_reply_to_id`. Use them:

- Reply to an inline review thread (kind=inline). Use the databaseId of the
  top-level comment in the thread -- that is `in_reply_to_id` when set,
  otherwise the comment's own `id`:

    gh api -X POST \\
      repos/{owner}/{repo}/pulls/{pr}/comments/<top_id>/replies \\
      -f body="<reply>"

- Reply to a conversation comment (kind=issue) or review body
  (kind=review_body). These have no per-comment reply endpoint -- post a
  new issue comment that references the original URL:

    gh api -X POST repos/{owner}/{repo}/issues/{pr}/comments \\
      -f body="Re: <html_url>$'\\n\\n'<reply>"

- Resolve an inline review thread (only for FIX, only kind=inline):

    gh api graphql -F threadId="<thread_id>" -f query='
      mutation($threadId:ID!) {{
        resolveReviewThread(input:{{threadId:$threadId}}) {{
          thread {{ id isResolved }}
        }}
      }}'

Never resolve a thread without a corresponding commit or reply.

## Pushback loop

If a reviewer (human or bot) replies disagreeing with your IGNORE or DEFER,
re-verify with the new information. If they have a point, switch to FIX. If
not, reply once more with more detail and stop. Two rounds maximum -- do
not get stuck in a loop.

## Anti-patterns

- Do not blindly apply every suggestion. Verify first.
- Do not bundle unrelated fixes into one commit.
- Do not amend earlier commits on a pushed branch; create new commits.
- Do not resolve a thread without a corresponding reply or commit.
- Do not use `--force` or `--no-verify`.
"""


class PRReviewTask(Task):
    task_type = "address_review"
    priority = 20

    def __init__(self, pr: PullRequest) -> None:
        self.pr = pr

    # ------------------------------------------------------------------
    # Task discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(repo: Repo) -> Iterator[PRReviewTask]:
        """Yield PRs that have new review comments from authorized users since the bot last responded."""
        from loony_dev.github import PullRequest

        for pr in PullRequest.list_open(repo=repo):
            if not pr.is_assigned_to(repo.bot_name):
                logger.debug("PR #%d is not assigned to bot — skipping", pr.number)
                continue
            logger.debug("Examining PR #%d: %s (labels=%s)", pr.number, pr.title, pr.labels)
            if "in-progress" in pr.labels:
                logger.debug("PR #%d is in-progress — skipping", pr.number)
                continue
            if "in-error" in pr.labels:
                logger.debug("PR #%d is in-error — skipping", pr.number)
                continue

            all_comments = PRReviewTask._assemble_comments(pr, repo)
            new_comments = PRReviewTask._new_since_bot(all_comments, repo.bot_name)

            if not new_comments:
                logger.debug("PR #%d has no new comments — skipping", pr.number)
                continue

            authorized_comments = [
                c for c in new_comments
                if repo.is_authorized(c.author)
            ]
            if not authorized_comments:
                logger.debug(
                    "PR #%d has %d new comment(s) but none from authorized users — skipping",
                    pr.number, len(new_comments),
                )
                continue

            logger.debug(
                "PR #%d has %d authorized new comment(s) — yielding task",
                pr.number, len(authorized_comments),
            )
            # Create a new PR object with just the relevant data for the task
            from loony_dev.github import PullRequest as PR
            yield PRReviewTask(PR(
                number=pr.number,
                branch=pr.branch,
                title=pr.title,
                new_comments=authorized_comments,
                _repo=pr._repo,
            ))

    @staticmethod
    def _assemble_comments(pr: PullRequest, repo: Repo) -> list[Comment]:
        """Combine general comments, review bodies, and inline review comments."""
        from loony_dev.github import Comment

        comments = list(pr.comments)
        comments += [r for r in pr.reviews if r.body]
        comments.extend(pr.inline_comments)
        comments.sort(key=lambda c: c.created_at)
        return comments

    @staticmethod
    def _new_since_bot(comments: list[Comment], bot_name: str) -> list[Comment]:
        """Return non-bot comments after the bot's last *successful* response."""
        bot_last_success_idx = -1
        bot_last_success_body: str | None = None
        for i, c in enumerate(comments):
            if c.author == bot_name and c.body.startswith(SUCCESS_MARKER_PREFIX):
                bot_last_success_idx = i
                bot_last_success_body = c.body

        if bot_last_success_idx == -1:
            result = [c for c in comments if c.author != bot_name]
        else:
            last_seen = decode_last_seen(bot_last_success_body or "")
            if last_seen is not None:
                result = [c for c in comments if c.author != bot_name and c.created_at > last_seen]
            else:
                result = [c for c in comments[bot_last_success_idx + 1:] if c.author != bot_name]

        logger.debug(
            "_new_since_bot: last success marker at index %d, returning %d new comment(s)",
            bot_last_success_idx, len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Task interface
    # ------------------------------------------------------------------

    @property
    def session_key(self) -> str:
        return f"pr:{self.pr.number}"

    @property
    def target_branch(self) -> str:
        return self.pr.branch

    def describe(self) -> str:
        from loony_dev import config

        allow_create_issues = bool(
            config.settings.get("pr_review_allow_create_issues", True)
        )
        repo = self.pr._repo
        owner = repo.owner
        repo_name = repo.name.split("/", 1)[1] if "/" in repo.name else repo.name

        comments_text = "\n\n".join(
            self._format_comment(c) for c in self.pr.new_comments
        )

        defer_action = (
            "If the bug looks real, open a tracking issue in this repo "
            "(`gh issue create -R " + repo.name + "`) with a short description "
            "and a link back to the comment URL."
            if allow_create_issues else
            "If the bug looks real, mention in your reply that a follow-up "
            "issue should be filed. Do NOT open the issue yourself."
        )

        instructions = _REVIEW_INSTRUCTIONS_TEMPLATE.format(
            owner=owner,
            repo=repo_name,
            pr=self.pr.number,
            defer_action=defer_action,
        )

        return (
            f"Address review comments on PR #{self.pr.number}: {self.pr.title}\n\n"
            f"You are on branch: {self.pr.branch}\n\n"
            f"New review comments to address:\n\n{comments_text}\n\n"
            f"{instructions}"
        )

    def _format_comment(self, comment: Comment) -> str:
        header_bits = [f"author={comment.author}", f"kind={comment.kind}"]
        if comment.id is not None:
            header_bits.append(f"id={comment.id}")
        if comment.thread_id:
            header_bits.append(f"thread_id={comment.thread_id}")
        if comment.in_reply_to_id is not None:
            header_bits.append(f"in_reply_to_id={comment.in_reply_to_id}")
        if comment.path:
            loc = comment.path + (f":{comment.line}" if comment.line else "")
            header_bits.append(f"location={loc}")
        if comment.html_url:
            header_bits.append(f"url={comment.html_url}")
        header = " | ".join(header_bits)
        return f"--- comment ---\n{header}\n\n{comment.body}"

    def on_start(self, repo: Repo) -> None:
        logger.debug("PR #%d: adding 'in-progress'", self.pr.number)
        self.pr.add_label("in-progress")
        self.pr.assign()

    def on_complete(self, repo: Repo, result: TaskResult) -> None:
        logger.debug("PR #%d: removing 'in-progress'", self.pr.number)
        self.pr.remove_label("in-progress")
        last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
        marker = encode_marker(SUCCESS_MARKER_PREFIX, last_seen_ts) if last_seen_ts else SUCCESS_MARKER
        if result.post_summary:
            logger.debug("Completion comment body: %s", truncate_for_log(result.summary))
            self.pr.add_comment(
                f"{marker}\n\nReview comments addressed.\n\n{result.summary}",
            )
        else:
            logger.debug("PR #%d: no code changes detected — posting silent marker", self.pr.number)
            self.pr.add_comment(f"{marker}\n\nNo changes needed.")

    def on_failure(self, repo: Repo, error: Exception) -> None:
        logger.debug("PR #%d: task failed (%s), removing 'in-progress'", self.pr.number, error)
        self.pr.remove_label("in-progress")
        if isinstance(error, RateLimitedError):
            logger.info(
                "PR #%d: rate-limited — skipping error comment (quota will reset automatically)",
                self.pr.number,
            )
            return
        last_seen_ts = max((c.created_at for c in self.pr.new_comments), default="")
        marker = encode_marker(FAILURE_MARKER_PREFIX, last_seen_ts) if last_seen_ts else FAILURE_MARKER
        failure_body = f"{marker}\n\nFailed to address review comments: {error}"
        self.pr.check_and_post_failure(
            failure_body,
            repo.bot_name,
            repo.repeated_failure_threshold,
            repo.owner,
        )
