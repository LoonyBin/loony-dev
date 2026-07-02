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
- Any behavior-tuning number you introduce (timeout, poll interval, backoff, retry count, size/length limit, TTL, threshold) must go through the config system per CLAUDE.md's "Configuration & tunable constants" convention: a **named module-level constant** in the right tier (Tier-1 Click option, Tier-2 `_worker_setting`/`gh_setting` key, or Tier-3 internal with a `# not configurable:` comment) — **never an inline magic literal**. If it's Tier 1 or 2, add its documented line to `config.toml.example` in the same change.
- After making changes, read `.githooks/pre-commit` to understand what checks this project requires, run all applicable checks, and fix any failures before stopping
- Do NOT commit, push, or create a pull request — stop after making code changes
