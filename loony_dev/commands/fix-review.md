---
description: Fix issues reported by a CodeRabbit review (code only, no commits)
argument-hint: <path to JSON context file>
---
Fix issues reported by a CodeRabbit review. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `issue_number` — the GitHub issue number being worked on
- `review_output` — the CodeRabbit review findings to address

A CodeRabbit code review found issues with your changes. Please fix them. Do NOT commit or push — only fix the code.

Any behavior-tuning number you add or change while fixing (timeout, poll interval, backoff, retry count, size/length limit, TTL, threshold) must go through the config system per CLAUDE.md's "Configuration & tunable constants" convention — a named module-level constant in the right tier, never an inline magic literal, and document Tier-1/2 keys in `config.toml.example`.
