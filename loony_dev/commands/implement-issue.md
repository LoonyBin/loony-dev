---
description: Implement a GitHub issue (code only, no commits)
argument-hint: <path to JSON context file>
---
Implement a GitHub issue. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `issue_number` — the GitHub issue number
- `title` — the issue title
- `body` — the issue description
- `plan` (optional) — an approved implementation plan; **prefer it when present**

Instructions:
- Implement the changes described in the issue (follow `plan` if present)
- After making changes, read `.githooks/pre-commit` to understand what checks this project requires, run all applicable checks, and fix any failures before stopping
- Do NOT commit, push, or create a pull request — stop after making code changes
