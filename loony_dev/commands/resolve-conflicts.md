---
description: Resolve merge conflicts on a PR branch
argument-hint: <PR number/title, branch, default branch>
---
Resolve merge conflicts on a PR.

$ARGUMENTS

The PR branch has conflicts with the default branch that must be resolved before merging.

Instructions:
- Run: git checkout <pr-branch>
- Run: git merge <default-branch>
- If conflicts arise, read each conflicting file, understand the intent of both sides,
  and resolve the markers appropriately
- Stage resolved files and run: git merge --continue
- Push: git push --force-with-lease
- Do NOT create a new PR or commit unrelated changes
