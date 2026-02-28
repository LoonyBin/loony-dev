from __future__ import annotations

import fnmatch
import logging
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from loony_dev.github import GitHubClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------

def list_accessible_repos() -> list[str]:
    """Return 'owner/repo' strings for all repos accessible to the authenticated gh user.

    Uses the GitHub REST API /user/repos?type=all endpoint, which covers owned repos,
    org repos (member or collaborator), and external collaborator repos.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", "/user/repos",
                "--paginate",
                "-X", "GET",
                "-f", "type=all",
                "-f", "per_page=100",
                "--jq", ".[].full_name",
            ],
            capture_output=True, text=True, check=True,
        )
        repos = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return sorted(set(repos))
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to list accessible repos via gh api /user/repos: %s", exc.stderr.strip())
        return []
    except Exception:
        logger.exception("Unexpected error listing accessible repos")
        return []


# ---------------------------------------------------------------------------
# Repository filtering
# ---------------------------------------------------------------------------

def _matches_pattern(repo: str, pattern: str) -> bool:
    """Return True if *repo* ('owner/repo') matches *pattern*.

    Patterns containing '/' are matched against the full 'owner/repo' string.
    Patterns without '/' are matched against the repo name portion only.
    Matching uses fnmatch (case-sensitive on Linux).
    """
    if "/" in pattern:
        return fnmatch.fnmatch(repo, pattern)
    # Match against the repo name portion only
    repo_name = repo.split("/", 1)[-1]
    return fnmatch.fnmatch(repo_name, pattern)


def filter_repos(
    repos: list[str],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[str]:
    """Filter 'owner/repo' strings by include/exclude glob patterns.

    - If *include* patterns are given, only repos matching at least one pattern are kept.
    - Then, any repo matching at least one *exclude* pattern is removed.
    - Patterns with '/' are matched against 'owner/repo'; patterns without '/' match
      the repo name portion only.
    """
    result = repos

    if include:
        result = [r for r in result if any(_matches_pattern(r, p) for p in include)]

    if exclude:
        result = [r for r in result if not any(_matches_pattern(r, p) for p in exclude)]

    return result


# ---------------------------------------------------------------------------
# Repository checkout / removal
# ---------------------------------------------------------------------------

def ensure_repo_checked_out(repo: str, base_dir: Path) -> Path:
    """Ensure 'owner/repo' is cloned at base_dir/owner/repo.

    Clones if not present; skips if the directory is already a git repo.
    Returns the local path.
    """
    owner, name = repo.split("/", 1)
    owner_dir = base_dir / owner
    repo_dir = owner_dir / name

    owner_dir.mkdir(parents=True, exist_ok=True)

    if (repo_dir / ".git").exists():
        logger.debug("Repo %s already cloned at %s, skipping.", repo, repo_dir)
        return repo_dir

    logger.info("Cloning %s into %s …", repo, repo_dir)
    try:
        subprocess.run(
            ["gh", "repo", "clone", repo, str(repo_dir)],
            check=True,
        )
        logger.info("Cloned %s successfully.", repo)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to clone %s: exit code %d", repo, exc.returncode)
        raise

    return repo_dir


def remove_repo(repo: str, base_dir: Path) -> None:
    """Remove the checkout directory for *repo* at base_dir/owner/repo.

    Does NOT remove the log directory. If the owner directory becomes empty
    after removal, removes it too.
    """
    owner, name = repo.split("/", 1)
    repo_dir = base_dir / owner / name

    if repo_dir.exists():
        logger.info("Removing checkout directory %s", repo_dir)
        shutil.rmtree(repo_dir)
    else:
        logger.debug("Checkout directory %s does not exist, nothing to remove.", repo_dir)

    owner_dir = base_dir / owner
    try:
        if owner_dir.exists() and not any(owner_dir.iterdir()):
            logger.debug("Owner directory %s is empty, removing.", owner_dir)
            owner_dir.rmdir()
    except OSError:
        logger.debug("Could not remove owner directory %s", owner_dir)


# ---------------------------------------------------------------------------
# Worker process management
# ---------------------------------------------------------------------------

@dataclass
class WorkerProcess:
    repo: str
    work_dir: Path
    log_file: Path
    process: subprocess.Popen
    started_at: float
    restart_count: int = field(default=0)


def _worker_command(repo: str, work_dir: Path, worker_interval: int, bot_name: str | None, verbose: bool, log_file: Path) -> list[str]:
    """Build the argv list for a worker subprocess."""
    # Prefer the installed entry point; fall back to running the module directly.
    cmd_prefix: list[str]
    if shutil.which("loony-dev"):
        cmd_prefix = ["loony-dev"]
    else:
        cmd_prefix = [sys.executable, "-m", "loony_dev.cli"]

    cmd = cmd_prefix + [
        "worker",
        "--repo", repo,
        "--work-dir", str(work_dir),
        "--interval", str(worker_interval),
        "--log-file", str(log_file),
    ]
    if bot_name:
        cmd += ["--bot-name", bot_name]
    if verbose:
        cmd += ["--verbose"]
    return cmd


def launch_worker(
    repo: str,
    work_dir: Path,
    log_file: Path,
    worker_interval: int,
    bot_name: str | None,
    verbose: bool,
) -> WorkerProcess:
    """Spawn a worker subprocess; stdout/stderr are redirected to *log_file*."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = _worker_command(repo, work_dir, worker_interval, bot_name, verbose, log_file)
    logger.info("Launching worker for %s (log: %s)", repo, log_file)

    log_fh = open(log_file, "a")  # noqa: SIM115  — intentionally kept open
    process = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh)
    log_fh.close()  # Popen inherits the fd; close our copy

    return WorkerProcess(
        repo=repo,
        work_dir=work_dir,
        log_file=log_file,
        process=process,
        started_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

def _terminate_worker(wp: WorkerProcess, timeout: float = 10.0) -> None:
    """Send SIGTERM, wait *timeout* seconds, then SIGKILL if still alive."""
    try:
        wp.process.terminate()
    except OSError:
        return
    try:
        wp.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Worker for %s did not stop in %.0fs; killing.", wp.repo, timeout)
        try:
            wp.process.kill()
        except OSError:
            pass
        try:
            wp.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_supervisor(
    base_dir: Path,
    interval: int,
    worker_interval: int,
    bot_name: str | None,
    verbose: bool,
    log_file: str | None,
    min_restart_delay: float,
    max_restart_delay: float,
    include: list[str] | None,
    exclude: list[str] | None,
    refresh_interval: int,
) -> None:
    """Discover repositories, check them out, and run a worker for each.

    Runs until interrupted by SIGINT or SIGTERM.
    """
    base_dir.mkdir(parents=True, exist_ok=True)

    workers: dict[str, WorkerProcess] = {}
    shutdown_requested = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal shutdown_requested
        logger.info("Signal %d received; shutting down supervisor…", signum)
        shutdown_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    last_discovery: float = 0.0  # Force discovery on first iteration

    logger.info(
        "Supervisor started. base_dir=%s interval=%ds refresh=%ds",
        base_dir, interval, refresh_interval,
    )
    if include:
        logger.info("Include patterns: %s", include)
    if exclude:
        logger.info("Exclude patterns: %s", exclude)

    while not shutdown_requested:
        now = time.monotonic()

        # ------------------------------------------------------------------ #
        # Discovery phase
        # ------------------------------------------------------------------ #
        if now - last_discovery >= refresh_interval:
            last_discovery = now
            logger.info("Running repo discovery…")

            all_repos = list_accessible_repos()
            if not all_repos:
                logger.warning("No repos discovered (gh returned nothing or failed). Will retry on next refresh.")
            else:
                active = filter_repos(all_repos, include, exclude)
                logger.info(
                    "Discovered %d repos; %d match filters.",
                    len(all_repos), len(active),
                )
                if not active:
                    logger.warning(
                        "No repos matched the current include/exclude patterns. "
                        "Supervisor is idle; check your --include/--exclude options."
                    )

                active_set = set(active)
                current_set = set(workers.keys())

                # Start workers for new repos
                for repo in active:
                    if repo in workers:
                        continue
                    try:
                        work_dir = ensure_repo_checked_out(repo, base_dir)
                    except Exception:
                        logger.error("Skipping %s this cycle due to clone failure.", repo)
                        continue

                    GitHubClient(repo, bot_name or "").ensure_required_labels()

                    owner, name = repo.split("/", 1)
                    log_path = base_dir / ".logs" / owner / name / "loony-worker.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)

                    try:
                        client = GitHubClient(repo, bot_name or "")
                        client.ensure_required_labels()
                    except Exception:
                        logger.warning("Label provisioning failed for %s; continuing to launch worker.", repo)

                    try:
                        wp = launch_worker(
                            repo=repo,
                            work_dir=work_dir,
                            log_file=log_path,
                            worker_interval=worker_interval,
                            bot_name=bot_name,
                            verbose=verbose,
                        )
                        workers[repo] = wp
                    except Exception:
                        logger.exception("Failed to launch worker for %s", repo)

                # Stop workers for repos no longer in the active set
                for repo in current_set - active_set:
                    wp = workers.pop(repo)
                    logger.info(
                        "Repo %s is no longer active (filtered out, deleted, archived, or access revoked); "
                        "worker stopped and checkout deleted. Logs preserved at %s",
                        repo, wp.log_file.parent,
                    )
                    _terminate_worker(wp)
                    remove_repo(repo, base_dir)

        if shutdown_requested:
            break

        # ------------------------------------------------------------------ #
        # Health-check phase
        # ------------------------------------------------------------------ #
        for repo, wp in list(workers.items()):
            rc = wp.process.poll()
            if rc is None:
                continue  # Still running

            logger.warning(
                "Worker for %s exited with code %d (restart #%d).",
                repo, rc, wp.restart_count + 1,
            )

            delay = min(min_restart_delay * (2 ** wp.restart_count), max_restart_delay)
            logger.info("Restarting worker for %s in %.1fs…", repo, delay)

            # Interruptible delay
            deadline = time.monotonic() + delay
            while not shutdown_requested and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))

            if shutdown_requested:
                break

            try:
                new_wp = launch_worker(
                    repo=repo,
                    work_dir=wp.work_dir,
                    log_file=wp.log_file,
                    worker_interval=worker_interval,
                    bot_name=bot_name,
                    verbose=verbose,
                )
                new_wp.restart_count = wp.restart_count + 1
                workers[repo] = new_wp
            except Exception:
                logger.exception("Failed to restart worker for %s", repo)

        if shutdown_requested:
            break

        # Interruptible sleep for the health-check interval
        sleep_deadline = time.monotonic() + interval
        while not shutdown_requested and time.monotonic() < sleep_deadline:
            time.sleep(min(1.0, sleep_deadline - time.monotonic()))

    # ---------------------------------------------------------------------- #
    # Graceful shutdown
    # ---------------------------------------------------------------------- #
    logger.info("Stopping all workers…")
    for repo, wp in workers.items():
        logger.info("Stopping worker for %s", repo)
        _terminate_worker(wp)
    logger.info("Supervisor stopped.")
