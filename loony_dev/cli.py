from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev import config
from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.null_agent import NullAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.git import GitRepo
from loony_dev.github import Repo
from loony_dev.orchestrator import Orchestrator


@click.group(cls=config.ClickGroup)
def cli() -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""


@cli.command("worker")
@click.option("--repo", default=None, help="owner/repo (default: detected from git remote)")
@click.option("--interval", default=60, help="Polling interval in seconds", show_default=True)
@click.option(
    "--max-concurrent-tasks", "max_concurrent_tasks", type=int, default=3, show_default=True,
    help="Maximum number of tasks this worker runs at once. Each runs in its own "
         "git worktree; GitHub label state prevents two workers from taking the same item.",
)
@click.option("--work-dir", default=".", type=click.Path(exists=True), help="Working directory for the agent")
@click.option("--bot-name", default=None, help="Bot username for watermark detection (default: detected from gh auth)")
@click.option(
    "--verbose", "-v", is_flag=True,
    help=(
        "Enable DEBUG-level logging. Surfaces per-module decision points, "
        "Claude CLI stdout/stderr, and full GitHub API response data."
    ),
)
@click.option(
    "--log-file", default=None, type=click.Path(), metavar="PATH",
    help="Write DEBUG logs to a file in addition to stderr (useful for long-running daemon deployments).",
)
@click.option(
    "--allowed-users", "allowed_users", multiple=True, metavar="USER",
    help="GitHub usernames always permitted to trigger runs (repeatable). "
         "Use for external contributors not in the repo's collaborators list.",
)
@click.option(
    "--min-role", "min_role", default="triage", show_default=True,
    type=click.Choice(["triage", "write", "admin"], case_sensitive=False),
    help="Minimum GitHub collaborator role required to trigger agent runs.",
)
@click.option(
    "--stuck-threshold-hours", "stuck_threshold_hours", default=12, show_default=True,
    help="Hours after which an in-progress item is considered stuck and will be reset.",
)
@click.option(
    "--skip-ci-checks", "skip_ci_checks", multiple=True, metavar="NAME",
    help="CI check names to ignore when detecting failures (repeatable). "
         "E.g. --skip-ci-checks 'deploy-preview' --skip-ci-checks 'license/cla'.",
)
@click.option(
    "--quota-fallback-seconds", "quota_fallback_seconds", default=1800, show_default=True,
    help="Seconds to disable a Claude agent for when quota is hit and no reset time can be parsed.",
)
@click.option(
    "--repeated-failure-threshold", "repeated_failure_threshold", default=2, show_default=True,
    help="Consecutive identical bot failure comments before an item is marked in-error and skipped.",
)
def worker(**_) -> None:
    """Run the orchestrator worker loop for a single repository."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=config.settings.log_level, format=log_format)

    if config.settings.log_file:
        file_handler = logging.FileHandler(config.settings.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().info("Also writing DEBUG logs to %s", config.settings.log_file)

    work_path = Path(config.settings.work_dir).resolve()

    repo_name = config.settings.repo
    if repo_name is None:
        repo_name = Repo.detect(cwd=str(work_path))
        click.echo(f"Detected repo: {repo_name}")

    repo = Repo(repo_name, cwd=str(work_path))
    default_branch = repo.detect_default_branch()
    click.echo(f"Default branch: {default_branch}")
    git = GitRepo(work_dir=work_path, default_branch=default_branch)
    agents = [NullAgent(), CodingAgent(repo=repo_name), PlanningAgent(repo=repo_name)]

    orchestrator = Orchestrator(repo=repo, git=git, agents=agents)

    click.echo(
        f"Starting orchestrator for {repo_name} (polling every {config.settings.interval}s, "
        f"up to {orchestrator.max_concurrent} concurrent task(s))"
    )
    orchestrator.run()


@cli.command("supervisor")
@click.option("--base-dir", default=".", show_default=True,
              help="Base directory for repo checkouts (<base-dir>/<owner>/<repo>) and logs (<base-dir>/.logs/<owner>/<repo>/)")
@click.option("--interval", default=15, show_default=True,
              help="Health-check interval in seconds")
@click.option("--refresh-interval", default=1800, show_default=True,
              help="How often (seconds) to re-discover repos and checkout new ones")
@click.option("--include", "include_patterns", multiple=True, metavar="PATTERN",
              help="Only supervise repos matching this glob pattern (repeatable). "
                   "Matched against 'owner/repo'; patterns without '/' match repo name only.")
@click.option("--exclude", "exclude_patterns", multiple=True, metavar="PATTERN",
              help="Skip repos matching this glob pattern (repeatable). Applied after --include.")
@click.option("--min-restart-delay", default=5.0, show_default=True,
              help="Minimum seconds before restarting a crashed worker")
@click.option("--max-restart-delay", default=300.0, show_default=True,
              help="Maximum backoff delay (seconds) for restarting a crashed worker")
@click.option("--no-remote-control", "no_remote_control", is_flag=True,
              help="Do not launch a 'claude --remote-control' session per repo "
                   "(use in environments without Anthropic relay access to avoid restart churn).")
@click.option("--verbose", "-v", is_flag=True,
              help="Enable DEBUG logging in supervisor (workers log to their own files)")
@click.option("--log-file", default=None,
              help="Write supervisor DEBUG logs to this file")
@click.option("--accept-invites-from", "accept_invites_from", multiple=True, metavar="USER",
              help="Automatically accept repo invitations from these users. Repeatable. "
                   "Use '*' to accept from anyone (not recommended). "
                   "If omitted, no invitations are accepted.")
@click.argument("worker_args", nargs=-1, type=click.UNPROCESSED)
def supervisor_cmd(**_) -> None:
    """Discover all accessible repositories and run a worker for each in parallel.

    To forward arguments to each worker, append them after ``--``:

        loony-dev supervisor --base-dir /repos -- --interval 30 --bot-name mybot
    """
    from loony_dev.supervisor import run_supervisor

    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=config.settings.log_level, format=log_format)

    config.settings.supervisor_log.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(config.settings.supervisor_log)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().info("Also writing DEBUG logs to %s", config.settings.supervisor_log)

    run_supervisor()


@cli.command("web")
@click.option(
    "--base-dir", default=".", show_default=True,
    help="Base directory for log/PID discovery (same default as supervisor)",
)
@click.option(
    "--supervisor-log", default=None, type=click.Path(), metavar="PATH",
    help="Path to supervisor log file (default: <base-dir>/.logs/supervisor.log)",
)
@click.option(
    "--port", default=5338, show_default=True,
    help="Port for the web dashboard (bound to 127.0.0.1 only).",
)
@click.option(
    "--tail-lines", "tail_lines", default=100, show_default=True,
    help="Default number of log lines returned by the log-tail endpoint.",
)
@click.option(
    "--claude-home", "claude_home", default=None, type=click.Path(), metavar="PATH",
    help="Global Claude config root for the skills/commands editor (default: ~/.claude).",
)
@click.option(
    "--stuck-after", "stuck_after", default=300, show_default=True,
    help="Seconds a blocked Claude descendant must be alive before it is "
         "reported as stuck.",
)
@click.option(
    "--activity-sample", "activity_sample", default=0.3, show_default=True,
    help="Seconds between the two CPU/IO samples used to decide a Claude "
         "subtree is idle (only taken when a blocked candidate exists).",
)
@click.option(
    "--kill-grace", "kill_grace", default=5.0, show_default=True,
    help="Seconds to wait after SIGTERM before escalating to SIGKILL.",
)
def web_cmd(**_) -> None:
    """Launch the read-only web dashboard to monitor the supervisor and workers.

    The dashboard runs as a separate process from the supervisor and reads all
    state from the on-disk file layout under <base-dir>/.logs. It binds to
    127.0.0.1 only; tunnel in (e.g. SSH port-forward) to reach it remotely.
    """
    import uvicorn

    from loony_dev.web import create_app

    base_dir = config.settings.base_dir
    supervisor_log = config.settings.supervisor_log
    port = int(config.settings.get("port", 5338))
    tail_lines = int(config.settings.get("tail_lines", 100))
    claude_home_raw = config.settings.get("claude_home")
    claude_home = Path(claude_home_raw).expanduser() if claude_home_raw else None
    stuck_after = int(config.settings.get("stuck_after", 300))
    activity_sample = float(config.settings.get("activity_sample", 0.3))
    kill_grace = float(config.settings.get("kill_grace", 5.0))

    app = create_app(
        base_dir=base_dir,
        supervisor_log=supervisor_log,
        tail_lines=tail_lines,
        claude_home=claude_home,
        stuck_after_seconds=stuck_after,
        activity_sample_seconds=activity_sample,
        kill_grace_seconds=kill_grace,
    )
    click.echo(f"Serving loony-dev dashboard at http://127.0.0.1:{port} (base-dir: {base_dir})")
    uvicorn.run(app, host="127.0.0.1", port=port)


# Keep 'main' as an alias so existing scripts that imported it continue to work.
main = cli
