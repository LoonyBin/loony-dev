# Loony-Dev

An extensible agent orchestrator that watches GitHub and dispatches work to AI agents. Currently ships with a **coding agent** powered by Claude Code CLI, with an architecture designed for adding triage, planning, review, design, and other agent types.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [GitHub CLI (`gh`)](https://cli.github.com/) — authenticated
- [Claude Code CLI (`claude`)](https://docs.anthropic.com/en/docs/claude-code) — installed and authenticated

## Installation

```bash
uv pip install -e .
```

## Quick Start

Run from within a git repository that has a GitHub remote:

```bash
loony-dev
```

The orchestrator will auto-detect the repo from your git remote and start polling every 60 seconds.

## Usage

```
loony-dev [OPTIONS]

Options:
  --repo TEXT         owner/repo (default: detected from git remote)
  --interval INTEGER  Polling interval in seconds  [default: 60]
  --work-dir PATH     Working directory for the agent
  --bot-name TEXT     Bot username for watermark detection  [default: loony-dev[bot]]
  -v, --verbose       Enable debug logging
  --help              Show this message and exit.
```

### Examples

```bash
# Run with auto-detected repo
loony-dev

# Explicit repo, faster polling, verbose output
loony-dev --repo myorg/myrepo --interval 30 -v

# Use a specific working directory
loony-dev --work-dir /path/to/repo
```

## How It Works

### Polling Loop

The orchestrator polls GitHub on a configurable interval and processes one task per tick, prioritized as:

1. **PR review comments** — new comments after the bot's last comment
2. **Issues** — labeled `ready-for-development`

### State Tracking

All state lives on GitHub — no local state files.

#### Issues

| Stage | GitHub State |
|---|---|
| Ready for pickup | `ready-for-development` label present |
| Work in progress | `ready-for-development` removed, `in-progress` added |
| Completed | `in-progress` removed, bot posts summary comment |
| Failed | `in-progress` removed, `ready-for-development` restored, bot posts error comment |

#### Pull Request Reviews

| Stage | GitHub State |
|---|---|
| New comments detected | Comments exist after bot's last comment (watermark) |
| Work in progress | `in-progress` label added |
| Completed | `in-progress` removed, bot posts summary comment |
| Failed | `in-progress` removed, bot posts error comment |

The bot's last comment acts as a **watermark** — only comments posted after it are considered "new". This includes issue comments, review comments, and inline code comments.

### Cleanup

After each task completes (or fails), the orchestrator:

1. Checks for uncommitted changes
2. Force-commits and pushes if any remain
3. Checks out `main`

## Architecture

```
loony_dev/
├── cli.py                  # Click CLI entry point
├── orchestrator.py         # Polling loop, prioritization, dispatch
├── github.py               # GitHub API via gh CLI
├── git.py                  # Git operations
├── models.py               # Data classes (Issue, PullRequest, Comment, TaskResult)
├── agents/
│   ├── base.py             # Abstract Agent interface
│   └── coding.py           # Claude Code coding agent
└── tasks/
    ├── base.py             # Abstract Task interface
    ├── issue_task.py       # Issue implementation task
    └── pr_review_task.py   # PR review task
```

### Core Abstractions

**Agent** — something that can execute work using a specific tool:

```python
class Agent(ABC):
    name: str
    def execute(self, task: Task) -> TaskResult: ...
    def can_handle(self, task: Task) -> bool: ...
```

**Task** — a unit of work with lifecycle hooks for GitHub state management:

```python
class Task(ABC):
    task_type: str
    def describe(self) -> str: ...       # Prompt for the agent
    def on_start(self, github): ...      # Label changes before execution
    def on_complete(self, github, result): ...  # Post-success updates
    def on_failure(self, github, error): ...    # Post-failure updates
```

### Coding Agent

The `CodingAgent` invokes Claude Code CLI as a subprocess:

```bash
claude -p --dangerously-skip-permissions "<task prompt>"
```

After execution, it makes a second Claude call to generate a summary of the work done, which gets posted as a GitHub comment.

## Extending

### Adding a New Agent

1. Create `loony_dev/agents/my_agent.py`:

```python
from loony_dev.agents.base import Agent
from loony_dev.models import TaskResult

class MyAgent(Agent):
    name = "my-agent"

    def can_handle(self, task):
        return task.task_type == "my_task_type"

    def execute(self, task):
        # Your tool invocation here
        return TaskResult(success=True, output="...", summary="...")
```

2. Register it in `cli.py`:

```python
agents = [CodingAgent(work_dir=work_path), MyAgent()]
```

### Adding a New Task Type

1. Create `loony_dev/tasks/my_task.py`:

```python
from loony_dev.tasks.base import Task

class MyTask(Task):
    task_type = "my_task_type"

    def describe(self):
        return "Instructions for the agent..."

    def on_start(self, github):
        github.add_label(self.number, "my-label")

    def on_complete(self, github, result):
        github.remove_label(self.number, "my-label")
        github.post_comment(self.number, result.summary)

    def on_failure(self, github, error):
        github.remove_label(self.number, "my-label")
        github.post_comment(self.number, f"Failed: {error}")
```

2. Add gathering logic in `orchestrator.py`'s `gather_tasks()` method.

### Adding a New Task Source

To watch something other than GitHub (e.g. Slack, Linear), add gathering logic that returns `Task` subclasses in the orchestrator's `gather_tasks()` method, or create a pluggable source interface.

## Required GitHub Labels

Create these labels in your repository:

- `ready-for-development` — marks issues ready for the bot to pick up
- `in-progress` — applied while the bot works on an issue/PR

## Dev Mode (Auto-Reload)

To run loony-dev in a mode that automatically pulls upstream changes and restarts, use [gitmon](https://github.com/TMaYaD/gitmon).

### Basic usage

```bash
gitmon uv run loony-dev supervisor --base-dir ./workspace
```

gitmon starts the supervisor immediately, then polls `git fetch` every 30 seconds. When new commits appear on the upstream branch, it runs `git pull` and restarts the supervisor.

### Dog-fooding setup

This is how to run loony-dev on itself — having the bot work on its own source repo:

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

- gitmon only restarts the supervisor process — gitmon itself remains alive through bad deployments
- If a merged PR introduces a bug that crashes the supervisor, gitmon waits for the next commit, pulls the fix, and restarts automatically — fully self-recovering
- Worker clones live under `workspace/`, which is listed in `.gitignore`, so they never dirty the outer repo's working tree or cause `git pull` to fail
