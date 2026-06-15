---
description: Fix code so a rejected git hook passes (code only, no commits)
argument-hint: <path to JSON context file>
---
Fix code so a rejected git hook passes. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `issue_number` — the GitHub issue number being worked on
- `hook_output` — the output from the git hook that rejected the commit

A git hook rejected the commit. Please fix the code to satisfy the hook. Do NOT commit or push — only fix the code.
