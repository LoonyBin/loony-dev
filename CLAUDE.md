# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this is

`loony-dev` is an agent orchestrator that watches a GitHub repo and dispatches issues to Claude-powered agents. It is **dogfooded**: it runs against its own issues. On GitHub the bot account is **trixy**. Entry point: the `loony-dev` CLI (`loony_dev/cli.py`), which is a command group — `worker`, `supervisor`, `web`, `setup`, `hook` (there is no bare `loony-dev` command). The orchestrator loop is in `loony_dev/orchestrator.py`.

## Process model

Three long-running process kinds, **coordinating through the filesystem only** — no IPC/sockets between them except the per-session Unix control/attach sockets under the session registry:

- **worker** (`loony-dev worker`, `loony_dev/orchestrator.py`) — the orchestrator loop for one repo. Runs each *pipeline*'s tasks in a per-pipeline git worktree, retained across all phases of one issue (plan → implement → review → CI fix → conflict) and reclaimed only when the work reaches a terminal GitHub state (see "How work is discovered" below); agents shell out to `claude -p`. GitHub label state prevents two workers taking the same item.
- **supervisor** (`loony-dev supervisor`, `loony_dev/supervisor.py`) — discovers every accessible repo, runs a `worker` per repo under `<base-dir>/<owner>/<repo>`, and restarts crashed children with exponential backoff. Unless `--no-remote-control` is set it also launches one `claude --remote-control` session per repo and captures the claude.ai join URL into a connection file (see below).
- **web** (`loony-dev web`, `loony_dev/web/`) — a read-only FastAPI dashboard. It derives all state from the on-disk layout under `<base-dir>/.logs/<owner>/<repo>/` and the session registry; it runs as a wholly separate process and shares no memory with workers.

Shared on-disk surfaces are the coordination substrate: logs/PID files under `<base-dir>/.logs/...`, and the **session registry** (`loony_dev/session_registry.py`) at `<base>/.logs/<owner>/<repo>/sessions/<task-slug>/` (`session.json`, `attach.sock`, `injections/`) — a stable, public-ish contract both workers and the dashboard touch.

## Commands

- Install: `uv pip install -e .`
- Run tests: `uv run pytest`
- Run a single test: `uv run pytest loony_dev/tests/test_x.py::test_name`
- Run a single-repo worker: `loony-dev worker` (from a git repo with a GitHub remote)
- Run the multi-repo supervisor: `loony-dev supervisor --base-dir ./workspace`
- Run the dashboard: `loony-dev web --base-dir ./workspace`

## How agents run Claude (turn execution)

**Both agents drive Claude non-interactively via `claude -p`** (`ClaudeQuotaMixin._run_claude_cli` / `_invoke_claude` in `loony_dev/agents/claude_quota.py`): one subprocess per turn, prompt on stdin (no ARG_MAX limit), context carried across turns by `--resume <session-id>` (falling back to `--session-id` to create). `_run_claude_cli` takes a `timeout`; on overrun the CLI **process group** is SIGKILLed and the call returns rc `124`.

- `PlanningAgent.execute` has always used this path.
- `CodingAgent` uses a thin `_CliSession` (`coding.py`) wrapping `_run_claude_cli` behind the old `open`/`send_turn`/`close` surface (so the multi-phase turn loop and its tests are unchanged). It **replaced** the persistent PTY `ClaudeSession` because driving Claude's interactive TUI over a PTY proved unreliable on recent CLI versions (e.g. 2.1.178): turns intermittently never executed — no hooks, no Stop, no transcript — and the worker stalled for the whole backstop. `claude -p` is the supported automation interface and is reliable.
- A fresh branch (no prior commits) gets a brand-new random session id (not the deterministic `session_key` id) so its phases stay resumable without inheriting stale context from a reused id.

## Persistent ClaudeSession + hooks (dashboard observe/steer only)

`ClaudeSession` (`loony_dev/agents/claude_session.py`) — the persistent PTY-backed session — is **no longer used to run agent turns**. It is retained for the web dashboard's live observe/steer bridge (#164). It learns its lifecycle transitions — startup readiness, turn completion, interrupt, tool calls — from **Claude Code hook events**, not by polling the JSONL transcript (issue #178).

- Hooks are wired **per-session, not globally**: when `ClaudeSession.open()` launches `claude` it passes `--settings <json>` carrying our `SessionStart` / `Stop` / `PreToolUse` / `PostToolUse` hooks (`session_hooks.session_settings_json`). They therefore apply **only** to loony-managed sessions and never touch a human's own `claude` runs — and there is no global `~/.claude/settings.json` state to install, verify, or drift. `loony-dev setup` is now only an informational backward-compat command.
- The hook command is `{sys.executable} -m loony_dev hook <event>` (`loony_dev/__main__.py` → `session_hooks.run_hook`), invoked through the **current interpreter** rather than the bare `loony-dev` console script, so it resolves even when loony-dev is installed in a venv that is not on the session's `PATH`.
- Each hook reads its stdin payload, looks up the session's control socket by `session_id`, and writes one event line. Sockets live at `<claude-config>/_loony/sessions/<session_id>/control.sock`; the session binds the listener **before** forking `claude` so an immediate `SessionStart` is never missed.
- A long *backstop* timeout (`[worker] claude_session_backstop_seconds`, default 600s) on `open`/`send_turn` is a **liveness net only** — it trips when `claude` crashes before firing a hook, never as the primary signal.
- Migration seam: `[worker] session_events` selects `"hooks"` (default) or the legacy `"jsonl"` poll/parse path (kept for one release, then deleted). The JSONL path needs no hooks, so it omits the `--settings` payload.

**Mic / turn-lock semantics (dashboard steer).** The session has a single input "mic" guarded by `_turn_lock`. A bot `send_turn` holds the lock for the whole turn; *between* turns the human operator owns the input. `operator_write(data)` enforces this and returns one of three outcomes: it passes the keystrokes through to the PTY between turns (`OPERATOR_WRITTEN`); mid-turn it routes a lone ESC to `interrupt()` so a watching human can abort a stuck turn without killing the process (`OPERATOR_INTERRUPTED`); and mid-turn it **refuses** any other keystroke (`OPERATOR_REFUSED`) so operator input can't corrupt a turn in flight. `interrupt()` writes ESC only when a turn is actually running; the check and the ESC write are atomic under the turn-state lock. This is purely the dashboard observe/steer path — it is **not** how agent turns are executed (those run via `claude -p`, above).

## Slash commands (agent prompts)

Agent prompts are packaged as Claude Code **slash commands**, not inline prompt text (#165/#166). The canonical markdown lives in `loony_dev/commands/*.md`; `install_commands` (called at worker startup, `cli.py`) writes/upgrades them into each repo checkout's `<repo>/.claude/commands/<name>.md` (git-excluded — reinstalled each run). An agent turn is dispatched as a short invocation `/<command> <abs-path-to-context.json>` (`claude_quota._command_turn` → `f"/{command} {path}"`); the command body reads the JSON context file (`loony_dev/agents/context_file.py`). If the command file is missing from the worktree, `CommandNotInstalledError` is raised **loudly** rather than silently falling back to an inline prompt — so don't reintroduce inline prompts; edit the canonical `.md` instead.

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

## How work is discovered (Pipelines)

Each tick, the orchestrator enumerates **pipelines** rather than scanning six task classes (`loony_dev/pipeline.py`, issue #197). A `Pipeline` is one logical work-thread keyed by branch (`issue-N`, or `pr-P` for an externally-opened PR with no originating issue). It groups the issue facet and the PR facet — every phase of an issue (plan → implement → review → CI fix → conflict) shares its `issue-N` key (the #181 worktree/session key).

- `Pipeline.discover(repo)` enumerates open issues + PRs **once** and groups them into pipelines.
- `Pipeline.next_task(repo)` is a **pure function of GitHub + git state**: it walks the same priority ladder (stuck 5 → conflict 10 → CI 15 → review 20 → plan 30 → implement 40) and returns the single highest-priority actionable task, or `None`. **One task per pipeline.** It never mutates GitHub — per-phase idempotency (e.g. the CI marker-vs-`updatedAt` check) is computed here once, not re-derived per task.
- `_find_work` (`orchestrator.py`) sources candidates from `_gather_candidates` (one `next_task()` per pipeline), then arbitrates with the unchanged scheduler: global priority order, the `max_concurrent` / `_free_slots` cap, and `_task_identity` in-flight dedupe.
- The per-task predicates live as module-level helpers (`*_action`) in each `loony_dev/tasks/*.py`; both the legacy `Task.discover()` (kept for unit tests) and `next_task` call the same helpers, so the logic is identical. Because `next_task` is a pure read, a label-reconciliation side effect (dropping a stale `ready-for-planning` once a plan is approved) now lives in `IssueTask.on_start`, not in discovery.

### Pipeline session (worktree retention)

Each active pipeline owns **one** git worktree, not one per task (`loony_dev/pipeline_session.py`, `Orchestrator._pipeline_sessions`, issue #198). It is lazily created on the pipeline's first task and reused — together with a deterministic Claude session id — across every subsequent phase, so consecutive phases of one issue see the same on-disk state and skip the per-task create/trust/install/teardown cycle.

- **Reclamation is driven by terminal GitHub state, not idle time** (`_reclaim_completed_pipelines`, every tick): a pipeline's worktree is released only once its PR is merged, its PR is closed without merging, or — with no PR — its issue is closed. A pipeline that is still in-flight or held by a drive lease (`loony_dev/pipeline_lease.py`, #199) is always kept.
- **Debuggability is the point:** because the worktree survives between phases, the operator can `cd <base-dir>/<owner>/<repo>/.worktrees/<owner>/<repo>/issue-N` at any point and inspect exactly what the bot did. Retention is for inspection, not performance.

## Architecture notes

- `loony_dev/agents/` — `planning.py`, `coding.py` (each shells out to the Claude Code CLI with a session id for context continuity).
- `loony_dev/tasks/` — one class per task type (planning, issue, pr_review, ci_failure, conflict_resolution, stuck cleanup); each defines `discover`, `on_start`, `on_complete`, `on_failure`, and a priority.
- `loony_dev/github/` — GitHub API wrappers (REST + GraphQL via `gh`); `issue.py`, `comment.py`, `client.py`, `repo.py`.
- `loony_dev/git.py` — `GitRepo` with branch + worktree lifecycle helpers.
- `loony_dev/coderabbit.py` — wraps `coderabbit review --agent`.
- `loony_dev/pipeline.py` — pipeline discovery + `next_task` (see "How work is discovered").
- `loony_dev/supervisor.py` — multi-repo supervisor (`loony-dev supervisor`): worker-per-repo with backoff restart + the per-repo `claude --remote-control` relay.
- `loony_dev/web/` — read-only FastAPI dashboard (`loony-dev web`): SSE/WebSocket streaming with an htmx + Alpine + xterm.js front end; all state derived from `<base-dir>/.logs/...`, observe/steer over the `ClaudeSession` bridge.
- `loony_dev/session_registry.py` — on-disk session contract under `.logs/<owner>/<repo>/sessions/<task-slug>/`, shared by workers and the dashboard.
- `loony_dev/pipeline_log.py` — per-pipeline worker logging (#220). A `PipelineLogHandler` on the root logger routes records emitted under `pipeline_log_context(key)` (set in `Orchestrator._run_task`) to `.logs/<owner>/<repo>/pipelines/<task-slug>.log`, **additive** to the universal `loony-worker.log` (unchanged — the OS-level stderr `dup2`). Paths are forward-only: a `<slug>.key` sidecar records the raw key so listing never reverses the irreversible `task_slug` digest. The web layer exposes tail/stream/list + a structured `/api/pipelines/{key}/activity` feed (the #225 Activity-timeline source).

The framework rework shipped worktree-isolated parallel task execution and the web dashboard, and **removed the Textual TUI** (issues #126–#134). Residual "why we moved off the PTY/TUI" notes are historical, not descriptions of current components.
