# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`loony-dev` is an agent orchestrator that watches a GitHub repo and dispatches issues to Claude-powered agents. It is **dogfooded**: it runs against its own issues. On GitHub the bot account is **trixy**. Entry point: `loony-dev` CLI (`loony_dev/cli.py`), orchestrator loop in `loony_dev/orchestrator.py`.

## Commands

- Install: `uv pip install -e .`
- Run tests: `uv run pytest`
- Run a single test: `uv run pytest loony_dev/tests/test_x.py::test_name`
- Run the orchestrator: `loony-dev` (from a git repo with a GitHub remote)

## Worker setup (Claude Code hooks)

The persistent `ClaudeSession` (`loony_dev/agents/claude_session.py`) learns its lifecycle transitions — startup readiness, turn completion, interrupt, tool calls — from **Claude Code hook events**, not by polling the JSONL transcript (issue #178).

- Hooks are wired **per-session, not globally**: when `ClaudeSession.open()` launches `claude` it passes `--settings <json>` carrying our `SessionStart` / `Stop` / `PreToolUse` / `PostToolUse` hooks (`session_hooks.session_settings_json`). They therefore apply **only** to loony-managed sessions and never touch a human's own `claude` runs — and there is no global `~/.claude/settings.json` state to install, verify, or drift. `loony-dev setup` is now only an informational backward-compat command.
- The hook command is `{sys.executable} -m loony_dev hook <event>` (`loony_dev/__main__.py` → `session_hooks.run_hook`), invoked through the **current interpreter** rather than the bare `loony-dev` console script, so it resolves even when loony-dev is installed in a venv that is not on the session's `PATH`.
- Each hook reads its stdin payload, looks up the session's control socket by `session_id`, and writes one event line. Sockets live at `<claude-config>/_loony/sessions/<session_id>/control.sock`; the session binds the listener **before** forking `claude` so an immediate `SessionStart` is never missed.
- A long *backstop* timeout (`[worker] claude_session_backstop_seconds`, default 600s) on `open`/`send_turn` is a **liveness net only** — it trips when `claude` crashes before firing a hook, never as the primary signal.
- Migration seam: `[worker] session_events` selects `"hooks"` (default) or the legacy `"jsonl"` poll/parse path (kept for one release, then deleted). The JSONL path needs no hooks, so it omits the `--settings` payload.

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
