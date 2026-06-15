from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev import config
from loony_dev.agents import session_hooks
from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.null_agent import NullAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.commands import install_commands
from loony_dev.git import GitRepo
from loony_dev.github import Repo
from loony_dev.orchestrator import Orchestrator


@click.group(cls=config.ClickGroup)
def cli() -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""


@cli.command("setup")
def setup_cmd() -> None:
    """Report Claude Code hook configuration (no global install required).

    loony-dev no longer installs hooks into ``~/.claude/settings.json``. Instead
    it passes its SessionStart/Stop/PreToolUse/PostToolUse hooks to each session
    it launches via ``claude --settings`` (see
    :mod:`loony_dev.agents.session_hooks`), so the hooks apply only to
    loony-managed sessions and never to your own ``claude`` invocations. This
    command exists for backward compatibility and prints the hook command that
    will be used.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    click.echo(
        "loony-dev hooks are applied per-session via `claude --settings`; "
        "no global install is needed."
    )
    click.echo(f"Hook command example (Stop): {session_hooks.hook_command('Stop')}")


@cli.command("hook", context_settings={"ignore_unknown_options": True})
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def hook_cmd(args: tuple[str, ...]) -> None:
    """Internal: the executable Claude Code invokes for each lifecycle hook.

    Reads the hook payload on stdin and writes one event line to the session's
    control socket. Not meant to be run by hand — settings.json wires it up.
    """
    import sys

    raise SystemExit(session_hooks.run_hook(list(args), sys.stdin.read()))


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

    # Install/upgrade the bundled slash commands into <repo-checkout>/.claude/commands/
    # so workers and attached operators share the same prompt vocabulary (#165).
    try:
        written = install_commands(work_path)
        if written:
            click.echo(f"Installed {len(written)} loony-dev slash command(s) into {work_path / '.claude' / 'commands'}")
    except OSError as e:
        logging.getLogger(__name__).warning("Failed to install slash commands: %s", e)

    # Hook-driven session events (#178) are wired per-session: each ClaudeSession
    # launches ``claude --settings <json>`` carrying loony-dev's lifecycle hooks
    # (see loony_dev.agents.session_hooks), so there is no global settings.json
    # state to install or verify here — and a human's own ``claude`` runs are
    # never affected.

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
    "--host", default="127.0.0.1", show_default=True,
    help="Address to bind the dashboard to. Defaults to loopback. WARNING: the "
         "dashboard exposes mutating endpoints (write skills/commands, kill "
         "processes, attach to / steer a task's Claude session) and has no auth "
         "— only bind to a non-loopback/0.0.0.0 address on a trusted network.",
)
@click.option(
    "--port", default=5338, show_default=True,
    help="Port for the web dashboard. Defaults to loopback (127.0.0.1); make it "
         "reachable remotely via --host or an SSH port-forward.",
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
@click.option(
    "--auto-interrupt-after", "auto_interrupt_after", default=0.0, show_default=True,
    help="Seconds a Claude turn may stay stuck before the dashboard ESC-interrupts "
         "it automatically. 0 disables auto-intervention (SIGKILL is never "
         "auto-escalated).",
)
def web_cmd(**_) -> None:
    """Launch the read-only web dashboard to monitor the supervisor and workers.

    The dashboard runs as a separate process from the supervisor and reads all
    state from the on-disk file layout under <base-dir>/.logs. It binds to
    127.0.0.1 by default; tunnel in (e.g. SSH port-forward) to reach it
    remotely, or use --host to bind another address (see the --host warning).
    """
    import uvicorn

    from loony_dev.web import create_app

    base_dir = config.settings.base_dir
    supervisor_log = config.settings.supervisor_log
    host = config.settings.get("host", "127.0.0.1")
    port = int(config.settings.get("port", 5338))
    tail_lines = int(config.settings.get("tail_lines", 100))
    claude_home_raw = config.settings.get("claude_home")
    claude_home = Path(claude_home_raw).expanduser() if claude_home_raw else None
    stuck_after = int(config.settings.get("stuck_after", 300))
    activity_sample = float(config.settings.get("activity_sample", 0.3))
    kill_grace = float(config.settings.get("kill_grace", 5.0))
    auto_interrupt_after = float(config.settings.get("auto_interrupt_after", 0.0))

    app = create_app(
        base_dir=base_dir,
        supervisor_log=supervisor_log,
        tail_lines=tail_lines,
        claude_home=claude_home,
        stuck_after_seconds=stuck_after,
        activity_sample_seconds=activity_sample,
        kill_grace_seconds=kill_grace,
        auto_interrupt_after_seconds=auto_interrupt_after,
    )
    click.echo(f"Serving loony-dev dashboard at http://{host}:{port} (base-dir: {base_dir})")
    uvicorn.run(app, host=host, port=port)


# Keep 'main' as an alias so existing scripts that imported it continue to work.
main = cli
