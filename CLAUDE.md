# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`loony-dev` is an agent orchestrator that watches a GitHub repo and dispatches issues to Claude-powered agents. It is **dogfooded**: it runs against its own issues. On GitHub the bot account is **trixy**. Entry point: `loony-dev` CLI (`loony_dev/cli.py`), orchestrator loop in `loony_dev/orchestrator.py`.

## Commands

- Install: `uv pip install -e .`
- Run tests: `uv run pytest`
- Run a single test: `uv run pytest loony_dev/tests/test_x.py::test_name`
- Run the orchestrator: `loony-dev` (from a git repo with a GitHub remote)

## Conventions

- **Python 3.12+.**
- **Conventional Commits**, with a scope: `feat(git):`, `fix(github):`, `refactor(planning):`. The scope is the subsystem (`github`, `git`, `planning`, `coding`, `pr-review`, `bot`, `coderabbit`).
- **Branches:** `issue-<number>/<slug>` for issue work, `fix/<slug>` / `feat/<slug>` otherwise.
- **One issue → one branch → one PR.** PR title `{title} (#{number})`; body has `## Summary` and `## Test plan`; closes the issue.
- Prefer raising on failure over silently returning empty/default values (recent fixes have hardened this pattern across `github/` and `git.py`).
- Match the surrounding code's style; tests live in `loony_dev/tests/` and use pytest.

## The lifecycle (label state machine)

Issues move through GitHub labels (defined in `loony_dev/github/repo.py`):

1. `ready-for-planning` → the planning agent posts a plan as an issue comment and **waits for a human** to approve. New comments trigger a re-plan.
2. `ready-for-development` → the coding agent implements: code → CodeRabbit review → commit/push → open PR. It removes the label and adds `in-progress` while working.
3. `in-progress` → bot actively working (auto-reset if stuck >12h).
4. `in-error` → set after repeated identical failures; **stops and requires a human.**

The bot also self-handles CI failures, merge conflicts, and post-PR review comments on its own PRs. Plan-approval and PR-merge are the two gates intentionally left to a human.

## Architecture notes

- `loony_dev/agents/` — `planning.py`, `coding.py` (each shells out to the Claude Code CLI with a session id for context continuity).
- `loony_dev/tasks/` — one class per task type (planning, issue, pr_review, ci_failure, conflict_resolution, stuck cleanup); each defines `discover`, `on_start`, `on_complete`, `on_failure`, and a priority.
- `loony_dev/github/` — GitHub API wrappers (REST + GraphQL via `gh`); `issue.py`, `comment.py`, `client.py`, `repo.py`.
- `loony_dev/git.py` — `GitRepo` with branch + worktree lifecycle helpers.
- `loony_dev/coderabbit.py` — wraps `coderabbit review --agent`.
- Active direction: worktree-isolated parallel task execution and a web dashboard replacing the Textual TUI (issues #126–#134).
