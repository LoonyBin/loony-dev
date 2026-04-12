from __future__ import annotations

import fnmatch
import logging
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time

from dataclasses import dataclass, field
from pathlib import Path

from loony_dev import config
from loony_dev.github import GitHubClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def _write_pid_file(path: Path, pid: int) -> None:
    """Write *pid* to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(str(pid))
    except OSError as exc:
        logger.warning("Failed to write PID file %s: %s", path, exc)


def _remove_pid_file(path: Path) -> None:
    """Remove the PID file at *path* if it exists."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove PID file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Invitation acceptance
# ---------------------------------------------------------------------------

def list_pending_invitations() -> list[dict]:
    """Return all pending repository invitation objects for the authenticated user."""
    import json
    try:
        result = subprocess.run(
            ["gh", "api", "/user/repository_invitations", "--paginate"],
            capture_output=True, text=True, check=True,
        )
        # --paginate emits one JSON array per page; merge them all
        invitations: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
                if isinstance(chunk, list):
                    invitations.extend(chunk)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse invitation response chunk: %s", exc)
        return invitations
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to list pending invitations: %s", exc.stderr.strip())
        return []
    except Exception:
        logger.exception("Unexpected error listing pending invitations")
        return []


def accept_pending_invitations() -> list[str]:
    """Accept pending repository invitations from configured users.

    Reads ``config.settings.accept_invites_from`` to determine which inviters
    are trusted.  Returns a list of accepted 'owner/repo' strings.
    """
    accept_from: tuple[str, ...] = config.settings.get("accept_invites_from") or ()
    if not accept_from:
        return []

    wildcard = accept_from == ("*",)
    if wildcard:
        logger.warning(
            "--accept-invites-from='*' is set. The agent will accept repository invitations "
            "from ANY GitHub user. This allows anyone to inject arbitrary repositories into this "
            "agent's workspace. Only use this in fully trusted environments."
        )

    invitations = list_pending_invitations()
    if not invitations:
        return []

    accepted: list[str] = []
    skipped = 0

    for inv in invitations:
        inv_id = inv.get("id")
        repo = (inv.get("repository") or {}).get("full_name", "<unknown>")
        inviter = (inv.get("inviter") or {}).get("login", "<unknown>")

        if not wildcard and inviter not in accept_from:
            logger.debug("Skipping invitation to %s from %s (not in accept_invites_from)", repo, inviter)
            skipped += 1
            continue

        try:
            subprocess.run(
                ["gh", "api", "--method", "PATCH", f"/user/repository_invitations/{inv_id}"],
                capture_output=True, text=True, check=True,
            )
            logger.info("Accepted invitation to %s from %s", repo, inviter)
            accepted.append(repo)
        except subprocess.CalledProcessError as exc:
            logger.warning("Failed to accept invitation %s to %s: %s", inv_id, repo, exc.stderr.strip())

    if skipped and not accepted:
        logger.info("No pending invitations from configured users")

    return accepted


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

def _configure_git_hooks(repo: str, repo_dir: Path) -> None:
    """If *repo_dir* contains a .githooks directory, configure git to use it."""
    githooks_dir = repo_dir / ".githooks"
    if not githooks_dir.is_dir():
        return
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Configured .githooks as hooks path for %s", repo)
    except subprocess.CalledProcessError as exc:
        logger.warning("Failed to configure .githooks for %s: %s", repo, exc.stderr.strip())


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
    pid_file: Path
    process: multiprocessing.Process
    started_at: float
    restart_count: int = field(default=0)


def _run_worker_process(log_file: Path, cmd_args: list[str]) -> None:
    """Entry point for a worker multiprocessing.Process.

    Redirects sys.stdout and sys.stderr to *log_file*, then invokes the CLI.
    """
    import os
    import sys

    # Duplicate file descriptors so subprocesses (e.g. claude) log here too.
    with open(log_file, "a") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

    # Update Python-level standard streams just in case
    log_file_obj = open(log_file, "a")
    sys.stdout = log_file_obj
    sys.stderr = log_file_obj

    from loony_dev.cli import cli
    try:
        cli.main(args=cmd_args, prog_name="loony-dev")
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


def launch_worker(repo: str, work_dir: Path, log_file: Path, pid_file: Path) -> WorkerProcess:
    """Spawn a worker multiprocessing.Process; stdout/stderr are redirected to *log_file*."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd_args = ["worker", "--repo", repo, "--work-dir", str(work_dir)]
    extra = config.settings.get("worker_args") or ()
    if extra:
        cmd_args += list(extra)

    logger.info("Launching worker for %s (log: %s)", repo, log_file)

    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=_run_worker_process,
        args=(log_file, cmd_args),
        name=f"worker-{repo.replace('/', '-')}"
    )
    process.start()

    _write_pid_file(pid_file, process.pid)

    return WorkerProcess(
        repo=repo,
        work_dir=work_dir,
        log_file=log_file,
        pid_file=pid_file,
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
        _remove_pid_file(wp.pid_file)
        return
    try:
        wp.process.join(timeout=timeout)
    except Exception:
        pass
        
    if wp.process.exitcode is None:
        logger.warning("Worker for %s did not stop in %.0fs; killing.", wp.repo, timeout)
        try:
            wp.process.kill()
        except OSError:
            pass
        try:
            wp.process.join(timeout=5)
        except Exception:
            pass
    _remove_pid_file(wp.pid_file)


def run_supervisor() -> None:
    """Discover repositories, check them out, and run a worker for each.

    Runs until interrupted by SIGINT or SIGTERM.
    """
    config.settings.base_dir.mkdir(parents=True, exist_ok=True)
    (config.settings.base_dir / ".logs").mkdir(parents=True, exist_ok=True)

    supervisor_pid_file = config.settings.base_dir / ".logs" / "supervisor.pid"
    _write_pid_file(supervisor_pid_file, os.getpid())

    workers: dict[str, WorkerProcess] = {}
    shutdown_requested = False
    graceful_shutdown = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal shutdown_requested, graceful_shutdown
        if signum == signal.SIGQUIT:
            logger.info("SIGQUIT received — supervisor will shut down after current tasks complete.")
            shutdown_requested = True
            graceful_shutdown = True
        else:
            logger.info("Signal %d received; shutting down supervisor…", signum)
            shutdown_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGQUIT, handle_signal)

    last_discovery: float = 0.0  # Force discovery on first iteration

    logger.info(
        "Supervisor started. base_dir=%s interval=%ds refresh=%ds",
        config.settings.base_dir, config.settings.interval, config.settings.refresh_interval,
    )
    if config.settings.include:
        logger.info("Include patterns: %s", config.settings.include)
    if config.settings.exclude:
        logger.info("Exclude patterns: %s", config.settings.exclude)

    while not shutdown_requested:
        now = time.monotonic()

        # ------------------------------------------------------------------ #
        # Discovery phase
        # ------------------------------------------------------------------ #
        if now - last_discovery >= config.settings.refresh_interval:
            last_discovery = now
            logger.info("Running repo discovery…")

            accept_pending_invitations()
            all_repos = list_accessible_repos()
            if not all_repos:
                logger.warning("No repos discovered (gh returned nothing or failed). Will retry on next refresh.")
            else:
                active = filter_repos(all_repos, config.settings.include, config.settings.exclude)
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
                        work_dir = ensure_repo_checked_out(repo, config.settings.base_dir)
                    except Exception:
                        logger.error("Skipping %s this cycle due to clone failure.", repo)
                        continue

                    _configure_git_hooks(repo, work_dir)

                    owner, name = repo.split("/", 1)
                    log_path = config.settings.base_dir / ".logs" / owner / name / "loony-worker.log"
                    pid_path = config.settings.base_dir / ".logs" / owner / name / "loony-worker.pid"
                    log_path.parent.mkdir(parents=True, exist_ok=True)

                    try:
                        GitHubClient(repo).ensure_required_labels()
                    except Exception:
                        logger.warning("Label provisioning failed for %s; continuing to launch worker.", repo)

                    try:
                        wp = launch_worker(
                            repo=repo,
                            work_dir=work_dir,
                            log_file=log_path,
                            pid_file=pid_path,
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
                    remove_repo(repo, config.settings.base_dir)

        if shutdown_requested:
            break

        # ------------------------------------------------------------------ #
        # Health-check phase
        # ------------------------------------------------------------------ #
        for repo, wp in list(workers.items()):
            rc = wp.process.exitcode
            if rc is None:
                continue  # Still running

            if graceful_shutdown:
                # Worker finished naturally during graceful shutdown; don't restart
                logger.info("Worker for %s has exited during graceful shutdown.", repo)
                workers.pop(repo)
                _remove_pid_file(wp.pid_file)
                continue

            logger.warning(
                "Worker for %s exited with code %d (restart #%d).",
                repo, rc, wp.restart_count + 1,
            )
            _remove_pid_file(wp.pid_file)

            delay = min(
                config.settings.min_restart_delay * (2 ** wp.restart_count),
                config.settings.max_restart_delay,
            )
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
                    pid_file=wp.pid_file,
                )
                new_wp.restart_count = wp.restart_count + 1
                workers[repo] = new_wp
            except Exception:
                logger.exception("Failed to restart worker for %s", repo)

        if shutdown_requested:
            break

        # Interruptible sleep for the health-check interval
        sleep_deadline = time.monotonic() + config.settings.interval
        while not shutdown_requested and time.monotonic() < sleep_deadline:
            time.sleep(min(1.0, sleep_deadline - time.monotonic()))

    # ---------------------------------------------------------------------- #
    # Shutdown
    # ---------------------------------------------------------------------- #
    logger.info("Stopping all workers…")
    for repo, wp in workers.items():
        logger.info("Stopping worker for %s", repo)
        if graceful_shutdown:
            try:
                if wp.process.pid:
                    os.kill(wp.process.pid, signal.SIGQUIT)
            except OSError:
                pass
        else:
            _terminate_worker(wp)

    if graceful_shutdown:
        logger.info("Waiting for all workers to finish current tasks…")
        for repo, wp in workers.items():
            wp.process.join()
            logger.info("Worker %s has exited.", repo)
            _remove_pid_file(wp.pid_file)

    _remove_pid_file(supervisor_pid_file)
    logger.info("Supervisor stopped.")
