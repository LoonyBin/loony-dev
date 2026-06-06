"""Framework-agnostic data layer for the web dashboard.

These functions derive all state from the supervisor's on-disk file layout
under ``<base>/.logs/...`` (and repo checkouts under ``<base>/<owner>/<repo>``).
The web process runs separately from the supervisor and never shares any
in-memory state with it, so everything here is reconstructed from the
filesystem.

No FastAPI imports live in this module — the route layer is a thin wrapper
around these pure functions, which keeps them directly unit-testable.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loony_dev.git import GitRepo

WORKER_LOG_NAME = "loony-worker.log"
WORKER_PID_NAME = "loony-worker.pid"

# Expected location for #129's remote-control "connection JSON files".
# NOTE: #129 is not merged yet — there are no session/connection files in the
# repo. This path and the parsing in ``list_sessions`` are a best-effort guess
# and MUST be reconciled with #129's actual output once it lands. Until then the
# directory simply does not exist and ``list_sessions`` returns ``[]``.
SESSIONS_DIR_NAME = ".sessions"


# ---------------------------------------------------------------------------
# Data views (plain dataclasses, JSON-serialisable via asdict)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerView:
    repo: str
    pid: int | None
    status: str  # "running" | "stale" | "unknown"
    started_at: str | None  # ISO-8601, approx (PID-file mtime)
    exitcode: None  # always null in v1 — unobservable cross-process
    log_path: str


@dataclass(frozen=True)
class WorktreeView:
    repo: str
    path: str
    branch: str | None
    head: str | None
    detached: bool
    bare: bool


@dataclass(frozen=True)
class SessionView:
    session_id: str
    repo: str | None
    key: str | None


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def process_status(pid: int) -> str:
    """Map a PID to a worker status using the 3-state ``os.kill(pid, 0)`` probe.

    Returns:
        "running" — the process exists and we can signal it.
        "unknown" — the process exists but is owned by another user
                    (``PermissionError``).
        "stale"   — no such process (``ProcessLookupError``).
    """
    try:
        os.kill(pid, 0)
        return "running"
    except PermissionError:
        return "unknown"
    except (ProcessLookupError, OSError):
        return "stale"


def _read_pid(pid_path: Path) -> int | None:
    """Read a bare integer PID from *pid_path*; return None if unreadable."""
    try:
        return int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _iso_mtime(path: Path) -> str | None:
    """Return *path*'s mtime as a UTC ISO-8601 string, or None if unavailable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_repos(base_dir: Path) -> list[tuple[str, str, Path]]:
    """Scan ``.logs/<owner>/<repo>/`` and return ``(owner, name, repo_log_dir)``.

    Mirrors the discovery in ``tui.SupervisorApp`` / ``_discover_entries`` so the
    web dashboard surfaces exactly the same set of workers. Hidden directories
    (e.g. ``.sessions``) are skipped.
    """
    logs_dir = base_dir / ".logs"
    found: list[tuple[str, str, Path]] = []
    if not logs_dir.exists():
        return found
    for owner_dir in sorted(logs_dir.iterdir()):
        if not owner_dir.is_dir() or owner_dir.name.startswith("."):
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            found.append((owner_dir.name, repo_dir.name, repo_dir))
    return found


def list_workers(base_dir: Path) -> list[WorkerView]:
    """Return one :class:`WorkerView` per discovered worker log directory.

    ``repo`` is derived from the PID-file path. ``started_at`` is the PID file's
    mtime (written once at worker launch) used as an approximate launch time.
    ``exitcode`` is always ``None`` — a separate process cannot observe the exit
    code of the supervisor's ``multiprocessing.Process`` workers; the knowable
    information is conveyed via ``status`` instead.
    """
    workers: list[WorkerView] = []
    for owner, name, repo_dir in _discover_repos(base_dir):
        pid_path = repo_dir / WORKER_PID_NAME
        log_path = repo_dir / WORKER_LOG_NAME
        pid = _read_pid(pid_path)
        if pid is None:
            status = "stale"
            started_at = None
        else:
            status = process_status(pid)
            started_at = _iso_mtime(pid_path)
        workers.append(
            WorkerView(
                repo=f"{owner}/{name}",
                pid=pid,
                status=status,
                started_at=started_at,
                exitcode=None,
                log_path=str(log_path),
            )
        )
    return workers


def list_worktrees(base_dir: Path) -> list[WorktreeView]:
    """Flatten ``git worktree list`` across every checked-out repo.

    A repo is checked out at ``<base>/<owner>/<repo>`` (see
    ``supervisor.ensure_repo_checked_out``). For each repo discovered from the
    ``.logs`` scan that has a git checkout, list its worktrees and tag each with
    the owning ``repo``. Repos without a checkout (or that error) are skipped.
    """
    worktrees: list[WorktreeView] = []
    for owner, name, _repo_dir in _discover_repos(base_dir):
        checkout = base_dir / owner / name
        if not (checkout / ".git").exists():
            continue
        try:
            infos = GitRepo(work_dir=checkout).list_worktrees()
        except Exception:
            continue
        for info in infos:
            worktrees.append(
                WorktreeView(
                    repo=f"{owner}/{name}",
                    path=str(info.path),
                    branch=info.branch,
                    head=info.head,
                    detached=info.detached,
                    bare=info.bare,
                )
            )
    return worktrees


def list_sessions(base_dir: Path) -> list[SessionView]:
    """Parse remote-control connection JSON files from ``.logs/.sessions/``.

    DEPENDENCY: #129 (remote-control connection files) is not merged yet, so the
    directory does not exist and this returns ``[]``. The parser is intentionally
    defensive — unknown/extra keys are ignored and malformed files are skipped —
    so the dashboard works today and lights up automatically once #129 lands.
    The path/schema below MUST be reconciled with #129's actual output.
    """
    import json

    sessions_dir = base_dir / ".logs" / SESSIONS_DIR_NAME
    if not sessions_dir.exists():
        return []

    sessions: list[SessionView] = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = data.get("session_id") or path.stem
        sessions.append(
            SessionView(
                session_id=str(session_id),
                repo=data.get("repo"),
                key=data.get("key"),
            )
        )
    return sessions


# ---------------------------------------------------------------------------
# Log tail
# ---------------------------------------------------------------------------

class LogNotFoundError(Exception):
    """Raised when a requested worker log path is invalid or does not exist."""


def _safe_repo_log_path(base_dir: Path, owner: str, repo: str) -> Path:
    """Resolve the worker log path for ``owner/repo``, rejecting traversal.

    Rejects any ``owner``/``repo`` segment containing path separators or ``..``
    and confirms the resolved log path stays within ``<base>/.logs``.
    """
    for segment in (owner, repo):
        if not segment or segment in (".", "..") or "/" in segment or "\\" in segment or "\x00" in segment:
            raise LogNotFoundError(f"invalid path segment: {segment!r}")

    logs_root = (base_dir / ".logs").resolve()
    candidate = (logs_root / owner / repo / WORKER_LOG_NAME).resolve()
    if logs_root not in candidate.parents:
        raise LogNotFoundError("resolved path escapes logs directory")
    return candidate


def tail_log(base_dir: Path, owner: str, repo: str, lines: int) -> list[str]:
    """Return up to the last *lines* lines of ``owner/repo``'s worker log.

    Raises :class:`LogNotFoundError` for invalid segments or a missing log file.
    Reads the whole file but keeps only the tail in a bounded ``deque`` so memory
    stays proportional to *lines* rather than file size.
    """
    log_path = _safe_repo_log_path(base_dir, owner, repo)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            tail: deque[str] = deque(fh, maxlen=max(lines, 0))
    except FileNotFoundError as exc:
        raise LogNotFoundError(f"no log for {owner}/{repo}") from exc
    return [line.rstrip("\n") for line in tail]
