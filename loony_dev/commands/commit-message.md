---
description: Generate a conventional commit message only
argument-hint: <path to JSON context file>
---
Generate a conventional commit message. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `issue_number` — the GitHub issue number the changes implement
- `title` — the issue title

Generate a conventional commit message for the changes made to implement the issue.

Output ONLY the commit message — no explanation, no preamble, no markdown fences. The message must reference `#<issue_number>`.
