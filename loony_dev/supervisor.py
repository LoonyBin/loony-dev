"""Multi-repo process supervisor: a worker (and relay) per accessible repo.

``run_supervisor`` discovers every repository the authenticated ``gh`` user can
reach (filtered by ``--include`` / ``--exclude``), checks each one out under
``<base-dir>/<owner>/<repo>``, and for each runs two long-lived child processes:

- a :class:`WorkerProcess` — ``loony-dev worker`` for that repo (the
  orchestrator loop); and
- unless ``--no-remote-control`` is set, a :class:`RemoteControlProcess` — a
  persistent ``claude rc`` **server** in the repo's base checkout. The user
  creates sessions on demand from claude.ai/code or the mobile app; each is
  isolated in its own git worktree (``--spawn worktree``). The supervisor writes
  an atomically-rewritten **connection file** carrying only server *health*
  (running / restarting / errored) — there is no single followed session, join
  URL, or per-session conversation to surface.

Both child kinds are health-checked each ``--interval`` and relaunched through
exponential backoff (``_restart_after_backoff``) when they crash. Workers restart
indefinitely; a remote-control server that crashes more than
``--max-restart-retries`` times is left in an ``errored`` state (surfaced in the
dashboard) rather than restarted forever. New repos are picked up every
``--refresh-interval``; pending invitations from ``--accept-invites-from`` users
are auto-accepted.

Coordination with the worker and dashboard is **filesystem-only** — logs and PID
files under ``<base-dir>/.logs/<owner>/<repo>/``, plus the session registry.
Signals: ``SIGQUIT`` requests a *graceful* shutdown (let in-flight tasks finish,
no restarts); ``SIGINT`` / ``SIGTERM`` shut down immediately.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loony_dev import config
from loony_dev.github import Repo

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


def launch_worker(
    repo: str, work_dir: Path, log_file: Path, pid_file: Path, base_dir: Path,
) -> WorkerProcess:
    """Spawn a worker multiprocessing.Process; stdout/stderr are redirected to *log_file*.

    *base_dir* is threaded down via ``--base-dir`` so the worker resolves the
    *same* base directory as the supervisor and web dashboard. Without it a
    spawned worker (config re-parsed fresh, ``[worker]`` carries no ``base_dir``)
    would fall back to its checkout root, writing the session registry / pipeline
    logs / leases under a tree the web never reads (#285).
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd_args = ["worker", "--repo", repo, "--work-dir", str(work_dir), "--base-dir", str(base_dir)]
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
# Remote-control process management
# ---------------------------------------------------------------------------
#
# When a worker is launched for a repo, the supervisor also spawns a sibling
# process that runs a persistent ``claude rc`` **server** in the repo's *base
# checkout* (``<base>/<owner>/<repo>``). Unlike the classic single-session
# ``claude --remote-control <id>`` mode this replaced (#304), the server is not a
# session we follow: the user creates sessions on demand from claude.ai/code or
# the mobile app, and ``--spawn worktree`` isolates each on-demand session in its
# own git worktree so the base checkout stays clean. loony-dev's job is simply to
# clone the repo and run the server so the user can create sessions on demand.
#
# Each (re)launch writes a small "connection file" describing server *health*
# (``{repo, pid, status, started_at, command}``), consumed by the web dashboard
# (see ``loony_dev/web/services.py``). The writer is atomic and additive — unknown
# keys must be ignorable by readers.

# Connection-file ``status`` values (server health, not per-session state):
STATUS_RUNNING = "running"      # server process is up
STATUS_RESTARTING = "restarting"  # crashed; backing off before a relaunch
STATUS_ERRORED = "errored"      # crashed more than --max-restart-retries times


@dataclass
class RemoteControlProcess:
    repo: str
    base_dir: Path  # cwd = the repo's base checkout (<base>/<owner>/<repo>)
    log_file: Path
    pid_file: Path
    conn_file: Path
    process: multiprocessing.Process
    started_at: float  # monotonic; supervisor health-check bookkeeping
    started_at_iso: str  # wall-clock ISO-8601, written to the connection file
    restart_count: int = field(default=0)


def _remote_control_name(repo: str) -> str:
    """Return the ``--name`` for *repo*'s ``claude rc`` server.

    ``loony-<owner>-<repo>-<hash>`` with any non-alphanumeric run collapsed to a
    single ``-``. With one rc server per repo on the same host, the default
    hostname-derived names would be indistinguishable in claude.ai/code, so we
    give each a stable, human-readable name.

    The sanitizer alone is collision-prone — ``acme/foo-bar``, ``acme/foo_bar``
    and ``acme-foo/bar`` all sanitize to ``loony-acme-foo-bar``. A short SHA-256
    digest of the original repo string keeps the name deterministic while making
    it unique (hex is alphanumeric, so it preserves the sanitizer contract).
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "-", repo).strip("-")
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:10]
    return f"loony-{safe}-{digest}"


def _remote_control_command(repo: str) -> list[str]:
    """Build the persistent ``claude rc`` server command line for *repo* (#304).

    - ``--permission-mode bypassPermissions`` lets on-demand sessions bypass
      permission checks (matches how workers already invoke ``claude``).
    - ``--spawn worktree`` isolates every on-demand session in its own git
      worktree, keeping the base checkout clean.
    - ``--no-create-session-in-dir`` starts the server idle: sessions exist only
      when the user creates them, not pre-created in the base checkout.
    - ``--name`` gives this per-repo server a distinguishable name in claude.ai.
    """
    return [
        "claude", "rc",
        "--permission-mode", "bypassPermissions",
        "--spawn", "worktree",
        "--no-create-session-in-dir",
        "--name", _remote_control_name(repo),
    ]


# Default PTY geometry for the remote-control child (see
# ``_run_remote_control_process``). The rc server logs through a PTY; these are
# just the size of that logged terminal.
# not configurable: purely the dimensions of the drained-to-log PTY, of no
# operator value now that no join-URL footer is scraped from it (#304).
_REMOTE_CONTROL_PTY_ROWS = 50
_REMOTE_CONTROL_PTY_COLS = 200


def _write_connection_file(
    conn_file: Path,
    *,
    repo: str,
    pid: int | None,
    started_at: str,
    command: list[str],
    status: str = STATUS_RUNNING,
) -> None:
    """Atomically serialise the remote-control server *health* schema (#304).

    Single source of truth for the on-disk shape, used by both the launcher and
    the child's PID refresh. The file is a minimal process-status record — no
    session id / attach handle / join URL, since there is no single followed
    session. Writes to a temp file and renames so readers never observe a partial
    file.
    """
    conn_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "repo": repo,
        "mode": "remote-control",
        "pid": pid,
        "status": status,
        "started_at": started_at,
        "command": list(command),
    }
    tmp = conn_file.with_name(conn_file.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, conn_file)
    except OSError as exc:
        logger.warning("Failed to write connection file %s: %s", conn_file, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_connection_file(path: Path) -> None:
    """Remove the connection file at *path* if it exists (logs are preserved)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove connection file %s: %s", path, exc)


def _run_remote_control_process(
    log_file: Path,
    conn_file: Path,
    repo: str,
    base_dir: Path,
    started_at: str,
) -> None:
    """Entry point for a remote-control multiprocessing.Process.

    Allocates a PTY, refreshes the connection file with the live PID, launches the
    persistent ``claude rc`` server with the PTY slave as its stdio in a new
    session, and drains the PTY master into *log_file*. Exits with ``claude``'s
    return code so the supervisor's health check observes a non-``None``
    ``exitcode`` and restarts via the backoff loop.
    """
    import errno
    import fcntl
    import pty
    import select
    import struct
    import termios

    command = _remote_control_command(repo)

    master_fd, slave_fd = pty.openpty()
    # Give the PTY a sane, non-zero geometry so the server's logged output is not
    # rendered into a 0x0 terminal. Fail fast (the backoff loop restarts us) if
    # the PTY can't be sized rather than launching claude on a broken terminal.
    try:
        winsize = struct.pack(
            "HHHH", _REMOTE_CONTROL_PTY_ROWS, _REMOTE_CONTROL_PTY_COLS, 0, 0
        )
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError as exc:
        os.close(master_fd)
        os.close(slave_fd)
        logger.error("Could not set remote-control PTY winsize for %s: %s", repo, exc)
        sys.exit(1)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(base_dir),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        import traceback
        with open(log_file, "a") as f:
            traceback.print_exc(file=f)
        sys.exit(1)

    # The child holds the slave end; this wrapper only reads from the master.
    os.close(slave_fd)

    # Refresh the connection file with this process's live PID now that the server
    # has actually launched. Writing it only after the winsize/Popen steps succeed
    # avoids persisting a "running" server for a child that died during PTY setup.
    _write_connection_file(
        conn_file,
        repo=repo,
        pid=os.getpid(),
        started_at=started_at,
        command=command,
    )

    with open(log_file, "ab") as logf:
        while True:
            try:
                rlist, _, _ = select.select([master_fd], [], [], 1.0)
            except OSError:
                break
            if master_fd in rlist:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        logger.debug("Error reading remote-control PTY for %s: %s", repo, exc)
                    break  # EIO => slave closed (child exited)
                if not chunk:
                    break
                logf.write(chunk)
                logf.flush()
            elif proc.poll() is not None:
                break

    try:
        os.close(master_fd)
    except OSError:
        pass
    sys.exit(proc.wait())


def launch_remote_control(
    repo: str,
    base_dir: Path,
    log_file: Path,
    pid_file: Path,
    conn_file: Path,
) -> RemoteControlProcess:
    """Spawn a sibling ``claude rc`` server process for *repo* in its base checkout."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    started_at_iso = datetime.now(timezone.utc).isoformat()
    command = _remote_control_command(repo)

    logger.info("Launching remote-control server for %s (log=%s)", repo, log_file)

    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=_run_remote_control_process,
        args=(log_file, conn_file, repo, base_dir, started_at_iso),
        name=f"remote-control-{repo.replace('/', '-')}",
    )
    process.start()

    _write_pid_file(pid_file, process.pid)
    _write_connection_file(
        conn_file,
        repo=repo,
        pid=process.pid,
        started_at=started_at_iso,
        command=command,
    )

    return RemoteControlProcess(
        repo=repo,
        base_dir=base_dir,
        log_file=log_file,
        pid_file=pid_file,
        conn_file=conn_file,
        process=process,
        started_at=time.monotonic(),
        started_at_iso=started_at_iso,
    )


# ---------------------------------------------------------------------------
# Web dashboard process management
# ---------------------------------------------------------------------------
#
# When ``--web`` is set the supervisor runs the read-only dashboard as a single
# managed child (one instance serves every repo, unlike the per-repo workers and
# remote-control sessions). Only ``--base-dir`` and ``--supervisor-log`` are
# forwarded so the dashboard scans the same tree and tails the same supervisor
# log this process writes; host/port and other tuning come from the ``[web]``
# config section the ``web`` command reads itself. This is a convenience switch:
# the supervisor adds no host/port flags, so the dashboard keeps its safe
# loopback default unless the operator overrides it in config.

@dataclass
class WebProcess:
    base_dir: Path
    log_file: Path
    pid_file: Path
    process: multiprocessing.Process
    started_at: float
    restart_count: int = field(default=0)


def _web_bind_address() -> tuple[str, int]:
    """Resolve the dashboard's configured host/port for log/URL surfacing only.

    The ``--web`` supervisor flag shadows the ``[web]`` section in
    ``config.settings`` (same key), so read the section straight from the config
    files here. Falls back to the ``web`` command's safe loopback defaults.
    """
    web_cfg = config._load_config().get("web", {})
    host = web_cfg.get("host", "127.0.0.1") if isinstance(web_cfg, dict) else "127.0.0.1"
    port = web_cfg.get("port", 5338) if isinstance(web_cfg, dict) else 5338
    return host, int(port)


def launch_web(
    base_dir: Path, supervisor_log: Path, log_file: Path, pid_file: Path
) -> WebProcess:
    """Spawn the read-only web dashboard as a managed child process."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    cmd_args = [
        "web",
        "--base-dir", str(base_dir),
        "--supervisor-log", str(supervisor_log),
    ]

    host, port = _web_bind_address()
    logger.info(
        "Launching web dashboard at http://%s:%d (base_dir=%s, log=%s)",
        host, port, base_dir, log_file,
    )

    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(
        target=_run_worker_process,
        args=(log_file, cmd_args),
        name="web-dashboard",
    )
    process.start()

    _write_pid_file(pid_file, process.pid)

    return WebProcess(
        base_dir=base_dir,
        log_file=log_file,
        pid_file=pid_file,
        process=process,
        started_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

# Tier-1 default (see ``--max-restart-retries`` in ``cli.py`` and the
# ``[supervisor]`` section of ``config.toml.example``): how many times a crashed
# ``claude rc`` server is relaunched before it is left in an ``errored`` state
# instead of restarted forever. Workers are unaffected (they restart
# indefinitely). Used as the fallback when the setting is absent.
_DEFAULT_MAX_RESTART_RETRIES = 5


def _remote_control_gave_up(restart_count: int, max_retries: int) -> bool:
    """Return True once a crashed rc server has exhausted its restart budget.

    ``restart_count`` is the number of restarts already performed; when it reaches
    ``max_retries`` the server is left ``errored`` instead of restarted again.
    """
    return restart_count >= max_retries


def _terminate_process(
    process: multiprocessing.Process,
    pid_file: Path,
    label: str,
    timeout: float = 10.0,
) -> None:
    """Send SIGTERM, wait *timeout* seconds, then SIGKILL if still alive.

    Shared by worker and remote-control teardown. Connection-file removal is
    remote-control-specific and handled by the caller.
    """
    try:
        process.terminate()
    except OSError:
        _remove_pid_file(pid_file)
        return
    try:
        process.join(timeout=timeout)
    except Exception:
        pass

    if process.exitcode is None:
        logger.warning("%s did not stop in %.0fs; killing.", label, timeout)
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.join(timeout=5)
        except Exception:
            pass
    _remove_pid_file(pid_file)


def _interruptible_sleep(seconds: float, should_stop: Callable[[], bool]) -> None:
    """Sleep up to *seconds*, waking early (in <=1s) once *should_stop* is true."""
    deadline = time.monotonic() + seconds
    while not should_stop() and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def _restart_after_backoff(
    record: WorkerProcess | RemoteControlProcess,
    label: str,
    relaunch: Callable[[], WorkerProcess | RemoteControlProcess],
    should_stop: Callable[[], bool],
) -> WorkerProcess | RemoteControlProcess | None:
    """Apply exponential backoff, then relaunch *record* via *relaunch*.

    Shared by worker and remote-control restart. Computes the same
    ``min(min_restart_delay * 2**restart_count, max_restart_delay)`` delay, sleeps
    interruptibly, and (unless shutdown was requested meanwhile) relaunches with an
    incremented ``restart_count``. Returns the new record, or ``None`` if shutdown
    interrupted the delay or the relaunch failed.
    """
    delay = min(
        config.settings.min_restart_delay * (2 ** record.restart_count),
        config.settings.max_restart_delay,
    )
    logger.info("Restarting %s for %s in %.1fs…", label, record.repo, delay)

    _interruptible_sleep(delay, should_stop)
    if should_stop():
        return None

    try:
        new_record = relaunch()
    except Exception:
        logger.exception("Failed to restart %s for %s", label, record.repo)
        return None
    new_record.restart_count = record.restart_count + 1
    return new_record


def run_supervisor() -> None:
    """Discover repositories, check them out, and run a worker for each.

    Runs until interrupted: ``SIGQUIT`` shuts down gracefully (in-flight tasks
    finish, no restarts), ``SIGINT`` / ``SIGTERM`` shut down immediately.
    """
    config.settings.base_dir.mkdir(parents=True, exist_ok=True)
    (config.settings.base_dir / ".logs").mkdir(parents=True, exist_ok=True)

    supervisor_pid_file = config.settings.base_dir / ".logs" / "supervisor.pid"
    _write_pid_file(supervisor_pid_file, os.getpid())

    workers: dict[str, WorkerProcess] = {}
    remote_controls: dict[str, RemoteControlProcess] = {}
    web_proc: WebProcess | None = None
    web_log_file = config.settings.base_dir / ".logs" / "web.log"
    web_pid_file = config.settings.base_dir / ".logs" / "web.pid"
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

    if config.settings.get("web"):
        try:
            web_proc = launch_web(
                config.settings.base_dir,
                config.settings.supervisor_log,
                web_log_file,
                web_pid_file,
            )
        except Exception:
            logger.exception("Failed to launch web dashboard")

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
                        Repo(repo).ensure_required_labels()
                    except Exception:
                        logger.warning("Label provisioning failed for %s; continuing to launch worker.", repo)

                    try:
                        wp = launch_worker(
                            repo=repo,
                            work_dir=work_dir,
                            log_file=log_path,
                            pid_file=pid_path,
                            base_dir=config.settings.base_dir,
                        )
                        workers[repo] = wp
                    except Exception:
                        logger.exception("Failed to launch worker for %s", repo)
                        continue

                    # Also launch a sibling ``claude rc`` server in the base
                    # checkout (unless opted out). A failure here must never
                    # block the worker.
                    if config.settings.get("no_remote_control"):
                        continue
                    log_dir = config.settings.base_dir / ".logs" / owner / name
                    try:
                        rcp = launch_remote_control(
                            repo=repo,
                            base_dir=work_dir,
                            log_file=log_dir / "remote-control.log",
                            pid_file=log_dir / "remote-control.pid",
                            conn_file=log_dir / "remote-control.json",
                        )
                        remote_controls[repo] = rcp
                    except Exception:
                        logger.exception("Failed to launch remote-control for %s", repo)

                # Stop workers for repos no longer in the active set
                for repo in current_set - active_set:
                    wp = workers.pop(repo)
                    logger.info(
                        "Repo %s is no longer active (filtered out, deleted, archived, or access revoked); "
                        "worker stopped and checkout deleted. Logs preserved at %s",
                        repo, wp.log_file.parent,
                    )
                    _terminate_process(wp.process, wp.pid_file, f"Worker for {repo}")
                    rcp = remote_controls.pop(repo, None)
                    if rcp is not None:
                        _terminate_process(rcp.process, rcp.pid_file, f"Remote-control for {repo}")
                        _remove_connection_file(rcp.conn_file)
                    remove_repo(repo, config.settings.base_dir)

        if shutdown_requested:
            break

        # ------------------------------------------------------------------ #
        # Health-check phase
        # ------------------------------------------------------------------ #
        should_stop = lambda: shutdown_requested  # noqa: E731

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

            new_wp = _restart_after_backoff(
                wp,
                "worker",
                lambda repo=repo, wp=wp: launch_worker(
                    repo=repo,
                    work_dir=wp.work_dir,
                    log_file=wp.log_file,
                    pid_file=wp.pid_file,
                    base_dir=config.settings.base_dir,
                ),
                should_stop,
            )
            if shutdown_requested:
                break
            if new_wp is not None:
                workers[repo] = new_wp

        if shutdown_requested:
            break

        # Remote-control servers restart with the same backoff as workers, but —
        # unlike workers — are given up on after --max-restart-retries crashes and
        # left in an ``errored`` state the dashboard surfaces (#304).
        max_rc_retries = config.settings.get(
            "max_restart_retries", _DEFAULT_MAX_RESTART_RETRIES
        )
        for repo, rcp in list(remote_controls.items()):
            rc = rcp.process.exitcode
            if rc is None:
                continue  # Still running

            if graceful_shutdown:
                # Nothing to drain for a server; drop it.
                logger.info("Remote-control for %s has exited during graceful shutdown.", repo)
                remote_controls.pop(repo)
                _remove_pid_file(rcp.pid_file)
                _remove_connection_file(rcp.conn_file)
                continue

            _remove_pid_file(rcp.pid_file)

            if _remote_control_gave_up(rcp.restart_count, max_rc_retries):
                # Crashed too many times: stop restarting and mark the server
                # errored so the dashboard shows it instead of churning forever.
                logger.error(
                    "Remote-control for %s crashed %d times (>= max-restart-retries=%d); "
                    "leaving it in an errored state.",
                    repo, rcp.restart_count + 1, max_rc_retries,
                )
                _write_connection_file(
                    rcp.conn_file,
                    repo=repo,
                    pid=None,
                    started_at=rcp.started_at_iso,
                    command=_remote_control_command(repo),
                    status=STATUS_ERRORED,
                )
                remote_controls.pop(repo)
                continue

            logger.warning(
                "Remote-control for %s exited with code %d (restart #%d).",
                repo, rc, rcp.restart_count + 1,
            )
            # The server is dead; mark the connection file ``restarting`` so the
            # web layer stops advertising a stale ``running`` server while we back
            # off and relaunch (the relaunch rewrites it to ``running``).
            _write_connection_file(
                rcp.conn_file,
                repo=repo,
                pid=None,
                started_at=rcp.started_at_iso,
                command=_remote_control_command(repo),
                status=STATUS_RESTARTING,
            )

            new_rcp = _restart_after_backoff(
                rcp,
                "remote-control",
                lambda repo=repo, rcp=rcp: launch_remote_control(
                    repo=repo,
                    base_dir=rcp.base_dir,
                    log_file=rcp.log_file,
                    pid_file=rcp.pid_file,
                    conn_file=rcp.conn_file,
                ),
                should_stop,
            )
            if shutdown_requested:
                break
            if new_rcp is not None:
                remote_controls[repo] = new_rcp

        if shutdown_requested:
            break

        # The web dashboard is a single child (not per-repo); restart it with the
        # same exponential backoff as workers when it crashes.
        if web_proc is not None and web_proc.process.exitcode is not None:
            rc = web_proc.process.exitcode
            _remove_pid_file(web_proc.pid_file)
            if graceful_shutdown:
                logger.info("Web dashboard exited during graceful shutdown.")
                web_proc = None
            else:
                logger.warning(
                    "Web dashboard exited with code %d (restart #%d).",
                    rc, web_proc.restart_count + 1,
                )
                delay = min(
                    config.settings.min_restart_delay * (2 ** web_proc.restart_count),
                    config.settings.max_restart_delay,
                )
                logger.info("Restarting web dashboard in %.1fs…", delay)
                prev_count = web_proc.restart_count
                web_proc = None
                _interruptible_sleep(delay, should_stop)
                if shutdown_requested:
                    break
                try:
                    web_proc = launch_web(
                        config.settings.base_dir,
                        config.settings.supervisor_log,
                        web_log_file,
                        web_pid_file,
                    )
                    web_proc.restart_count = prev_count + 1
                except Exception:
                    logger.exception("Failed to restart web dashboard")

        # Interruptible sleep for the health-check interval
        sleep_deadline = time.monotonic() + config.settings.interval
        while not shutdown_requested and time.monotonic() < sleep_deadline:
            time.sleep(min(1.0, sleep_deadline - time.monotonic()))

    # ---------------------------------------------------------------------- #
    # Shutdown
    # ---------------------------------------------------------------------- #
    # The web dashboard is read-only with nothing to drain, so it is terminated
    # on both graceful (SIGQUIT) and immediate shutdown.
    if web_proc is not None:
        logger.info("Stopping web dashboard")
        _terminate_process(web_proc.process, web_proc.pid_file, "Web dashboard")

    # Remote-control servers have nothing to drain, and their lifetime is the
    # supervisor's lifetime (#304), so they are terminated on both graceful
    # (SIGQUIT) and immediate shutdown.
    for repo, rcp in remote_controls.items():
        logger.info("Stopping remote-control for %s", repo)
        _terminate_process(rcp.process, rcp.pid_file, f"Remote-control for {repo}")
        _remove_connection_file(rcp.conn_file)

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
            _terminate_process(wp.process, wp.pid_file, f"Worker for {repo}")

    if graceful_shutdown:
        logger.info("Waiting for all workers to finish current tasks…")
        for repo, wp in workers.items():
            wp.process.join()
            logger.info("Worker %s has exited.", repo)
            _remove_pid_file(wp.pid_file)

    _remove_pid_file(supervisor_pid_file)
    logger.info("Supervisor stopped.")
