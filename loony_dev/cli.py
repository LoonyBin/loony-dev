from __future__ import annotations

import logging
import subprocess
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
    agents = [NullAgent(), CodingAgent(work_dir=work_path, repo=repo_name), PlanningAgent(work_dir=work_path, repo=repo_name)]

    orchestrator = Orchestrator(repo=repo, git=git, agents=agents)

    click.echo(f"Starting orchestrator for {repo_name} (polling every {config.settings.interval}s)")
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


@cli.command("ui")
@click.option(
    "--base-dir", default=".", show_default=True,
    help="Base directory for log/PID discovery (same default as supervisor)",
)
@click.option(
    "--supervisor-log", default=None, type=click.Path(), metavar="PATH",
    help="Path to supervisor log file (default: <base-dir>/.logs/supervisor.log)",
)
@click.option(
    "--scan-interval", default=5, show_default=True,
    help="How often (seconds) to re-scan for new/removed workers",
)
@click.option(
    "--max-buffer-lines", "max_buffer_lines", default=5000, show_default=True,
    help="Maximum log lines to keep in memory per worker log.",
)
@click.option(
    "--tail-lines", "tail_lines", default=100, show_default=True,
    help="Log lines to render initially; the rest are loaded lazily on scroll-up.",
)
def ui_cmd(**_) -> None:
    """Launch the terminal UI to monitor the supervisor and workers."""
    from loony_dev.tui import SupervisorApp

    app = SupervisorApp()
    app.run()


@cli.command("project-manager")
@click.option("--repo", default=None, help="owner/repo (default: detected from git remote)")
@click.option("--n", default=1, show_default=True, help="Max issues to keep in progress simultaneously.")
@click.option("--interval", default=120, show_default=True, help="Polling interval in seconds.")
@click.option(
    "--skip-planning", "skip_planning", is_flag=True, default=False,
    help="Label issues ready-for-development directly (skip planning stage).",
)
@click.option(
    "--skip-merge", "skip_merge", is_flag=True, default=False,
    help="Do not auto-merge PRs; leave merge to a human reviewer.",
)
@click.option(
    "--merge-delay", "merge_delay", default=86400, show_default=True,
    help="Seconds to wait after CI passes before auto-merging (default: 24h).",
)
@click.option(
    "--deploy-workflow", "deploy_workflow", default="deploy", show_default=True,
    help="Name of the deployment workflow (without .yml) to verify after merge. "
         "Set to empty string to skip deployment verification.",
)
@click.option(
    "--milestone-soon-days", "milestone_soon_days", default=14, show_default=True,
    help="Days until a milestone counts as 'due soon' for prioritisation.",
)
@click.option(
    "--shortlist-size", "shortlist_size", default=5, show_default=True,
    help="Number of candidates forwarded to the AI agent in Phase 2.",
)
@click.option(
    "--ai-model", "ai_model", default="claude-opus-4-6", show_default=True,
    help="Claude model used for Phase-2 candidate ranking.",
)
@click.option(
    "--verbose", "-v", is_flag=True,
    help="Enable DEBUG-level logging.",
)
@click.option(
    "--log-file", default=None, type=click.Path(), metavar="PATH",
    help="Write DEBUG logs to a file in addition to stderr.",
)
def project_manager_cmd(**_) -> None:
    """Prioritise open issues and drive them through the worker pipeline."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=config.settings.log_level, format=log_format)

    if config.settings.log_file:
        file_handler = logging.FileHandler(config.settings.log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().info("Also writing DEBUG logs to %s", config.settings.log_file)

    repo = config.settings.repo
    if repo is None:
        try:
            repo = Repo.detect()
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            click.echo(
                "Could not detect a GitHub repository. Pass --repo or run inside a repository checkout."
                + (f"\n{detail}" if detail else ""),
                err=True,
            )
            raise SystemExit(2) from exc
        click.echo(f"Detected repo: {repo}")

    github = Repo(repo)

    from loony_dev.project_manager import ProjectManager

    # Read project_manager-specific settings, falling back to CLI-supplied values.
    pm_cfg = config.settings.get("project_manager") or {}

    manager = ProjectManager(
        github=github,
        n=int(pm_cfg.get("n", config.settings.n)),
        interval=int(pm_cfg.get("interval", config.settings.interval)),
        skip_planning=bool(pm_cfg.get("skip_planning", config.settings.skip_planning)),
        skip_merge=bool(pm_cfg.get("skip_merge", config.settings.skip_merge)),
        merge_delay=int(pm_cfg.get("merge_delay", config.settings.merge_delay)),
        deploy_workflow=pm_cfg.get("deploy_workflow", config.settings.deploy_workflow) or None,
        milestone_soon_days=int(pm_cfg.get("milestone_soon_days", config.settings.milestone_soon_days)),
        milestone_cache_ttl=float(
            pm_cfg.get("milestone_cache_ttl", getattr(config.settings, "milestone_cache_ttl", 3600.0))
        ),
        shortlist_size=int(pm_cfg.get("shortlist_size", config.settings.shortlist_size)),
        dependency_patterns=pm_cfg.get(
            "dependency_patterns",
            getattr(config.settings, "dependency_patterns", None),
        ),
        ai_model=str(pm_cfg.get("ai_model", config.settings.ai_model)),
    )

    click.echo(
        f"Starting project-manager for {repo} "
        f"(n={manager.n}, interval={manager.interval}s, "
        f"skip_merge={manager.skip_merge})"
    )
    manager.run()


# Keep 'main' as an alias so existing scripts that imported it continue to work.
main = cli
