from __future__ import annotations

import logging
from pathlib import Path

import click

from loony_dev.agents.coding import CodingAgent
from loony_dev.git import GitRepo
from loony_dev.github import GitHubClient
from loony_dev.orchestrator import Orchestrator


@click.command()
@click.option("--repo", default=None, help="owner/repo (default: detected from git remote)")
@click.option("--interval", default=60, help="Polling interval in seconds", show_default=True)
@click.option("--work-dir", default=".", type=click.Path(exists=True), help="Working directory for the agent")
@click.option("--bot-name", default="loony-dev[bot]", help="Bot username for watermark detection", show_default=True)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(repo: str | None, interval: int, work_dir: str, bot_name: str, verbose: bool) -> None:
    """Loony-Dev: Agent orchestrator that watches GitHub and dispatches work."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    work_path = Path(work_dir).resolve()

    if repo is None:
        repo = GitHubClient.detect_repo()
        click.echo(f"Detected repo: {repo}")

    github = GitHubClient(repo=repo, bot_name=bot_name)
    git = GitRepo(work_dir=work_path)
    agents = [CodingAgent(work_dir=work_path)]

    orchestrator = Orchestrator(
        github=github,
        git=git,
        agents=agents,
        interval=interval,
    )

    click.echo(f"Starting orchestrator for {repo} (polling every {interval}s)")
    orchestrator.run()
