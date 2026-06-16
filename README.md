# Loony-Dev

An agent orchestrator that watches a GitHub repo and dispatches issues and pull
requests to Claude-powered agents. It plans issues, implements approved work,
runs CodeRabbit review, opens PRs, and self-handles CI failures, merge conflicts,
and review comments on its own PRs — leaving **plan approval** and **PR merge** as
the two gates for a human.

It is **dogfooded**: it runs against its own repository, with the GitHub bot
account **trixy**.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [GitHub CLI (`gh`)](https://cli.github.com/) — installed and authenticated
- [Claude Code CLI (`claude`)](https://docs.anthropic.com/en/docs/claude-code) — installed and authenticated

## Installation

```bash
uv pip install -e .
```

This installs the `loony-dev` console script, a command group with five
subcommands: `worker`, `supervisor`, `web`, `setup`, and `hook`. (There is no
bare `loony-dev` command.)

## Quick Start

Run a worker from within a git repository that has a GitHub remote:

```bash
loony-dev worker
```

The worker auto-detects the repo from your git remote and the bot name from `gh`
auth, then polls every 60 seconds. Two steps are intentionally left to a human:
**approving a posted plan** (moving an issue to `ready-for-development`) and
**merging a PR**.

## Commands

### `loony-dev worker`

Runs the orchestrator loop for a single repository. Each actionable task runs in
its own git worktree, so a worker can process several tasks concurrently; GitHub
label state prevents two workers from taking the same item.

Key options:

| Option | Default | Purpose |
|---|---|---|
| `--repo owner/repo` | detected from git remote | Repository to watch |
| `--interval SECONDS` | `60` | Polling interval |
| `--max-concurrent-tasks N` | `3` | Tasks run at once (each in its own worktree) |
| `--work-dir PATH` | `.` | Repo checkout to operate on |
| `--bot-name NAME` | detected from `gh` auth | Bot username for watermark detection |
| `--allowed-users USER` | — | Always-permitted triggerers (repeatable) |
| `--min-role triage\|write\|admin` | `triage` | Minimum collaborator role to trigger a run |
| `--stuck-threshold-hours H` | `12` | When an in-progress item is reset as stuck |
| `--skip-ci-checks NAME` | — | CI check names to ignore (repeatable) |
| `--repeated-failure-threshold N` | `2` | Identical failures before marking `in-error` |
| `--log-file PATH` / `-v` | — | File logging / DEBUG logging |

### `loony-dev supervisor`

Discovers every repository the authenticated `gh` user can reach and runs a
`worker` for each in parallel under `<base-dir>/<owner>/<repo>`, restarting
crashed workers with exponential backoff. It also launches one
`claude --remote-control` session per repo (see [Remote control](#remote-control)).

```bash
loony-dev supervisor --base-dir ./workspace
```

Key options: `--base-dir` (checkouts + logs root), `--interval` (health-check
cadence, default 15s), `--refresh-interval` (repo re-discovery, default 1800s),
`--include` / `--exclude` (glob filters on `owner/repo`, repeatable),
`--no-remote-control`, `--accept-invites-from USER` (auto-accept repo invites,
repeatable). Arguments after `--` are forwarded to every worker:

```bash
loony-dev supervisor --base-dir ./workspace -- --interval 30 --min-role write
```

### `loony-dev web`

A read-only web dashboard for monitoring the supervisor and workers in the
browser. It runs as a separate process and derives **all** state from the on-disk
layout under `<base-dir>/.logs` — it shares no memory with workers.

```bash
loony-dev web --base-dir ./workspace --port 5338
```

It binds to `127.0.0.1` by default; tunnel in (e.g. SSH port-forward) to reach it
remotely, or use `--host` to bind another address. **Warning:** the dashboard
exposes mutating endpoints (write skills/commands, kill processes, attach to and
steer a task's Claude session) and has no auth — only bind to a non-loopback
address on a trusted network.

Worker, worktree, and session tables refresh on an interval. Clicking a worker
repo opens a **live log stream** over Server-Sent Events
(`/api/logs/{owner}/{repo}/stream`), which emits the recent backlog and then
pushes new lines as they are written. Clicking a session opens a live xterm.js
view of that task's Claude session and (between turns) lets you type into it —
see [ClaudeSession](#claudesession--steering) below.

### `loony-dev setup`

Informational, backward-compatibility only. loony-dev no longer installs hooks
into `~/.claude/settings.json`; lifecycle hooks are passed to each managed
session via `claude --settings`, so they never affect your own `claude` runs.
This command just prints the hook command that will be used.

### `loony-dev hook`

Internal — the executable Claude Code invokes for each lifecycle hook event. Not
meant to be run by hand.

## Remote control

When the supervisor launches a `claude --remote-control` session per repo, it
scans that session's output for the **claude.ai join URL** and writes it (with the
live PID) to a per-repo connection file. The dashboard surfaces the join link /
QR code via `/api/sessions`, giving you a single relay per repo to join from a
phone or another machine. Pass `--no-remote-control` to skip this (e.g. in
environments without Anthropic relay access, to avoid restart churn).

## How it works

### Lifecycle (label state machine)

Issues move through GitHub labels:

1. `ready-for-planning` → the **planning agent** posts a plan as an issue comment
   and **waits for a human** to approve. New comments trigger a re-plan.
2. `ready-for-development` → the **coding agent** implements: code → CodeRabbit
   review → commit/push → open PR. It swaps the label for `in-progress` while
   working.
3. `in-progress` → bot actively working (auto-reset if stuck past
   `--stuck-threshold-hours`).
4. `in-error` → set after repeated identical failures; **stops and requires a
   human.**

The bot also self-handles CI failures, merge conflicts, and post-PR review
comments on its own PRs. All durable state lives on GitHub — there are no local
state files for issue/PR progress.

### Work discovery (pipelines)

Each tick, the worker enumerates **pipelines** — one logical work-thread per
branch (`issue-N`, or `pr-P` for an externally-opened PR). Each pipeline returns
its single highest-priority actionable task via a pure read of GitHub + git
state, walking the priority ladder:

```
stuck → conflict → CI failure → PR review → planning → implementation
```

The scheduler then arbitrates across pipelines (global priority, the
`--max-concurrent-tasks` cap, in-flight dedupe) and dispatches each chosen task in
its own worktree. See [`CLAUDE.md`](CLAUDE.md) for the full design.

### Agents

Both the planning and coding agents drive Claude **non-interactively** via
`claude -p` — one subprocess per turn, prompt on stdin, context carried across
turns with `--resume <session-id>`. Agent prompts are packaged as Claude Code
slash commands under `loony_dev/commands/*.md` and invoked as
`/<command> <context.json>` rather than inline text.

## Architecture

```
loony_dev/
├── cli.py                 # Click command group (worker / supervisor / web / setup / hook)
├── orchestrator.py        # Per-repo worker loop: discover → schedule → dispatch
├── supervisor.py          # Multi-repo: worker-per-repo + remote-control relay
├── pipeline.py            # Pipeline discovery + next_task priority ladder
├── git.py                 # GitRepo: branch + worktree lifecycle
├── coderabbit.py          # Wraps `coderabbit review --agent`
├── session_registry.py    # On-disk session contract (workers + dashboard)
├── models.py              # Data classes (Issue, PullRequest, Comment, TaskResult)
├── agents/
│   ├── planning.py        # Planning agent (claude -p)
│   ├── coding.py          # Coding agent (claude -p via a thin _CliSession)
│   ├── claude_quota.py    # Shared CLI invocation + quota handling
│   ├── claude_session.py  # Persistent PTY session (dashboard observe/steer only)
│   ├── session_bridge.py  # Framed wire protocol + per-connection mic state
│   └── session_hooks.py   # Per-session lifecycle hooks via `claude --settings`
├── commands/              # Canonical slash-command markdown (installed per repo)
├── github/                # GitHub API wrappers (REST + GraphQL via gh)
├── tasks/                 # One class per task type (planning, issue, pr_review, …)
└── web/                   # Read-only FastAPI dashboard (SSE + htmx/Alpine/xterm.js)
```

### ClaudeSession & steering

`ClaudeSession` (`agents/claude_session.py`) is a persistent PTY-backed Claude
session. It **no longer runs agent turns** (those go through `claude -p`); it is
retained solely for the dashboard's live observe/steer bridge. Its input is a
single "mic": the bot holds it for the duration of a turn, and between turns a
human watching the dashboard owns the input. Mid-turn, a lone ESC interrupts the
turn (without killing the process) and any other keystroke is refused, so
operator input can't corrupt a turn in flight.

## Process model

Three long-running process kinds coordinate **through the filesystem only** — no
IPC between them except the per-session Unix sockets under the session registry:

- **worker** — orchestrator loop for one repo (`loony-dev worker`).
- **supervisor** — runs a worker per accessible repo and the remote-control relay
  (`loony-dev supervisor`).
- **web** — the read-only dashboard, reading `<base-dir>/.logs/...`
  (`loony-dev web`).

The on-disk **session registry** at
`<base-dir>/.logs/<owner>/<repo>/sessions/<task-slug>/` (`session.json`,
`attach.sock`, `injections/`) is a stable contract both workers and the dashboard
touch.

## Dev mode (auto-reload)

To run loony-dev in a mode that automatically pulls upstream changes and
restarts, use [gitmon](https://github.com/TMaYaD/gitmon):

```bash
gitmon uv run loony-dev supervisor --base-dir ./workspace
```

gitmon starts the supervisor immediately, then polls `git fetch` every 30
seconds. When new commits appear upstream, it runs `git pull` and restarts the
supervisor.

### Dog-fooding setup

Running loony-dev on its own source repo:

```bash
cd ~/LoonyBin/loony-dev
gitmon -i 60 uv run loony-dev supervisor --base-dir ./workspace
```

**Topology:**

```
~/LoonyBin/loony-dev/           ← Running copy (monitored by gitmon, always on main)
~/LoonyBin/loony-dev/workspace/ ← Worker clones (git-ignored, invisible to outer repo)
```

**How it stays safe:**

- gitmon only restarts the supervisor process — gitmon itself remains alive
  through bad deployments.
- If a merged PR crashes the supervisor, gitmon waits for the next commit, pulls
  the fix, and restarts automatically — fully self-recovering.
- Worker clones live under `workspace/`, which is in `.gitignore`, so they never
  dirty the outer repo's working tree.
