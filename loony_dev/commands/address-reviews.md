---
description: Triage and address review comments on a PR
argument-hint: <path to JSON context file>
---
Address review comments on a PR. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `pr_number` / `pr` — the PR number
- `title` — the PR title
- `branch` — the PR branch you are on
- `owner` — the repository owner (for `gh api` calls below)
- `repo` — the repository name (for `gh api` calls below)
- `allow_create_issues` — whether you may open tracking issues for deferred findings
- `comments` — the new review comments to address (each block carries its `kind`, `id`, and for inline review comments `thread_id` and `in_reply_to_id`)

# Instructions

You are addressing the review comments in `comments`. Each comment comes from
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
- If `allow_create_issues` is true and the bug looks real, open a tracking
  issue in this repo (`gh issue create -R <owner>/<repo>`) with a short
  description and a link back to the comment URL. If `allow_create_issues`
  is false, do NOT open the issue yourself — instead mention in your reply
  that a follow-up issue should be filed.
- For inline review threads (kind=inline), do NOT resolve the thread
  yourself.

For FIX verdicts on bot-authored inline review threads, resolve the thread
after the commit lands (the commit speaks for itself).
Do NOT resolve human-authored inline review threads; leave those for the
human reviewer.

## Replying and resolving

Each comment block carries its `kind`, `id`, and (for inline review
comments) `thread_id` and `in_reply_to_id`. Use them:

- Reply to an inline review thread (kind=inline). Use the databaseId of the
  top-level comment in the thread -- that is `in_reply_to_id` when set,
  otherwise the comment's own `id`:

    gh api -X POST \
      repos/<owner>/<repo>/pulls/<pr>/comments/<top_id>/replies \
      -f body="<reply>"

- Reply to a conversation comment (kind=issue) or review body
  (kind=review_body). These have no per-comment reply endpoint -- post a
  new issue comment that references the original URL:

    gh api -X POST repos/<owner>/<repo>/issues/<pr>/comments \
      -f body="Re: <html_url>$'\n\n'<reply>"

- Resolve an inline review thread (only for FIX, only kind=inline):

    gh api graphql -F threadId="<thread_id>" -f query='
      mutation($threadId:ID!) {
        resolveReviewThread(input:{threadId:$threadId}) {
          thread { id isResolved }
        }
      }'

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
