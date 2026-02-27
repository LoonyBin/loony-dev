from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev.agents.coding import CodingAgent
from loony_dev.agents.planning import PlanningAgent
from loony_dev.git import GitRepo
from loony_dev.github import GitHubClient
from loony_dev.orchestrator import Orchestrator


@click.command()
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
def main(repo: str | None, interval: int, work_dir: str, bot_name: str, verbose: bool, log_file: str | None) -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""
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
