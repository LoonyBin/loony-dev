---
description: Fix failing CI checks on a PR
argument-hint: <path to JSON context file>
---
Fix CI failures on a PR. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `pr_number` — the PR number
- `title` — the PR title
- `branch` — the PR branch to fix
- `failed_checks` — a list of failing checks, each with `name`, `conclusion`, and `url`

Instructions:
- Review the CI logs at the `url` of each failed check
- Identify the root cause of each failure
- Make targeted fixes on the PR branch
- Do not change unrelated code
- Push the fixes when done
