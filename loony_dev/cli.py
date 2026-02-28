from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.git import GitRepo
from loony_dev.github import GitHubClient
from loony_dev.orchestrator import Orchestrator


@click.group()
def cli() -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""


@cli.command("worker")
@click.option("--repo", default=None, help="owner/repo (default: detected from git remote)")
@click.option("--interval", default=60, help="Polling interval in seconds", show_default=True)
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
def worker(repo: str | None, interval: int, work_dir: str, bot_name: str, verbose: bool, log_file: str | None) -> None:
    """Run the orchestrator worker loop for a single repository."""
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().info("Also writing DEBUG logs to %s", log_file)

    work_path = Path(work_dir).resolve()

    if repo is None:
        repo = GitHubClient.detect_repo()
        click.echo(f"Detected repo: {repo}")

    if bot_name is None:
        bot_name = GitHubClient.detect_bot_name()
        click.echo(f"Detected bot name: {bot_name}")

    github = GitHubClient(repo=repo, bot_name=bot_name)
    git = GitRepo(work_dir=work_path)
    agents = [CodingAgent(work_dir=work_path), PlanningAgent(work_dir=work_path)]

    orchestrator = Orchestrator(
        github=github,
        git=git,
        agents=agents,
        interval=interval,
    )

    click.echo(f"Starting orchestrator for {repo} (polling every {interval}s)")
    orchestrator.run()


@cli.command("supervisor")
@click.option("--base-dir", default=".", show_default=True,
              help="Base directory for repo checkouts (<base-dir>/<owner>/<repo>) and logs (<base-dir>/.logs/<owner>/<repo>/)")
@click.option("--interval", default=15, show_default=True,
              help="Health-check interval in seconds")
@click.option("--worker-interval", default=60, show_default=True,
              help="Polling interval forwarded to each worker")
@click.option("--refresh-interval", default=1800, show_default=True,
              help="How often (seconds) to re-discover repos and checkout new ones")
@click.option("--bot-name", default=None,
              help="Bot username forwarded to workers")
@click.option("--include", "include_patterns", multiple=True, metavar="PATTERN",
              help="Only supervise repos matching this glob pattern (repeatable). "
                   "Matched against 'owner/repo'; patterns without '/' match repo name only.")
@click.option("--exclude", "exclude_patterns", multiple=True, metavar="PATTERN",
              help="Skip repos matching this glob pattern (repeatable). Applied after --include.")
@click.option("--min-restart-delay", default=5.0, show_default=True,
              help="Minimum seconds before restarting a crashed worker")
@click.option("--max-restart-delay", default=300.0, show_default=True,
              help="Maximum backoff delay (seconds) for restarting a crashed worker")
@click.option("--verbose", "-v", is_flag=True,
              help="Enable DEBUG logging in supervisor (workers log to their own files)")
@click.option("--log-file", default=None,
              help="Write supervisor DEBUG logs to this file")
def supervisor_cmd(
    base_dir: str,
    interval: int,
    worker_interval: int,
    refresh_interval: int,
    bot_name: str | None,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    min_restart_delay: float,
    max_restart_delay: float,
    verbose: bool,
    log_file: str | None,
) -> None:
    """Discover all accessible repositories and run a worker for each in parallel."""
    from loony_dev.supervisor import run_supervisor

    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)

    base_path = Path(base_dir).resolve()

    if bot_name is None:
        bot_name = GitHubClient.detect_bot_name()
        click.echo(f"Detected bot name: {bot_name}")

    include = list(include_patterns) if include_patterns else None
    exclude = list(exclude_patterns) if exclude_patterns else None

    run_supervisor(
        base_dir=base_path,
        interval=interval,
        worker_interval=worker_interval,
        bot_name=bot_name,
        verbose=verbose,
        log_file=log_file,
        min_restart_delay=min_restart_delay,
        max_restart_delay=max_restart_delay,
        include=include,
        exclude=exclude,
        refresh_interval=refresh_interval,
    )


# Keep 'main' as an alias so existing scripts that imported it continue to work.
main = cli
