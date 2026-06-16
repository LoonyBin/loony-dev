---
description: Resolve merge conflicts on a PR branch
argument-hint: <path to JSON context file>
---
Resolve merge conflicts on a PR. The argument is the path to a JSON context file.

Read the JSON file at: $ARGUMENTS

It contains:
- `pr_number` — the PR number
- `title` — the PR title
- `branch` — the PR branch with conflicts
- `default_branch` — the branch to merge in

The PR branch has conflicts with the default branch that must be resolved before merging.

Instructions:
- Run: git checkout <branch>
- Run: git merge <default_branch>
- If conflicts arise, read each conflicting file, understand the intent of both sides,
  and resolve the markers appropriately
- Stage resolved files and run: git merge --continue
- Push: git push --force-with-lease
- Do NOT create a new PR or commit unrelated changes
