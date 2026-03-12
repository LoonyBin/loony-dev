from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev import config
from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.null_agent import NullAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.git import GitRepo
from loony_dev.github import GitHubClient
from loony_dev.orchestrator import Orchestrator

# ``config.settings`` is read at import time (before CLI overrides are applied)
# so it reflects config-file and built-in defaults — the same values that were
# previously exposed via a separate ``_defaults = config.new_settings()`` call.
_s = config.settings


@click.group()
def cli() -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""


@cli.command("worker")
@click.option("--repo", default=None, help="owner/repo (default: detected from git remote)")
@click.option(
    "--interval", default=_s.WORKER.INTERVAL, type=int,
    help="Polling interval in seconds",
)
@click.option("--work-dir", default=None, type=click.Path(exists=True), help="Working directory for the agent")
@click.option("--bot-name", default=None, help="Bot username for watermark detection (default: detected from gh auth)")
@click.option(
    "--verbose", "-v", is_flag=True, default=None,
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
    "--min-role", "min_role", default=_s.MIN_ROLE,
    type=click.Choice(["triage", "write", "admin"], case_sensitive=False),
    help="Minimum GitHub collaborator role required to trigger agent runs.",
)
def worker(
    repo: str | None,
    interval: int | None,
    work_dir: str | None,
    bot_name: str | None,
    verbose: bool | None,
    log_file: str | None,
    allowed_users: tuple[str, ...],
    min_role: str | None,
) -> None:
    """Run the orchestrator worker loop for a single repository."""
    config.initialize({
        "worker.repo": repo,
        "worker.interval": interval,
        "worker.work_dir": work_dir,
        "bot_name": bot_name,
        "verbose": verbose if verbose else None,
        "log_file": log_file,
        "allowed_users": list(allowed_users) if allowed_users else None,
        "min_role": min_role,
    })

    log_level = logging.DEBUG if config.settings.VERBOSE else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    configured_log_file = config.settings.get("LOG_FILE") or ""
    if configured_log_file:
        file_handler = logging.FileHandler(configured_log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().info("Also writing DEBUG logs to %s", configured_log_file)

    work_path = Path(config.settings.WORKER.WORK_DIR).resolve()
    final_repo = config.settings.WORKER.REPO

    github = GitHubClient(repo=final_repo)
    git = GitRepo(work_dir=work_path)
    agents = [NullAgent(), CodingAgent(), PlanningAgent()]

    orchestrator = Orchestrator(github=github, git=git, agents=agents)

    click.echo(
        f"Starting orchestrator for {final_repo} "
        f"(polling every {config.settings.WORKER.INTERVAL}s)"
    )
    orchestrator.run()


@cli.command("supervisor")
@click.option(
    "--base-dir", default=_s.SUPERVISOR.BASE_DIR,
    help="Base directory for repo checkouts and logs",
)
@click.option(
    "--interval", default=_s.SUPERVISOR.INTERVAL, type=int,
    help="Health-check interval in seconds",
)
@click.option(
    "--worker-interval", default=None, type=int,
    help="Polling interval (seconds) forwarded to each worker. "
         "When not set, each worker uses its own configuration.",
)
@click.option(
    "--refresh-interval", default=_s.SUPERVISOR.REFRESH_INTERVAL, type=int,
    help="How often (seconds) to re-discover repos and checkout new ones",
)
@click.option("--bot-name", default=None,
              help="Bot username forwarded to workers")
@click.option("--include", "include_patterns", multiple=True, metavar="PATTERN",
              help="Only supervise repos matching this glob pattern (repeatable). "
                   "Matched against 'owner/repo'; patterns without '/' match repo name only.")
@click.option("--exclude", "exclude_patterns", multiple=True, metavar="PATTERN",
              help="Skip repos matching this glob pattern (repeatable). Applied after --include.")
@click.option(
    "--min-restart-delay", default=_s.SUPERVISOR.MIN_RESTART_DELAY, type=float,
    help="Minimum seconds before restarting a crashed worker",
)
@click.option(
    "--max-restart-delay", default=_s.SUPERVISOR.MAX_RESTART_DELAY, type=float,
    help="Maximum backoff delay (seconds) for restarting a crashed worker",
)
@click.option("--verbose", "-v", is_flag=True, default=None,
              help="Enable DEBUG logging in supervisor (workers log to their own files)")
@click.option("--log-file", default=None,
              help="Write supervisor DEBUG logs to this file")
@click.option(
    "--allowed-users", "allowed_users", multiple=True, metavar="USER",
    help="GitHub usernames always permitted to trigger runs (repeatable). Forwarded to each worker.",
)
@click.option(
    "--min-role", "min_role", default=_s.MIN_ROLE,
    type=click.Choice(["triage", "write", "admin"], case_sensitive=False),
    help="Minimum GitHub collaborator role required to trigger runs. Forwarded to each worker.",
)
def supervisor_cmd(
    base_dir: str | None,
    interval: int | None,
    worker_interval: int | None,
    refresh_interval: int | None,
    bot_name: str | None,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    min_restart_delay: float | None,
    max_restart_delay: float | None,
    verbose: bool | None,
    log_file: str | None,
    allowed_users: tuple[str, ...],
    min_role: str | None,
) -> None:
    """Discover all accessible repositories and run a worker for each in parallel."""
    from loony_dev.supervisor import run_supervisor

    config.initialize({
        "supervisor.base_dir": base_dir,
        "supervisor.interval": interval,
        "supervisor.worker_interval": worker_interval,
        "supervisor.refresh_interval": refresh_interval,
        "supervisor.min_restart_delay": min_restart_delay,
        "supervisor.max_restart_delay": max_restart_delay,
        "supervisor.include": list(include_patterns) if include_patterns else None,
        "supervisor.exclude": list(exclude_patterns) if exclude_patterns else None,
        "bot_name": bot_name,
        "verbose": verbose if verbose else None,
        "log_file": log_file,
        "allowed_users": list(allowed_users) if allowed_users else None,
        "min_role": min_role,
    })

    log_level = logging.DEBUG if config.settings.VERBOSE else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    base_path = Path(config.settings.SUPERVISOR.BASE_DIR).resolve()
    logs_dir = base_path / ".logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    configured_log_file = config.settings.get("LOG_FILE") or ""
    if not configured_log_file:
        configured_log_file = str(logs_dir / "supervisor.log")

    file_handler = logging.FileHandler(configured_log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().info("Also writing DEBUG logs to %s", configured_log_file)

    run_supervisor(base_dir=base_path)


@cli.command("ui")
@click.option(
    "--base-dir", default=_s.UI.BASE_DIR,
    help="Base directory for log/PID discovery",
)
@click.option(
    "--supervisor-log", default=None, type=click.Path(), metavar="PATH",
    help="Path to supervisor log file (default: <base-dir>/.logs/supervisor.log)",
)
@click.option(
    "--scan-interval", default=_s.UI.SCAN_INTERVAL, type=int,
    help="How often (seconds) to re-scan for new/removed workers",
)
def ui_cmd(base_dir: str | None, supervisor_log: str | None, scan_interval: int | None) -> None:
    """Launch the terminal UI to monitor the supervisor and workers."""
    from loony_dev.tui import SupervisorApp

    config.initialize({
        "ui.base_dir": base_dir,
        "ui.supervisor_log": supervisor_log,
        "ui.scan_interval": scan_interval,
    })

    base_path = Path(config.settings.UI.BASE_DIR).resolve()
    configured_sup_log = config.settings.get("UI", {}).get("SUPERVISOR_LOG") or ""
    sup_log = Path(configured_sup_log) if configured_sup_log else base_path / ".logs" / "supervisor.log"

    app = SupervisorApp(
        base_dir=base_path,
        supervisor_log=sup_log,
        scan_interval=float(config.settings.UI.SCAN_INTERVAL),
    )
    app.run()


# Keep 'main' as an alias so existing scripts that imported it continue to work.
main = cli
