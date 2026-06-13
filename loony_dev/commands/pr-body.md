---
description: Write a GitHub PR body in the loony-dev format
argument-hint: <issue number/title/description and the diff>
---
Write a GitHub pull request body for the changes implementing the following:

$ARGUMENTS

Format the body exactly like this (no extra sections, no preamble):
## Summary
- <bullet points: what changed and why>

## Test plan
- [ ] <checkbox items to verify>

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Closes #<issue number>

Output ONLY the PR body — no explanation, no markdown fences.
