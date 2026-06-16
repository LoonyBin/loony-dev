---
description: Create an implementation plan for a GitHub issue (planning only)
argument-hint: <path to JSON context file>
---
Create or revise an implementation plan for a GitHub issue. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It always contains:
- `issue_number` — the GitHub issue number
- `title` — the issue title
- `body` — the issue description

For a **revision**, it additionally contains:
- `current_plan` — the existing plan to revise
- `feedback` — user feedback to incorporate
- `revision_note_delimiter` — a literal delimiter string you MUST emit (see below)

You may read the codebase to understand the existing structure before planning.

If `current_plan` is absent (a fresh plan):
- Output ONLY the plan text in well-structured markdown.

If `current_plan` is present (a revision):
- Output the updated plan in well-structured markdown.
- Then, on its own line, emit the literal `revision_note_delimiter` value exactly (it is `<!-- loony-revision-note -->`).
- After the delimiter, write a short (2-4 sentence) revision note summarising what changed in this revision and any questions or pushback you have about the feedback.
- The plan must come before the delimiter and the revision note after it; do not include the delimiter anywhere else.

Do NOT implement anything — planning only.
