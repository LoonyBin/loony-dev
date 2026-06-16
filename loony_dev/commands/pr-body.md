---
description: Write a GitHub PR body in the loony-dev format
argument-hint: <path to JSON context file>
---
Write a GitHub pull request body. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `issue_number` — the GitHub issue number the PR closes
- `title` — the issue title
- `body` — the issue description
- `diff` — the diff of the changes on the PR branch

Format the body exactly like this (no extra sections, no preamble):
## Summary
- <bullet points: what changed and why>

## Test plan
- [ ] <checkbox items to verify>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Closes #<issue_number>

Output ONLY the PR body — no explanation, no markdown fences.
